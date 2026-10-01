"""Synthetic connector/lifecycle protocol only: no GitHub writes or task launch."""
import base64
import json
import unittest
from unittest.mock import Mock
from tests.test_gam_repair_claim import GitHub, REPO, INCIDENT, NONCE1, NONCE2
from scripts.gam_repair_claim import Hold, Conflict
from scripts.gam_repair_native import NativeGitHub, Coordinator, TaskStatus, decode, ORIGIN


def envelope(value, *, raw=False):
    return {'isError': False, 'structuredContent': {'content': json.dumps(value)} if raw else value}


class Tools:
    def __init__(self):
        self.github = GitHub()
        self.calls = []
        self.candidate_files = None

    def __call__(self, name, args):
        self.calls.append((name, args))
        g = self.github
        if name == 'github_get_profile':
            return envelope({'id': str(g.actor['id']), 'nickname': g.actor['login']})
        if name == 'github_fetch':
            assert args['url'].startswith(ORIGIN)
            path = args['url'].removeprefix(ORIGIN)
            value = g('GET', path, optional=True)
            if value is None:
                return {'isError': True, 'structuredContent': {'error_code': 'NOT_FOUND', 'error_data': {'status': '404'}}}
            if path.startswith('/contents/'):
                raw = json.dumps(value['content_json']).encode()
                value = {'type': 'file', 'encoding': 'base64', 'size': len(raw), 'content': base64.b64encode(raw).decode()}
            if self.candidate_files is not None and path.startswith('/pulls/80') and '/files?' in path:
                value = self.candidate_files
            return envelope(value, raw=True)
        assert args.get('repository_full_name', args.get('repo_full_name')) == 'Sykezzz/gamgui'
        if name == 'github_create_tree':
            return envelope(g('POST', '/git/trees', {'base_tree': args['base_tree_sha'], 'tree': args['tree_elements']}))
        if name == 'github_create_commit':
            return envelope(g('POST', '/git/commits', {'message': args['message'], 'tree': args['tree_sha'], 'parents': [args['parent_sha']]}))
        if name == 'github_create_pull_request':
            value = g('POST', '/pulls', {k: args[k] for k in ('head', 'base', 'draft', 'title', 'body')})
            # Normalized connector lacks full REST head/base. Adapter re-fetches.
            return envelope({'number': value['number'], 'draft': True})
        if name == 'github_add_comment_to_issue':
            return envelope(g('POST', f"/issues/{args['pr_number']}/comments", {'body': args['comment']}))
        if name == 'github_update_pull_request':
            return envelope(g('PATCH', f"/pulls/{args['pr_number']}", {'state': args['state']}))
        raise AssertionError(name)

    def atomic(self, repository, payload):
        assert repository == 'Sykezzz/gamgui'
        try:
            result = self.github('POST', '/git/refs', payload)
        except Conflict:
            return {'http_status': 422, 'body': {'message': 'Reference already exists'}}
        return {'http_status': 201, 'body': {'ref': payload['ref'], 'object': result['object']}}


class Tasks:
    """Host-authoritative synthetic lifecycle; never starts a process/model."""
    def __init__(self):
        self.states = {}
        self.starts = []
        self.stops = []
        self.prepared = []
        self.keep_running = False
        self.fail_start = False
        self.fail_prepare = False

    def prepare(self, stage, ticket, candidate_sha):
        if self.fail_prepare:
            raise TimeoutError('synthetic prepare ambiguity')
        handle = stage + str(len(self.states))
        self.states[handle] = TaskStatus(handle, 'prepared', 0)
        self.prepared.append((stage, ticket, candidate_sha))
        return handle

    def start(self, handle):
        self.starts.append(handle)
        self.states[handle] = TaskStatus(handle, 'running', 1)
        if self.fail_start:
            raise TimeoutError('accepted start response lost')

    def status(self, handle):
        return self.states[handle]

    def stop(self, handle):
        self.stops.append(handle)
        if not self.keep_running:
            self.states[handle] = TaskStatus(handle, 'cancelled', 0)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tools = Tools()
        self.api = NativeGitHub(self.tools, atomic_create_ref=self.tools.atomic, writes_enabled=True)
        self.tasks = Tasks()
        self.host = Coordinator(self.api, self.tasks, activated=True)

    def test_observed_native_envelopes_and_profile_string_id(self):
        self.assertEqual(self.api('GET', ''), REPO)
        self.assertEqual(self.api('GET', '/user'), {'id': 81, 'login': 'Sykezzz'})
        path = f'/.github/repair-requests/gam-bump-{INCIDENT}.json'
        self.assertEqual(self.api('GET', '/contents' + path + '?ref=' + 'b'*40)['content_json'], self.tools.github.metadata)

    def test_structured_404_only_optional_exact_claim_ref(self):
        path = f'/git/ref/heads/automation/gam-repair-{INCIDENT}-attempt-1'
        self.assertIsNone(self.api('GET', path, optional=True))
        with self.assertRaises(Hold): self.api('GET', path)
        with self.assertRaises(Hold): self.api('GET', '/pulls/700', optional=True)
        # Unstructured text saying 404 must not be treated as absence.
        with self.assertRaises(Hold): decode({'isError': True, 'content': [{'text': '404'}]}, optional=True)

    def test_mutations_disabled_before_any_orphan_object(self):
        api = NativeGitHub(self.tools, writes_enabled=True)
        host = Coordinator(api, self.tasks, activated=True)
        with self.assertRaises(Hold): host.take(700, NONCE1)
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(self.tools.github.writes, [])

    def test_default_coordinator_cannot_claim(self):
        with self.assertRaises(Hold): Coordinator(self.api, self.tasks).take(700, NONCE1)
        self.assertEqual(self.tools.github.writes, [])

    def test_real_mapping_roundtrip_claim_and_no_upsert_tool(self):
        ticket = self.host.take(700, NONCE1)
        self.assertEqual(ticket.attempt, 1)
        names = [name for name, _ in self.tools.calls]
        self.assertNotIn('github_create_branch', names)
        self.assertNotIn('github_update_ref', names)
        self.assertEqual(self.tools.github.pulls[ticket.repair_pr]['state'], 'open')

    def test_verified_atomic_422_conflict_not_other_error(self):
        branch = f'refs/heads/automation/gam-repair-{INCIDENT}-attempt-1'
        data = {'ref': branch, 'sha': 'c'*40}
        self.api('POST', '/git/refs', data)
        with self.assertRaises(Conflict): self.api('POST', '/git/refs', data)
        for response in ({'http_status': 409, 'body': {}}, {'http_status': 422, 'body': {'message': 'Validation Failed'}},
                         {'http_status': 200, 'body': {'ref': branch, 'object': {'sha': 'c'*40}}}):
            api = NativeGitHub(self.tools, atomic_create_ref=lambda *_: response, writes_enabled=True)
            with self.assertRaises(Hold): api('POST', '/git/refs', data)

    def test_fixed_endpoint_and_method_no_publication_or_security(self):
        before = len(self.tools.calls)
        cases = [('DELETE', '/git/refs', {}), ('PATCH', '/git/refs', {}), ('PUT', '/pulls/700/merge', {}),
                 ('PATCH', '/pulls/700', {'state': 'open'}), ('GET', '/../../secrets', None),
                 ('GET', '/pulls/700?extra=true', None), ('GET', '/actions/runs/123#fragment', None)]
        for method, path, data in cases:
            with self.subTest(method=method, path=path), self.assertRaises(Hold): self.api(method, path, data)
        self.assertEqual(len(self.tools.calls), before)

    def test_raw_contents_must_be_bounded_file_not_link_or_wrong_size(self):
        path = '/contents/.github/repair-requests/gam-bump-' + INCIDENT + '.json?ref=' + 'b'*40
        for value in ({'type': 'symlink', 'encoding': 'base64', 'size': 2, 'content': 'e30='},
                      {'type': 'file', 'encoding': 'base64', 'size': 3, 'content': 'e30='},
                      {'type': 'file', 'encoding': 'base64', 'size': 5000, 'content': 'e30='}):
            api = NativeGitHub(lambda *_: envelope(value, raw=True))
            with self.assertRaises(Hold): api('GET', path)

    def test_normalized_contents_base64_is_not_raw_fetch_json(self):
        raw = json.dumps(self.tools.github.metadata).encode()
        file = {'type': 'file', 'encoding': 'base64', 'size': len(raw), 'content': base64.b64encode(raw).decode()}
        api = NativeGitHub(lambda *_: envelope(file))
        path = '/contents/.github/repair-requests/gam-bump-' + INCIDENT + '.json?ref=' + 'b'*40
        self.assertEqual(api('GET', path)['content_json'], self.tools.github.metadata)

    def test_changed_claim_before_diagnosis_blocks_all_launches(self):
        for changed in ('ref', 'pr', 'receipt'):
            with self.subTest(changed=changed):
                self.setUp()
                ticket = self.host.take(700, NONCE1)
                if changed == 'ref': self.tools.github.refs[ticket.branch] = 'd'*40
                elif changed == 'pr': self.tools.github.pulls[ticket.repair_pr]['draft'] = False
                else: self.tools.github.bad_receipt = True
                with self.assertRaises(Hold): self.host.launch('diagnosis')
                self.assertEqual(self.tasks.starts, [])

    def test_no_launch_before_ticket_and_registered_before_start(self):
        with self.assertRaises(Hold): self.host.launch('diagnosis')
        self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        self.assertEqual(self.host.handles['diagnosis'], handle)
        self.assertEqual(self.tasks.starts, [handle])
        with self.assertRaises(Hold): self.host.launch('diagnosis')

    def test_running_or_descendant_task_blocks_independent_stage(self):
        self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        with self.assertRaises(Hold): self.host.require_stopped()
        self.tasks.states[handle] = TaskStatus(handle, 'completed', 1)
        with self.assertRaises(Hold): self.host.require_stopped()

    def test_termination_stops_and_confirms_every_task_before_finish(self):
        self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        # Synthetic raw comment endpoint needed by normalized comment re-fetch.
        original = self.tools.github.operate
        def operate(method, path, data, optional):
            if method == 'GET' and path.startswith('/issues/comments/'):
                return next(c for values in self.tools.github.comments.values() for c in values if c['id'] == int(path.split('/')[-1]))
            return original(method, path, data, optional)
        self.tools.github.operate = operate
        result = self.host.terminate('failed')
        self.assertEqual(self.tasks.stops, [handle])
        self.assertFalse(result['merged'])
        self.assertEqual(self.tools.github.pulls[self.host.ticket.repair_pr]['state'], 'closed')
        with self.assertRaises(Hold): self.host.launch('tests', candidate_sha='d'*40)

    def test_stop_failed_no_terminal_write_and_no_next_attempt(self):
        self.host.take(700, NONCE1)
        self.host.launch('diagnosis')
        self.tasks.keep_running = True
        before = len(self.tools.github.writes)
        with self.assertRaises(Hold): self.host.terminate('failed')
        self.assertEqual(len(self.tools.github.writes), before)
        next_host = Coordinator(self.api, Tasks(), activated=True)
        with self.assertRaises(Hold): next_host.take(700, NONCE2)

    def test_model_return_text_or_wrong_handle_is_not_stop_evidence(self):
        self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        for status in ({'task_stopped': True}, TaskStatus('foreign', 'completed', 0), TaskStatus(handle, 'completed', False)):
            self.tasks.states[handle] = status
            with self.assertRaises(Hold): self.host.require_stopped()

    def test_start_lost_response_keeps_registered_handle_and_hold(self):
        self.host.take(700, NONCE1)
        self.tasks.fail_start = True
        with self.assertRaises(Hold): self.host.launch('diagnosis')
        self.assertIn('diagnosis', self.host.handles)
        self.assertTrue(self.host.launch_ambiguous)
        self.tasks.keep_running = True
        before = len(self.tools.github.writes)
        with self.assertRaises(Hold): self.host.terminate('failed')
        self.assertEqual(len(self.tools.github.writes), before)

    def test_prepare_ambiguity_without_handle_cannot_certify_stop(self):
        self.host.take(700, NONCE1)
        self.tasks.fail_prepare = True
        with self.assertRaises(Hold): self.host.launch('diagnosis')
        before = len(self.tools.github.writes)
        with self.assertRaises(Hold): self.host.terminate('failed')
        self.assertEqual(len(self.tools.github.writes), before)

    def test_review_and_tests_exact_candidate_from_actual_pr(self):
        ticket = self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        self.tasks.states[handle] = TaskStatus(handle, 'completed', 0, 'success')
        with self.assertRaises(Hold): self.host.launch('tests')
        self.tools.github.pulls[ticket.repair_pr]['head']['sha'] = 'd'*40
        self.tools.github.refs[ticket.branch] = 'd'*40
        self.tools.candidate_files = [{'filename': 'scripts/bump_gam.py', 'status': 'modified'}]
        tests = self.host.launch('tests', candidate_sha='d'*40)
        self.tasks.states[tests] = TaskStatus(tests, 'completed', 0, 'success')
        self.tools.github.pulls[ticket.repair_pr]['head']['sha'] = 'e'*40
        with self.assertRaises(Hold): self.host.launch('review', candidate_sha='e'*40)
        self.tools.github.pulls[ticket.repair_pr]['head']['sha'] = 'd'*40
        review = self.host.launch('review', candidate_sha='d'*40)
        self.assertEqual(self.tasks.prepared[-1][2], 'd'*40)
        self.assertEqual(self.host.handles['review'], review)

    def test_receipt_workflow_or_unrelated_final_diff_blocks_review(self):
        ticket = self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        self.tasks.states[handle] = TaskStatus(handle, 'completed', 0, 'success')
        self.tools.github.pulls[ticket.repair_pr]['head']['sha'] = 'd'*40
        self.tools.github.refs[ticket.branch] = 'd'*40
        for path in ('.github/workflows/gam-update.yml', '.github/repair-attempts/a.json', 'gamgui/auth.py'):
            self.tools.candidate_files = [{'filename': path}]
            with self.assertRaises(Hold): self.host.launch('tests', candidate_sha='d'*40)
        self.assertEqual(len(self.tasks.starts), 1)

    def test_missing_or_failed_tests_cannot_start_independent_review(self):
        ticket = self.host.take(700, NONCE1)
        handle = self.host.launch('diagnosis')
        self.tasks.states[handle] = TaskStatus(handle, 'completed', 0, 'success')
        self.tools.github.pulls[ticket.repair_pr]['head']['sha'] = 'd'*40
        self.tools.github.refs[ticket.branch] = 'd'*40
        self.tools.candidate_files = [{'filename': 'scripts/bump_gam.py'}]
        with self.assertRaises(Hold): self.host.launch('review', candidate_sha='d'*40)
        tests = self.host.launch('tests', candidate_sha='d'*40)
        self.tasks.states[tests] = TaskStatus(tests, 'completed', 0, 'failure')
        with self.assertRaises(Hold): self.host.launch('review', candidate_sha='d'*40)
        self.assertNotIn('review', self.host.handles)


if __name__ == '__main__':
    unittest.main()
