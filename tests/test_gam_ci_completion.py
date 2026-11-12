from copy import deepcopy

import pytest

from scripts import reconcile_gam_ci as module
from tests.test_reconcile_gam_ci import GitHub, PR, RUN, HEAD

REPO = {'id': module.REPOSITORY_ID, 'full_name': module.REPOSITORY}
SOURCE = RUN | {'workflow_id': module.CI_WORKFLOW_ID, 'name': 'CI', 'head_repository': REPO}
EVENT = {'action': 'completed', 'repository': REPO, 'workflow_run': SOURCE}
CONTEXT = dict(repository=module.REPOSITORY, repository_id=str(module.REPOSITORY_ID),
               ref='refs/heads/district-main', event_name='workflow_run')


class EventGitHub(GitHub):
    def __init__(self, *, source=None, pr=None, workflow=None, listed=None):
        current = deepcopy(PR if pr is None else pr)
        if pr is None:
            current['head']['repo'] = REPO
            current['base']['repo'] = REPO
        super().__init__(run=SOURCE if source is None else source, current=current)
        self.workflow = workflow or dict(id=module.CI_WORKFLOW_ID, path='.github/workflows/ci.yml', name='CI', state='active')
        self.listed = listed
        self.reads = []

    def json(self, args, payload=None):
        path = args[-1]
        self.reads.append(path)
        if path.endswith('/ci.yml'):
            return self.workflow
        if f'/workflows/{module.CI_WORKFLOW_ID}/runs?' in path:
            return [{'workflow_runs': [self.source] if self.listed is None else self.listed}]
        return super().json(args, payload)


def invoke(gh, payload=None, **context):
    return module.ci_completion(gh, EVENT if payload is None else payload, **(CONTEXT | context))


@pytest.mark.parametrize('change', [
    {'repository': 'attacker/fork'}, {'repository_id': '1'},
    {'ref': 'refs/heads/automation/gam-v7.52.00-123456789abc'},
    {'event_name': 'pull_request'}, {'event_name': 'workflow_dispatch'},
])
def test_wrong_privileged_context_does_not_read_or_write(change):
    gh = EventGitHub()
    assert invoke(gh, **change).startswith('ignored')
    assert gh.reads == [] and gh.writes == []


@pytest.mark.parametrize('change', [
    {'head_repository': {'id': 1, 'full_name': module.REPOSITORY}},
    {'head_repository': {'id': module.REPOSITORY_ID, 'full_name': 'attacker/fork'}},
    {'head_branch': 'fix/not-automation'}, {'head_sha': '../untrusted'},
    {'workflow_id': 123}, {'path': '.github/workflows/untrusted.yml'}, {'name': 'Other'},
    {'event': 'workflow_dispatch'}, {'event': 'push'}, {'status': 'in_progress'},
    {'run_attempt': 0}, {'run_attempt': True}, {'id': '../other'},
    {'conclusion': 'action_required'},
])
def test_untrusted_or_unrelated_source_ignored_before_reads(change):
    gh = EventGitHub()
    assert invoke(gh, EVENT | {'workflow_run': SOURCE | change}).startswith('ignored')
    assert gh.reads == [] and gh.writes == []


@pytest.mark.parametrize('change', [{'action': 'requested'}, {'repository': {'id': 1, 'full_name': module.REPOSITORY}}])
def test_wrong_event_identity_has_no_side_effects(change):
    gh = EventGitHub()
    assert invoke(gh, EVENT | change).startswith('ignored')
    assert gh.reads == [] and gh.writes == []


@pytest.mark.parametrize('change', [
    {'run_attempt': 2}, {'status': 'queued', 'conclusion': None},
    {'head_sha': 'b' * 40}, {'head_branch': 'automation/gam-v7.53.00-123456789abc'},
    {'event': 'push'}, {'head_repository': {'id': 1, 'full_name': module.REPOSITORY}},
])
def test_stale_or_changed_authoritative_run_prevents_writes(change):
    gh = EventGitHub(source=SOURCE | change)
    assert invoke(gh) == 'ignored stale CI completion'
    assert gh.writes == []


@pytest.mark.parametrize('change', [{'id': 123}, {'name': 'Not CI'}, {'state': 'disabled_manually'}, {'path': 'other.yml'}])
def test_authoritative_workflow_must_match(change):
    gh = EventGitHub()
    gh.workflow.update(change)
    assert invoke(gh) == 'ignored unexpected CI workflow'
    assert gh.writes == []


@pytest.mark.parametrize('location,key,value', [
    ('head', 'sha', 'b' * 40), ('head', 'repo', {'id': 1, 'full_name': module.REPOSITORY}),
    ('base', 'repo', {'id': 1, 'full_name': module.REPOSITORY}), ('base', 'ref', 'other'),
])
def test_current_pr_identity_and_head_are_authoritative(location, key, value):
    gh = EventGitHub()
    gh.current[location][key] = value
    assert invoke(gh) == 'ignored changed or ineligible pull request'
    assert gh.writes == []


def test_event_pull_list_is_not_trusted_and_valid_event_is_scoped(monkeypatch):
    calls = []
    monkeypatch.setattr(module, 'reconcile', lambda gh, pr, **kwargs: calls.append((pr, kwargs)) or 'scoped')
    gh = EventGitHub()
    payload = EVENT | {'workflow_run': SOURCE | {'pull_requests': [{'number': 999}]}}
    assert invoke(gh, payload) == 'scoped'
    assert calls[0][0]['number'] == 42
    assert calls[0][1]['expected_run'] == gh.source
    assert all('/pulls?' not in path and '/git/ref/' not in path for path in gh.reads)
    assert gh.writes == []


def test_newer_ci_run_rejects_stale_event_before_writes():
    gh = EventGitHub(listed=[SOURCE | {'id': 21}])
    assert invoke(gh) == 'completion superseded; recovery held'
    assert gh.writes == []


def test_provider_capacity_event_uses_existing_bounded_retry(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: True)
    gh = EventGitHub()
    assert invoke(gh) == 'capacity retry requested'
    assert gh.writes == [['run', 'rerun', '20', '--failed', '--repo', module.REPOSITORY]]


def test_exhausted_capacity_event_reports_without_retry(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: True)
    source = SOURCE | {'run_attempt': 3}
    gh = EventGitHub(source=source)
    assert invoke(gh, EVENT | {'workflow_run': source}) == 'capacity recovery exhausted'
    assert len(gh.writes) == 1 and gh.writes[0][:2] == ['issue', 'create']


def test_real_failure_event_reports_without_retry(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: False)
    gh = EventGitHub()
    assert invoke(gh) == 'failure reported'
    assert len(gh.writes) == 1 and gh.writes[0][:2] == ['issue', 'create']


def test_green_event_calls_exact_head_protected_merge_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(module, 'dispatch_test_and_merge', lambda **kwargs: calls.append(kwargs) or {'merge_sha': 'c' * 40})
    source = SOURCE | {'conclusion': 'success'}
    gh = EventGitHub(source=source)
    assert invoke(gh, EVENT | {'workflow_run': source}) == 'merged; exact-SHA release validation dispatched'
    assert calls[0]['head_sha'] == HEAD
    assert gh.writes[0][-1] == 'sha=' + 'c' * 40


def test_duplicate_completion_after_retry_does_not_retry_again(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: True)
    gh = EventGitHub()
    assert invoke(gh) == 'capacity retry requested'
    gh.source.update(run_attempt=2, status='queued', conclusion=None)
    assert invoke(gh) == 'ignored stale CI completion'
    assert len(gh.writes) == 1


def test_main_event_path_never_runs_broad_recovery(monkeypatch, tmp_path):
    import json
    import sys
    event_file = tmp_path / 'completion.json'
    event_file.write_text(json.dumps(EVENT))
    monkeypatch.setattr(sys, 'argv', ['reconcile_gam_ci.py'])
    for name, value in {'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_EVENT_PATH': str(event_file),
                        'GITHUB_REPOSITORY': module.REPOSITORY, 'GITHUB_REPOSITORY_ID': str(module.REPOSITORY_ID),
                        'GITHUB_REF': 'refs/heads/district-main'}.items():
        monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(module, 'ci_completion', lambda gh, payload, **context: calls.append((payload, context)) or 'scoped')
    def forbidden(*args, **kwargs):
        pytest.fail('workflow_run must not scan unrelated PRs or resume unrelated release dispatch')
    monkeypatch.setattr(module, 'pages', forbidden)
    monkeypatch.setattr(module, 'resume_release_validation', forbidden)
    assert module.main() == 0
    assert calls[0][0] == EVENT
    assert calls[0][1] == CONTEXT


def test_oversized_event_stops_before_any_handler_or_write(monkeypatch, tmp_path):
    import sys
    event_file = tmp_path / 'completion.json'
    event_file.write_bytes(b' ' * 1048577)
    monkeypatch.setattr(sys, 'argv', ['reconcile_gam_ci.py'])
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_run')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event_file))
    monkeypatch.setattr(module, 'ci_completion', lambda *args, **kwargs: pytest.fail('oversized event handled'))
    with pytest.raises(RuntimeError, match='size limit'):
        module.main()
