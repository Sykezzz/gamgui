"""Synthetic subprocess/HTTP protocol only; no executable or GitHub operation."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch
from scripts.gam_repair_atomic_gh import GhAtomicRef, OWNER_ID, environment, http_result
from scripts.gam_repair_native import NativeGitHub
from scripts.gam_repair_claim import Hold, REPO_ID


def response(status, body):
    return subprocess.CompletedProcess([], 0 if status < 400 else 1,
        'HTTP/2.0 ' + str(status) + ' Status\r\nContent-Type: application/json\r\n\r\n' + json.dumps(body), 'synthetic private stderr')

ACTOR = {'id': OWNER_ID, 'login': 'Sykezzz'}
REPO = {'id': REPO_ID, 'full_name': 'Sykezzz/gamgui', 'default_branch': 'district-main', 'owner': {'id': OWNER_ID}}
PAYLOAD = {'ref': 'refs/heads/automation/gam-repair-' + 'a'*40 + '-attempt-1', 'sha': 'b'*40}


class AtomicGhTests(unittest.TestCase):
    def port(self, responses, *, activated=False):
        runner = Mock(side_effect=responses)
        with patch.object(Path, 'is_file', return_value=True):
            port = GhAtomicRef(str(Path.cwd()/'gh.exe'), activated=activated, runner=runner)
        return port, runner

    def test_default_disabled_and_direct_request_cannot_bypass(self):
        port, runner = self.port([])
        for call in (lambda: port('Sykezzz/gamgui', PAYLOAD),
                     lambda: port.request('POST', '/repos/Sykezzz/gamgui/git/refs', PAYLOAD)):
            with self.assertRaises(Hold): call()
        runner.assert_not_called()

    def test_read_only_verification_does_not_activate_writes(self):
        port, runner = self.port([response(200, ACTOR), response(200, REPO)])
        result = port.verify_read_only()
        self.assertFalse(result['writes_enabled'])
        with self.assertRaises(Hold): port.require_ready()
        self.assertEqual(runner.call_count, 2)

    def test_exact_create_only_POST_stdin_shellfalse_and_deadline(self):
        port, runner = self.port([response(200, ACTOR), response(200, REPO), response(200, ACTOR), response(200, REPO),
                                 response(201, {'ref': PAYLOAD['ref'], 'object': {'sha': PAYLOAD['sha']}})], activated=True)
        port.verify_read_only()
        result = port('Sykezzz/gamgui', PAYLOAD)
        self.assertEqual(result['http_status'], 201)
        call = runner.call_args
        self.assertEqual(call.args[0][1:], ['api', '--hostname', 'github.com', '--method', 'POST', '--include',
                                          '/repos/Sykezzz/gamgui/git/refs', '--input', '-'])
        self.assertEqual(json.loads(call.kwargs['input']), PAYLOAD)
        self.assertIs(call.kwargs['shell'], False)
        self.assertEqual(call.kwargs['timeout'], 15)
        self.assertEqual(call.kwargs['env']['GH_PROMPT_DISABLED'], '1')
        self.assertEqual(runner.call_count, 5)

    def test_conflict_exact_http422_and_reference_message(self):
        port, runner = self.port([response(200, ACTOR), response(200, REPO), response(200, ACTOR), response(200, REPO),
                                 response(422, {'message': 'Reference already exists'})], activated=True)
        port.verify_read_only()
        self.assertEqual(port('Sykezzz/gamgui', PAYLOAD)['http_status'], 422)

    def test_nonconflict_422_or_200_upsert_is_held(self):
        for bad in (response(422, {'message': 'Validation Failed'}), response(200, {'ref': PAYLOAD['ref'], 'object': {'sha': PAYLOAD['sha']}})):
            port, runner = self.port([response(200, ACTOR), response(200, REPO), response(200, ACTOR), response(200, REPO), bad], activated=True)
            port.verify_read_only()
            with self.assertRaises(Hold): port('Sykezzz/gamgui', PAYLOAD)
            self.assertEqual(runner.call_count, 5)

    def test_timeout_no_retry_or_stderr_disclosure(self):
        port, runner = self.port([response(200, ACTOR), response(200, REPO), response(200, ACTOR), response(200, REPO),
                                 subprocess.TimeoutExpired('synthetic', 15)], activated=True)
        port.verify_read_only()
        with self.assertRaises(Hold) as error: port('Sykezzz/gamgui', PAYLOAD)
        self.assertEqual(runner.call_count, 5)
        self.assertNotIn('private stderr', str(error.exception))

    def test_wrong_identity_or_repo_never_verifies_or_posts(self):
        for actor, repo in (({'id': 999, 'login': 'Sykezzz'}, REPO), (ACTOR, dict(REPO, id=123))):
            port, runner = self.port([response(200, actor), response(200, repo)], activated=True)
            with self.assertRaises(Hold): port.verify_read_only()
            with self.assertRaises(Hold): port('Sykezzz/gamgui', PAYLOAD)
            self.assertEqual(runner.call_count, 2)

    def test_native_binding_preflight_holds_before_orphan_objects(self):
        port, runner = self.port([])
        tools = Mock()
        adapter = NativeGitHub(tools, atomic_create_ref=port, writes_enabled=True)
        with self.assertRaises(Hold): adapter.require_claim_binding()
        tools.assert_not_called()
        runner.assert_not_called()

    def test_trusted_host_inherits_existing_GH_auth_but_drops_other_credentials_and_overrides(self):
        env = environment({'PATH': 'trusted', 'USERPROFILE': 'fixture', 'GH_TOKEN': 'fake', 'GITHUB_TOKEN': 'fake',
                           'OPENAI_API_KEY': 'fake', 'SSH_AUTH_SOCK': 'fake', 'GH_HOST': 'other', 'GH_CONFIG_DIR': 'other',
                           'HTTPS_PROXY': 'other', 'GH_DEBUG': 'api'})
        self.assertEqual(set(env), {'PATH', 'USERPROFILE', 'GH_TOKEN', 'GITHUB_TOKEN',
                                  'GH_PROMPT_DISABLED', 'GH_NO_UPDATE_NOTIFIER', 'GH_NO_EXTENSION_UPDATE_NOTIFIER'})

    def test_fixed_endpoint_ref_repo_and_input_prevent_shell_injection(self):
        port, runner = self.port([])
        port.activated = port.verified = True
        for method, path, data in (('POST', '/repos/other/repo/git/refs', PAYLOAD), ('PATCH', '/repos/Sykezzz/gamgui/git/refs', PAYLOAD),
                                   ('POST', '/repos/Sykezzz/gamgui/git/refs', {'ref': 'refs/heads/district-main', 'sha': 'b'*40}),
                                   ('POST', '/repos/Sykezzz/gamgui/git/refs', dict(PAYLOAD, sha='$(secret)')),
                                   ('POST', '/repos/Sykezzz/gamgui/git/refs', dict(PAYLOAD, force=True))):
            with self.assertRaises(Hold): port.request(method, path, data)
        runner.assert_not_called()

    def test_http_status_not_stderr_error_text_and_bounded_body(self):
        for output, code in (('Reference already exists', 1), ('HTTP/2.0 201 Created\n\n{}', 1),
                             ('HTTP/2.0 422 Error\n\n{}', 0), ('HTTP/2.0 201 Created\n\n[]', 0), ('x'*1048577, 0)):
            with self.assertRaises((Hold, ValueError)): http_result(output, code)


if __name__ == '__main__':
    unittest.main()
