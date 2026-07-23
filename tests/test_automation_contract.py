from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_upstream_sync_is_non_forcing_and_targets_district_branch():
    workflow = _workflow("upstream-sync.yml")
    assert "gh repo sync" in workflow
    assert "--source goetchstone/gamgui" in workflow
    assert "--force" not in workflow
    assert "--base district-main" in workflow
    assert "gh workflow run ci.yml" in workflow
    assert "gh workflow run post-merge-validation.yml" in workflow
    assert "mergeCommit.oid" in workflow
    assert "gh pr merge" in workflow and "--auto" in workflow
    assert "Upstream sync blocked" in workflow
    assert "Report blocked upstream integration" in workflow
    assert "if: failure()" in workflow
    assert "Nothing was force-synced or installed." in workflow


def test_gam_update_refreshes_every_pinned_contract_before_auto_merge():
    workflow = _workflow("gam-update.yml")
    assert "scripts/bump_gam.py" in workflow
    assert "scripts/gam_checksums.txt" in workflow
    assert "command_catalog.json" in workflow
    assert "tests/fixtures/mock_gam.sh" in workflow
    assert "gh workflow run ci.yml" in workflow
    assert "gh workflow run post-merge-validation.yml" in workflow
    assert "mergeCommit.oid" in workflow
    assert "gh pr merge" in workflow and "--auto" in workflow
    assert "TAG: ${{ steps.release.outputs.tag }}" in workflow
    assert 'scripts/bump_gam.py --tag "$TAG"' in workflow
    assert 'scripts/bump_gam.py --tag "${{ steps.release.outputs.tag }}"' not in workflow


def test_post_merge_validation_is_exact_sha_and_update_ready_is_last():
    workflow = _workflow("post-merge-validation.yml")
    assert "test \"$(git rev-parse HEAD)\" = \"$EXPECTED_SHA\"" in workflow
    assert "pinned and latest GAM contracts" in workflow
    assert "updater, bundle, and rollback contracts" in workflow
    assert "GamGUI --self-test --json" in workflow
    assert "name: update-ready" in workflow
    assert "needs: [verify-sha, test, gam-contracts, updater-and-build]" in workflow


def test_ci_dispatches_post_merge_validation_only_for_district_main_push():
    workflow = _workflow("ci.yml")
    assert "github.ref == 'refs/heads/district-main'" in workflow
    assert "post-merge-validation.yml" in workflow
    assert 'branches: [main, district-main]' in workflow
    assert 'scripts/bump_gam.py --tag "$LATEST"' in workflow


def test_post_merge_latest_contract_is_pinned_before_download():
    workflow = _workflow("post-merge-validation.yml")
    assert 'scripts/bump_gam.py --tag "$LATEST"' in workflow
    assert "./scripts/fetch_gam.sh --tag latest" not in workflow


def test_repository_activation_script_enables_only_required_maintenance_settings():
    script = (ROOT / "scripts" / "configure_github_repo.sh").read_text(encoding="utf-8")
    assert "--enable-issues=true" in script
    assert "--enable-auto-merge=true" in script
    assert "--default-branch=district-main" in script
    assert "can_approve_pull_request_reviews=true" in script
    assert '"allow_force_pushes": false' in script
    assert '"allow_deletions": false' in script
    assert "gh repo sync" not in script


def test_workflow_actions_are_immutable_and_dependencies_are_locked():
    workflows = [
        _workflow(name)
        for name in (
            "ci.yml",
            "gam-update.yml",
            "post-merge-validation.yml",
            "upstream-sync.yml",
        )
    ]
    uses = [
        value
        for workflow in workflows
        for value in re.findall(r"^\s*-\s+uses:\s+(\S+)", workflow, re.MULTILINE)
    ]
    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", value) for value in uses)
    assert all("pip install" not in workflow for workflow in workflows)
    assert all(
        "uv sync --frozen" in workflow
        for workflow in workflows
        if "setup-python@" in workflow
    )

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    build_script = (ROOT / "scripts" / "build_app.sh").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert '"pyinstaller==6.20.0"' in pyproject
    assert "UV_VERSION := 0.11.7" in makefile
    assert "$(UV) sync --frozen" in makefile
    assert "pip install" not in build_script
    assert 'name = "pyinstaller"' in lock and 'version = "6.20.0"' in lock
