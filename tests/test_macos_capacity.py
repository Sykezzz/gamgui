import pytest

from scripts.macos_capacity import ALLOCATION_ERROR, capacity_only, pages
from scripts.merge_tested_automation_pr import _defer_capacity

HEAD = 'a' * 40


def job(name='gam-compat (pinned binary)', **changes):
    value = dict(id=1, name=name, run_id=20, head_sha=HEAD, status='completed',
                 conclusion='cancelled', steps=[], runner_id=0, labels=['macos-latest'])
    return value | changes


class GitHub:
    def __init__(self, jobs=None, annotations=None):
        self.jobs = [job()] if jobs is None else jobs
        self.annotations = [[{'annotation_level': 'failure', 'message': ALLOCATION_ERROR}]] if annotations is None else annotations
        self.calls = []

    def json(self, args):
        self.calls.append(args)
        assert args[:3] == ['api', '--paginate', '--slurp']
        if '/jobs?' in args[-1]:
            return [{'jobs': self.jobs[:1]}, {'jobs': self.jobs[1:]}]
        return self.annotations


def test_provider_allocation_only_and_pagination():
    gh = GitHub([job(), job(id=2, name='test (macos-latest, py3.12)')])
    assert capacity_only(gh, 'owner/repo', 20, HEAD)
    assert len(gh.calls) == 3
    assert all('--paginate' in call for call in gh.calls)


@pytest.mark.parametrize('changes', [
    {'steps': [{'name': 'Set up job', 'conclusion': 'success'}]},
    {'runner_id': 42}, {'labels': ['windows-latest']}, {'labels': ['self-hosted', 'macos-latest']},
    {'head_sha': 'b' * 40}, {'run_id': 21}, {'status': 'queued'},
    {'conclusion': 'timed_out'}, {'name': 'unknown job'}, {'id': '1'},
])
def test_unknown_executed_wrong_identity_or_runner_is_not_capacity(changes):
    assert not capacity_only(GitHub([job(**changes)]), 'owner/repo', 20, HEAD)


@pytest.mark.parametrize('annotations', [[], [[]], [[{'annotation_level': 'notice', 'message': ALLOCATION_ERROR}]],
    [[{'annotation_level': 'failure', 'message': 'Some infrastructure error'}]],
    [[{'annotation_level': 'notice', 'message': 'macOS arm64 capacity constraints'}]],
])
def test_explicit_provider_error_is_required(annotations):
    assert not capacity_only(GitHub(annotations=annotations), 'owner/repo', 20, HEAD)


def test_annotation_on_later_page_is_found():
    gh = GitHub(annotations=[[], [{'annotation_level': 'failure', 'message': ALLOCATION_ERROR}]])
    assert capacity_only(gh, 'owner/repo', 20, HEAD)


def test_real_failure_elsewhere_blocks_capacity_deferral():
    gh = GitHub([job(), job(id=2, name='Windows test (py3.12)', steps=[{}], conclusion='failure')])
    assert not capacity_only(gh, 'owner/repo', 20, HEAD)


def test_only_known_dependent_profile_assertion_may_fail():
    aggregate = job('macOS application build smoke', id=2, runner_id=3,
                    labels=['ubuntu-latest'], conclusion='failure',
                    steps=[{'name': 'Set up job', 'conclusion': 'success'},
                           {'name': 'Require every application profile build', 'conclusion': 'failure'}])
    source = job('macOS application build smoke (core)')
    assert capacity_only(GitHub([source, aggregate]), 'owner/repo', 20, HEAD)
    aggregate['steps'][0]['conclusion'] = 'failure'
    assert not capacity_only(GitHub([source, aggregate]), 'owner/repo', 20, HEAD)
    aggregate['steps'][0]['conclusion'] = 'success'
    assert not capacity_only(GitHub([job(), aggregate]), 'owner/repo', 20, HEAD)


@pytest.mark.parametrize('jobs', [[], [job(), job()], [job(conclusion='success')]])
def test_no_capacity_evidence_or_duplicate_jobs_rejected(jobs):
    assert not capacity_only(GitHub(jobs), 'owner/repo', 20, HEAD)


def test_deferral_does_not_claim_merge_and_unknown_failure_raises():
    result = _defer_capacity(GitHub(), 'owner/repo', {'databaseId': 20, 'url': 'url'}, HEAD)
    assert result == {'deferred': 'true', 'merge_sha': '', 'ci_run_url': 'url'}
    with pytest.raises(RuntimeError, match='without verified'):
        _defer_capacity(GitHub([job(steps=[{}])]), 'owner/repo', {'databaseId': 20}, HEAD)


def test_historical_queued_aggregate_is_dependent_not_executed_failure():
    source = job('macOS application build smoke (core)')
    aggregate = job('macOS application build smoke', id=2, status='queued', conclusion=None,
                    labels=['ubuntu-latest'])
    assert capacity_only(GitHub([source, aggregate]), 'owner/repo', 20, HEAD)
    aggregate['steps'] = [{'name': 'started'}]
    assert not capacity_only(GitHub([source, aggregate]), 'owner/repo', 20, HEAD)


def test_pending_is_not_inferred_for_unverified_failed_job():
    from scripts.macos_capacity import verification_pending
    queued = job(status='queued', conclusion=None)
    assert verification_pending(GitHub([queued]), 'owner/repo', 20, HEAD)
    assert not verification_pending(GitHub([queued, job(id=2, steps=[{}])]), 'owner/repo', 20, HEAD)
    assert not verification_pending(GitHub([]), 'owner/repo', 20, HEAD)


def test_mixed_running_and_executed_failure_is_not_pending_or_capacity():
    from scripts.macos_capacity import verification_pending
    mixed = GitHub([job(status='in_progress', conclusion=None),
                    job(id=2, name='test (ubuntu-latest, py3.12)', conclusion='failure', steps=[{}])])
    assert not verification_pending(mixed, 'owner/repo', 20, HEAD)
    assert not capacity_only(mixed, 'owner/repo', 20, HEAD)


@pytest.mark.parametrize('status', ['queued', 'in_progress'])
def test_mixed_verified_capacity_and_pending_macos_defers_without_passing_ci(status):
    from scripts.macos_capacity import verification_pending
    mixed = [job(), job('test (macos-latest, py3.12)', id=2, status=status, conclusion=None)]
    assert verification_pending(GitHub(mixed), 'owner/repo', 20, HEAD)
    assert not capacity_only(GitHub(mixed), 'owner/repo', 20, HEAD)


@pytest.mark.parametrize('changes', [
    {'name': 'unknown job'}, {'labels': ['ubuntu-latest']},
    {'status': 'unknown'}, {'status': 'completed'},
    {'head_sha': 'b' * 40}, {'run_id': 21},
    {'steps': [{'conclusion': 'failure'}]},
    {'steps': [{'conclusion': 'cancelled'}]},
    {'steps': [{'conclusion': 'timed_out'}]},
])
def test_mixed_capacity_does_not_excuse_unknown_or_already_failed_pending_job(changes):
    from scripts.macos_capacity import verification_pending
    pending = job('test (macos-latest, py3.12)', id=2, status='in_progress', conclusion=None)
    assert not verification_pending(GitHub([job(), pending | changes]), 'owner/repo', 20, HEAD)


def test_mixed_pending_needs_provider_evidence_and_does_not_hide_executed_failure():
    from scripts.macos_capacity import verification_pending
    pending = job('test (macos-latest, py3.12)', id=2, status='queued', conclusion=None)
    assert not verification_pending(GitHub([job(), pending], annotations=[[]]), 'owner/repo', 20, HEAD)
    executed = job('test (macos-latest, py3.14)', id=3, conclusion='failure', steps=[{}], runner_id=42)
    assert not verification_pending(GitHub([job(), pending, executed]), 'owner/repo', 20, HEAD)


def test_mixed_pending_build_and_queued_aggregate_can_wait_for_verification():
    from scripts.macos_capacity import verification_pending
    pending = job('macOS application build smoke (core)', id=2, status='queued', conclusion=None)
    aggregate = job('macOS application build smoke', id=3, status='queued', conclusion=None,
                    labels=['ubuntu-latest'])
    assert verification_pending(GitHub([job(), pending, aggregate]), 'owner/repo', 20, HEAD)
    assert not capacity_only(GitHub([job(), pending, aggregate]), 'owner/repo', 20, HEAD)


def test_pending_job_with_failed_step_is_not_excused_even_without_capacity_cancellation():
    from scripts.macos_capacity import verification_pending
    running = job(status='in_progress', conclusion=None, steps=[{'conclusion': 'failure'}])
    assert not verification_pending(GitHub([running]), 'owner/repo', 20, HEAD)
