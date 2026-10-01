"""Optional trusted-host atomic port using existing gh sign-in; default disabled.

This is an explicit host binding, never a fallback from the native branch tool.
No credential/config files are opened, tokens persisted, service or account added.
No repository code/model can receive this port. Never execute the Scoop shim:
the host supplies the already-installed, verified absolute native gh.exe path.
"""
import json
import os
from pathlib import Path
import re
import subprocess
from scripts.gam_repair_claim import Hold, REPO, REPO_ID, need, sha

OWNER_ID = 276981287
REF = re.compile(r'refs/heads/automation/gam-repair-[0-9a-f]{40}-attempt-[12]')


def environment(source):
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'HOMEDRIVE', 'HOMEPATH',
               'GH_TOKEN', 'GITHUB_TOKEN'}
    result = {key.upper(): source[key] for key in source if key.upper() in allowed}
    # This TRUSTED HOST ONLY port inherits the host's already-authorized GitHub
    # credential variables through normal process environment; no extraction,
    # new token, storage or model handoff. Worker clean_environment drops both.
    # gh api fails on missing auth; never login or add a credential.
    result.update(GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1', GH_NO_EXTENSION_UPDATE_NOTIFIER='1')
    return result


def http_result(output, returncode):
    need(isinstance(output, str) and len(output.encode('utf-8')) <= 1048576, 'bounded gh HTTP response')
    header, separator, body = output.replace('\r\n', '\n').partition('\n\n')
    match = re.match(r'HTTP/(?:1\.[01]|2(?:\.0)?) ([1-5][0-9]{2})(?: [^\n]*)?\n', header + '\n')
    need(bool(separator) and match is not None, 'actual HTTP status required')
    status = int(match[1])
    need((returncode == 0 and status < 400) or (returncode != 0 and status >= 400), 'gh exit/status disagreement')
    value = json.loads(body)
    need(isinstance(value, dict), 'HTTP JSON object required')
    return {'http_status': status, 'body': value}


class GhAtomicRef:
    def __init__(self, executable, *, activated=False, runner=subprocess.run):
        supplied = Path(executable)
        need(supplied.is_absolute() and supplied.name.lower() == 'gh.exe', 'absolute native gh.exe required')
        need(supplied.is_file(), 'existing native gh executable required')
        self.executable = str(supplied.resolve())
        self.activated, self.runner, self.verified = activated, runner, False

    def request(self, method, path, payload=None):
        allowed = (method == 'GET' and path in ('/user', '/repos/' + REPO) and payload is None
                   or method == 'POST' and path == '/repos/' + REPO + '/git/refs' and isinstance(payload, dict))
        need(allowed, 'fixed atomic-port endpoint')
        if method == 'POST':
            self.require_ready()
        argv = [self.executable, 'api', '--hostname', 'github.com', '--method', method, '--include', path]
        text = None
        if payload is not None:
            need(set(payload) == {'ref', 'sha'} and isinstance(payload['ref'], str) and REF.fullmatch(payload['ref']), 'fixed bounded attempt ref')
            sha(payload['sha'])
            text = json.dumps(payload, separators=(',', ':'))
            argv.extend(('--input', '-'))
        try:
            result = self.runner(argv, input=text, env=environment(os.environ), capture_output=True,
                                 text=True, timeout=15, shell=False)
            # Raw stderr may contain account diagnostics: never expose or persist.
            return http_result(result.stdout, result.returncode)
        except Hold:
            raise
        except Exception:
            raise Hold('gh atomic operation ambiguous; no retry, auth or permission fallback') from None

    def verify_read_only(self):
        self.verified = False
        actor = self.request('GET', '/user')
        repo = self.request('GET', '/repos/' + REPO)
        need(actor['http_status'] == repo['http_status'] == 200
             and actor['body'].get('id') == OWNER_ID and actor['body'].get('login') == 'Sykezzz'
             and repo['body'].get('id') == REPO_ID and repo['body'].get('full_name') == REPO
             and repo['body'].get('default_branch') == 'district-main'
             and repo['body'].get('owner', {}).get('id') == OWNER_ID, 'existing gh identity/repository mismatch')
        self.verified = True
        return {'actor_id': OWNER_ID, 'repository_id': REPO_ID, 'writes_enabled': self.activated is True}

    def require_ready(self):
        need(self.activated is True and self.verified is True, 'atomic gh write port disabled or unverified')

    def __call__(self, repository, payload):
        self.require_ready()
        need(repository == REPO, 'fixed repository only')
        # Revalidate existing sign-in immediately before the one create-only POST.
        self.verify_read_only()
        result = self.request('POST', '/repos/' + REPO + '/git/refs', payload)
        status, body = result['http_status'], result['body']
        need(status == 201 and body.get('ref') == payload['ref'] and body.get('object', {}).get('sha') == payload['sha']
             or status == 422 and body.get('message') == 'Reference already exists', 'atomic HTTP result ambiguous')
        return result
