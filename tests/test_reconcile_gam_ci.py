from copy import deepcopy
import base64

import pytest

from scripts import reconcile_gam_ci as module

HEAD = 'a' * 40
REPO = {'full_name': module.REPOSITORY}
PR = dict(number=42, state='open', draft=False, title='chore: bump GAM7 to v7.52.00',
          head=dict(ref='automation/gam-v7.52.00-123456789abc', sha=HEAD, repo=REPO),
          base=dict(ref='district-main', repo=REPO))
RUN = dict(id=20, head_sha=HEAD, event='pull_request', head_branch=PR['head']['ref'],
           workflow_id=30, path='.github/workflows/ci.yml', head_repository=REPO,
           pull_requests=[{'number': 42}], status='completed', conclusion='failure', run_attempt=1)


class GitHub:
    def __init__(self, run=None, current=None, existing=None):
        self.source = deepcopy(RUN if run is None else run)
        self.current = deepcopy(PR if current is None else current)
        self.existing = existing or []
        self.writes = []

    def json(self, args, payload=None):
        path = args[-1]
        if '/contents/' in path:
            return {'encoding': 'base64', 'content': base64.b64encode(b'v7.51.00\n').decode()}
        if path.endswith('/ci.yml'):
            return {'id': 30, 'path': '.github/workflows/ci.yml', 'state': 'active'}
        if '/workflows/30/runs?' in path:
            return [{'workflow_runs': []}, {'workflow_runs': [self.source]}]
        if path.endswith('/pulls/42'):
            return self.current
        if path.endswith('/runs/20'):
            return self.source
        if '/issues?' in path:
            return [self.existing]
        raise AssertionError(args)

    def run(self, args):
        self.writes.append(args)


def test_retry_verified_capacity_only(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: True)
    gh = GitHub()
    assert module.reconcile(gh, PR) == 'capacity retry requested'
    assert gh.writes == [['run', 'rerun', '20', '--failed', '--repo', module.REPOSITORY]]


def test_retry_budget_exhaustion_reports_without_retry(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: True)
    gh = GitHub(RUN | {'run_attempt': 3})
    assert module.reconcile(gh, PR) == 'capacity recovery exhausted'
    assert len(gh.writes) == 1 and gh.writes[0][:2] == ['issue', 'create']
    assert 'not a demonstrated test failure' in gh.writes[0][-1]


def test_executed_or_unclassified_failure_reports_without_retry(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: False)
    gh = GitHub()
    assert module.reconcile(gh, PR) == 'failure reported'
    assert len(gh.writes) == 1 and gh.writes[0][:2] == ['issue', 'create']


def test_deduplicates_issue_including_closed_tombstone(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: False)
    gh = GitHub(existing=[{'title': f'GAM CI failure: PR #42 at {HEAD}'}])
    module.reconcile(gh, PR)
    assert gh.writes == []


@pytest.mark.parametrize('change', [{'event': 'workflow_dispatch'}, {'head_sha': 'b' * 40},
    {'head_repository': {'full_name': 'attacker/fork'}}, {'head_branch': 'other'},
    {'pull_requests': [{'number': 43}]}, {'path': 'other.yml'}, {'workflow_id': 31}])
def test_rejects_wrong_run_provenance(change):
    gh = GitHub(RUN | change)
    assert module.reconcile(gh, PR) == 'waiting for eligible CI'
    assert gh.writes == []


@pytest.mark.parametrize('change', [{'state': 'closed'}, {'draft': True}, {'title': 'unrelated'}])
def test_ineligible_pr_no_writes(change):
    gh = GitHub()
    assert module.reconcile(gh, PR | change) == 'ineligible'
    assert gh.writes == []


def test_head_race_prevents_retry(monkeypatch):
    monkeypatch.setattr(module, 'capacity_only', lambda *args: True)
    changed = deepcopy(PR)
    changed['head']['sha'] = 'b' * 40
    gh = GitHub(current=changed)
    with pytest.raises(RuntimeError, match='changed'):
        module.reconcile(gh, PR)
    assert gh.writes == []


def test_pending_run_has_no_writes():
    gh = GitHub(RUN | {'status': 'queued', 'conclusion': None})
    assert module.reconcile(gh, PR) == 'verification running'
    assert gh.writes == []


def test_only_green_exact_sha_uses_protected_helper_then_dispatch(monkeypatch):
    calls = []
    def merge(**kwargs):
        calls.append(kwargs)
        return {'merge_sha': 'c' * 40}
    monkeypatch.setattr(module, 'dispatch_test_and_merge', merge)
    gh = GitHub(RUN | {'conclusion': 'success'})
    assert module.reconcile(gh, PR) == 'merged; exact-SHA release validation dispatched'
    assert calls[0]['head_sha'] == HEAD
    assert calls[0]['defer_macos_capacity'] is True
    assert gh.writes == [['workflow', 'run', 'post-merge-validation.yml', '--repo',
                          module.REPOSITORY, '--ref', 'district-main', '-f', 'sha=' + 'c' * 40]]


def test_superseded_pin_is_not_merged_or_retried():
    old = deepcopy(PR)
    old['title'] = 'chore: bump GAM7 to v7.51.00'
    gh = GitHub()
    assert module.reconcile(gh, old) == 'superseded by current pin'
    assert gh.writes == []


class ReleaseGitHub(GitHub):
    def __init__(self, *, runs=None, moved=False):
        super().__init__()
        self.runs = runs or []
        self.moved = moved
        self.ref_reads = 0

    def json(self, args, payload=None):
        path = args[-1]
        if '/git/ref/' in path:
            self.ref_reads += 1
            return {'object': {'sha': 'b' * 40 if self.moved and self.ref_reads > 1 else HEAD}}
        if '/pulls?state=closed' in path:
            return [[PR | {'state': 'closed', 'merged_at': 'now', 'merge_commit_sha': HEAD}]]
        if path.endswith('/post-merge-validation.yml'):
            return {'id': 31, 'path': '.github/workflows/post-merge-validation.yml', 'state': 'active'}
        if '/workflows/31/runs?' in path:
            return [{'workflow_runs': self.runs}]
        return super().json(args, payload)


def test_next_tick_recovers_merge_that_succeeded_before_dispatch_failed():
    gh = ReleaseGitHub()
    assert module.resume_release_validation(gh) == 'missing exact-SHA validation dispatched'
    assert gh.writes[0][-1] == 'sha=' + HEAD


def test_existing_validation_prevents_duplicate_dispatch_even_when_not_green():
    run = RUN | {'workflow_id': 31, 'path': '.github/workflows/post-merge-validation.yml',
                 'head_branch': 'district-main', 'event': 'workflow_dispatch'}
    gh = ReleaseGitHub(runs=[run])
    assert module.resume_release_validation(gh) == 'validation already dispatched'
    assert gh.writes == []


def test_changed_base_prevents_stale_dispatch():
    gh = ReleaseGitHub(moved=True)
    assert module.resume_release_validation(gh) == 'base changed; dispatch held'
    assert gh.writes == []


def test_failed_postmerge_dispatch_is_not_repeated_in_same_call(monkeypatch):
    monkeypatch.setattr(module, 'dispatch_test_and_merge', lambda **kwargs: {'merge_sha': 'c' * 40})
    gh = GitHub(RUN | {'conclusion': 'success'})
    def fail(args):
        gh.writes.append(args)
        raise RuntimeError('dispatch transport error')
    gh.run = fail
    with pytest.raises(RuntimeError, match='dispatch transport'):
        module.reconcile(gh, PR)
    assert len(gh.writes) == 1
