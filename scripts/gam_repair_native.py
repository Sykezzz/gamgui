"""Trusted native host binding. No service, network, credentials or model launch.

The host supplies its existing connector tools and lifecycle operations. The
current create_branch catalog says 'create or update', so it is deliberately
NOT used for the atomic claim. A verified create-only REST port is required.
Repository/model code must never receive this adapter or the lifecycle port.
"""
import base64
from dataclasses import dataclass
import json
import re
import threading
from scripts import gam_repair_claim as protocol

REPO = protocol.REPO
ORIGIN = 'https://api.github.com/repos/' + REPO
HEX = r'[0-9a-f]{40}'
BRANCH = rf'automation/gam-repair-{HEX}-attempt-[12]'
MARKER = rf'automation/gam-repair-request-{HEX}'
RECEIPT = rf'\.github/repair-attempts/{HEX}-[12]\.json'
CONTENT = rf'(?:{RECEIPT}|\.github/repair-requests/gam-bump-{HEX}\.json)'
GET_PATHS = (
    r'', r'/user', r'/actions/workflows/318866333', r'/actions/runs/[1-9][0-9]*',
    rf'/actions/workflows/318866333/runs\?branch=district-main&head_sha={HEX}&per_page=100',
    rf'/git/ref/heads/(?:district-main|{BRANCH}|{MARKER})',
    r'/pulls/[1-9][0-9]*', r'/pulls/[1-9][0-9]*/files\?per_page=100',
    rf'/pulls\?state=all&base=district-main&head=Sykezzz:{BRANCH}&per_page=100',
    r'/issues/[1-9][0-9]*/comments\?per_page=100', r'/issues/comments/[1-9][0-9]*',
    rf'/contents/{CONTENT}\?ref={HEX}', rf'/git/commits/{HEX}', rf'/compare/{HEX}\.\.\.{HEX}',
)


def need(value, message):
    protocol.need(value, message)


def decode(result, *, optional=False):
    """Actual native MCP envelopes, not text/error-message heuristics."""
    need(isinstance(result, dict), 'connector result absent')
    data = result.get('structuredContent')
    need(isinstance(data, dict), 'structured connector result required')
    if result.get('isError'):
        if (optional and data.get('error_code') == 'NOT_FOUND'
                and data.get('error_data', {}).get('status') == '404'):
            return None
        raise protocol.Hold('connector operation failed; no retry/fallback')
    raw = data.get('content')
    # Raw fetch wraps a JSON string. A normalized contents API file instead
    # carries base64 in its own content field; never JSON-decode that string.
    if isinstance(raw, str) and not ('encoding' in data and 'type' in data):
        need(len(raw.encode('utf-8')) <= 1048576, 'bounded connector response')
        return json.loads(raw)
    need(len(json.dumps(data)) <= 1048576, 'bounded connector response')
    return data


class NativeGitHub:
    def __init__(self, tools=protocol.denied, *, atomic_create_ref=None, writes_enabled=False):
        self.tools = tools
        self.atomic_create_ref = atomic_create_ref
        self.writes_enabled = writes_enabled

    def require_claim_binding(self):
        # Check before creating orphan trees/commits. Never infer atomicity from
        # a tool's name or a prompt, or silently substitute update_ref/gh/PAT.
        need(self.writes_enabled is True and callable(self.atomic_create_ref),
             'verified atomic create-only native ref port not bound; repair disabled')
        readiness = getattr(self.atomic_create_ref, 'require_ready', None)
        if readiness is not None:
            readiness()

    def get(self, path, *, optional=False):
        need(isinstance(path, str) and any(re.fullmatch(p, path) for p in GET_PATHS), 'fixed GET endpoint')
        need(not optional or re.fullmatch(rf'/git/ref/heads/{BRANCH}', path), 'optional only for claim ref')
        if path == '/user':
            data = decode(self.tools('github_get_profile', {}))
            value = data.get('id')
            need((type(value) is int and value > 0) or (isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value)), 'profile ID')
            return {'id': int(value), 'login': data.get('nickname')}
        data = decode(self.tools('github_fetch', {'url': ORIGIN + path}), optional=optional)
        if path.startswith('/contents/'):
            need(isinstance(data, dict) and data.get('type') == 'file' and data.get('encoding') == 'base64'
                 and type(data.get('size')) is int and 0 < data['size'] <= 4096, 'small metadata file required')
            raw = data.get('content')
            need(isinstance(raw, str) and len(raw) <= 8192, 'bounded encoded metadata')
            decoded = base64.b64decode(raw.replace('\n', ''), validate=True)
            need(len(decoded) == data['size'] and len(decoded) <= 4096, 'metadata size')
            return {'content_json': json.loads(decoded.decode('utf-8'))}
        return data

    def __call__(self, method, path, data=None, optional=False):
        try:
            if method == 'GET':
                need(data is None, 'GET body forbidden')
                return self.get(path, optional=optional)
            need(not optional and isinstance(data, dict) and len(json.dumps(data)) <= 8192, 'bounded mutation body')
            self.require_claim_binding()
            args = {'repository_full_name': REPO}
            if method == 'POST' and path == '/git/refs':
                need(set(data) == {'ref', 'sha'} and re.fullmatch(rf'refs/heads/{BRANCH}', data['ref']), 'claim ref only')
                protocol.sha(data['sha'])
                # This separate trusted port MUST issue exactly POST git/refs,
                # no retries/upserts. Its authenticated HTTP response is required.
                result = self.atomic_create_ref(REPO, dict(data))
                need(isinstance(result, dict) and type(result.get('http_status')) is int, 'atomic HTTP result required')
                status, body = result['http_status'], result.get('body')
                if status == 422 and isinstance(body, dict) and body.get('message') == 'Reference already exists':
                    raise protocol.Conflict('atomic ref already exists')
                need(status == 201 and isinstance(body, dict) and body.get('ref') == data['ref']
                     and body.get('object', {}).get('sha') == data['sha'], 'atomic creation ambiguous')
                return body
            if method == 'POST' and path == '/git/trees':
                need(set(data) == {'base_tree', 'tree'} and isinstance(data['tree'], list) and len(data['tree']) == 1, 'one receipt tree')
                protocol.sha(data['base_tree'])
                entry = data['tree'][0]
                need(isinstance(entry, dict) and set(entry) == {'path', 'mode', 'type', 'content'}
                     and re.fullmatch(RECEIPT, entry['path']) and entry['mode'] == '100644'
                     and entry['type'] == 'blob' and isinstance(entry['content'], str) and len(entry['content']) <= 4096, 'receipt blob only')
                args.update(base_tree_sha=data['base_tree'], tree_elements=data['tree'])
                return decode(self.tools('github_create_tree', args))
            if method == 'POST' and path == '/git/commits':
                need(set(data) == {'message', 'tree', 'parents'} and data['message'] == 'chore: reserve bounded GAM repair attempt'
                     and isinstance(data['parents'], list) and len(data['parents']) == 1, 'receipt commit only')
                args.update(message=data['message'], tree_sha=protocol.sha(data['tree']), parent_sha=protocol.sha(data['parents'][0]))
                return decode(self.tools('github_create_commit', args))
            if method == 'POST' and path == '/pulls':
                need(set(data) == {'head', 'base', 'draft', 'title', 'body'} and re.fullmatch(BRANCH, data['head'])
                     and data['base'] == 'district-main' and data['draft'] is True
                     and re.fullmatch(rf'\[GAM bump repair attempt [12]\] {HEX}', data['title'])
                     and isinstance(data['body'], str), 'separate draft repair PR only')
                args.update(data)
                result = decode(self.tools('github_create_pull_request', args))
                need(type(result.get('number')) is int and result['number'] > 0, 'created PR number')
                return self.get('/pulls/' + str(result['number']))
            match = re.fullmatch(r'/issues/([1-9][0-9]*)/comments', path)
            if method == 'POST' and match:
                need(set(data) == {'body'} and isinstance(data['body'], str)
                     and data['body'].startswith(protocol.TERMINAL_PREFIX) and len(data['body']) <= 4096, 'terminal comment only')
                result = decode(self.tools('github_add_comment_to_issue', {'repo_full_name': REPO,
                    'pr_number': int(match[1]), 'comment': data['body']}))
                identifier = result.get('id')
                need(type(identifier) is int and identifier > 0, 'actual comment ID required')
                return self.get('/issues/comments/' + str(identifier))
            match = re.fullmatch(r'/pulls/([1-9][0-9]*)', path)
            if method == 'PATCH' and match:
                need(data == {'state': 'closed'}, 'close only; never reopen/retarget/merge')
                decode(self.tools('github_update_pull_request', {'repository_full_name': REPO,
                    'pr_number': int(match[1]), 'state': 'closed'}))
                return self.get(path)
            raise protocol.Hold('mutation endpoint denied')
        except (protocol.Hold, protocol.Conflict):
            raise
        except Exception:
            raise protocol.Hold('native operation ambiguous; no retry/fallback') from None


@dataclass(frozen=True)
class TaskStatus:
    handle: str
    state: str
    active_descendants: int
    conclusion: str | None = None


class Coordinator:
    """Host owns lifecycle, tracks every diagnosis/review/test before launch.

    task_port.prepare(stage, ticket, candidate_sha) returns a fresh host-owned handle BEFORE
    task_port.start(handle). status/stop must be platform-authoritative and
    include test subprocesses. Model statements or return text are not status.
    A stopped TaskStatus is a trusted host assertion, not an OS attestation.
    """
    def __init__(self, api, task_port=None, *, activated=False):
        self.api, self.port, self.activated = api, task_port, activated
        self.ticket = None
        self.request = None
        self.candidate_sha = None
        self.handles = {}
        self.sealed = False
        self.launch_ambiguous = False
        self.lock = threading.RLock()

    def take(self, marker, nonce):
        with self.lock:
            need(self.activated is True and self.port is not None and self.ticket is None
                 and not self.sealed, 'native coordinator disabled or already used')
            self.api.require_claim_binding()
            self.ticket = protocol.claim(marker, nonce, api=self.api)
            self.request = protocol.load_request(self.api, marker)
            return self.ticket

    def launch(self, stage, *, candidate_sha=None):
        with self.lock:
            need(self.ticket is not None and not self.sealed and not self.launch_ambiguous
                 and stage in ('diagnosis', 'tests', 'review') and stage not in self.handles, 'one fixed task per stage')
            if stage != 'diagnosis':
                need('diagnosis' in self.handles, 'diagnosis first')
                self.require_success('diagnosis')
                if stage == 'review':
                    need('tests' in self.handles, 'successful exact-candidate tests before review')
                    self.require_success('tests')
                candidate_sha = protocol.sha(candidate_sha)
                need(self.candidate_sha in (None, candidate_sha), 'tests/review must use same exact candidate')
                pull = self.api('GET', f'/pulls/{self.ticket.repair_pr}')
                need(pull.get('state') == 'open' and pull.get('draft') is True
                     and pull.get('head', {}).get('sha') == candidate_sha
                     and pull['head'].get('ref') == self.ticket.branch
                     and protocol.repository(pull['head'].get('repo'))
                     and protocol.repository(pull.get('base', {}).get('repo'))
                     and pull['base'].get('ref') == 'district-main'
                     and pull.get('user', {}).get('id') == self.ticket.actor_id
                     and self.api('GET', '/git/ref/heads/' + self.ticket.branch)['object']['sha'] == candidate_sha,
                     'actual exact candidate PR/ref')
                protocol.candidate_paths([f.get('filename') for f in self.api('GET', f'/pulls/{self.ticket.repair_pr}/files?per_page=100')])
                compare = self.api('GET', f'/compare/{self.ticket.receipt_sha}...{candidate_sha}')
                need(compare.get('status') == 'ahead' and compare.get('behind_by') == 0, 'candidate must descend from receipt')
                self.candidate_sha = candidate_sha
            else:
                need(candidate_sha is None, 'diagnosis starts from claimed base')
                ticket, request = self.ticket, self.request
                pull = self.api('GET', f'/pulls/{ticket.repair_pr}')
                need(pull.get('state') == 'open' and pull.get('draft') is True
                     and pull.get('head', {}).get('sha') == ticket.receipt_sha
                     and pull['head'].get('ref') == ticket.branch
                     and protocol.repository(pull['head'].get('repo'))
                     and protocol.repository(pull.get('base', {}).get('repo'))
                     and pull['base'].get('ref') == 'district-main'
                     and pull.get('user', {}).get('id') == ticket.actor_id
                     and self.api('GET', '/git/ref/heads/' + ticket.branch)['object']['sha'] == ticket.receipt_sha,
                     'claimed repair PR/ref changed before diagnosis')
                path = f'.github/repair-attempts/{request.incident_sha}-{ticket.attempt}.json'
                expected = {'schema': 1, 'kind': 'repair-attempt', 'repository_id': protocol.REPO_ID,
                    'workflow_id': protocol.WORKFLOW_ID, 'incident_sha': request.incident_sha,
                    'marker_pr': request.marker_number, 'run_id': request.run_id, 'run_attempt': request.run_attempt,
                    'repair_attempt': ticket.attempt, 'nonce': ticket.nonce, 'actor_id': ticket.actor_id}
                need(self.api('GET', f'/contents/{path}?ref={ticket.receipt_sha}')['content_json'] == expected,
                     'claimed immutable receipt identity changed')
                compare = self.api('GET', f'/compare/{request.incident_sha}...{ticket.receipt_sha}')
                need(compare.get('status') == 'ahead' and compare.get('ahead_by') == 1
                     and compare.get('behind_by') == 0 and len(compare.get('files', [])) == 1
                     and compare['files'][0].get('filename') == path and compare['files'][0].get('status') == 'added',
                     'receipt-only claim history changed')
            need(protocol.load_request(self.api, self.request.marker_number) == self.request, 'source changed before native task')
            # Independent stages cannot race a still-running diagnosing worker.
            self.require_stopped()
            try:
                handle = self.port.prepare(stage, self.ticket, candidate_sha)
                need(isinstance(handle, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', handle)
                     and handle not in self.handles.values(), 'fresh host task handle')
                self.handles[stage] = handle
                self.port.start(handle)
                return handle
            except Exception:
                self.launch_ambiguous = True
                raise protocol.Hold('task launch ambiguous; hold claim and confirm stop') from None

    def require_success(self, stage):
        self.require_stopped()
        try:
            value = self.port.status(self.handles[stage])
        except Exception:
            raise protocol.Hold('successful stage status unavailable') from None
        need(isinstance(value, TaskStatus) and value.handle == self.handles[stage]
             and value.state == 'completed' and value.conclusion == 'success'
             and type(value.active_descendants) is int and value.active_descendants == 0,
             'platform-authoritative successful stage required')

    def require_stopped(self):
        for handle in self.handles.values():
            try:
                value = self.port.status(handle)
            except Exception:
                raise protocol.Hold('task status unknown; retain active claim') from None
            need(isinstance(value, TaskStatus) and value.handle == handle
                 and value.state in ('completed', 'failed', 'cancelled')
                 and type(value.active_descendants) is int and value.active_descendants == 0,
                 'task/reviewer/test descendants not confirmed stopped')

    def terminate(self, outcome):
        with self.lock:
            need(self.ticket is not None and not self.sealed and outcome in ('failed', 'rejected'), 'terminal coordinator state')
            # Seal BEFORE stop requests: no further launch, even if stop fails.
            self.sealed = True
            if self.launch_ambiguous and not self.handles:
                raise protocol.Hold('unregistered launch ambiguity; no terminal record')
            try:
                for handle in self.handles.values():
                    value = self.port.status(handle)
                    if not (isinstance(value, TaskStatus) and value.handle == handle
                            and value.state in ('completed', 'failed', 'cancelled')
                            and type(value.active_descendants) is int and value.active_descendants == 0):
                        self.port.stop(handle)
                self.require_stopped()
                # Only trusted coordinator can provide this protocol assertion.
                return protocol.finish(self.ticket, outcome, task_stopped=True, api=self.api)
            except protocol.Hold:
                raise
            except Exception:
                raise protocol.Hold('termination ambiguous; no retry or next attempt') from None
