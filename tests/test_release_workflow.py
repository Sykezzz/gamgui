from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PACKAGER = ROOT / "scripts" / "package_official_profile.sh"
VERIFIER = ROOT / "scripts" / "verify_official_artifact.sh"
MANIFEST = ROOT / "gamgui" / "core" / "release_manifest.py"


def test_release_workflow_is_manual_and_tag_driven_but_never_overwrites():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert 'tags:\n      - "v*"' in workflow
    assert "publish:" in workflow and "default: false" in workflow
    assert "git rev-parse HEAD" in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert 'test "$RELEASE_TAG" = "v$VERSION"' in workflow
    assert "Exact source SHA has no successful update-ready check." in workflow
    assert "Refusing to overwrite an existing official release." in workflow
    assert "gh release create" in workflow
    assert "--verify-tag" in workflow
    assert 'gh api "repos/$GITHUB_REPOSITORY/commits/$RELEASE_TAG" --jq .sha' in workflow
    assert "--clobber" not in workflow


def test_release_workflow_fails_closed_on_credentials_and_publishes_last():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    required = (
        "APPLE_DEVELOPER_ID_IDENTITY",
        "APPLE_DEVELOPER_ID_P12_BASE64",
        "APPLE_DEVELOPER_ID_P12_PASSWORD",
        "APPLE_RELEASE_KEYCHAIN_PASSWORD",
        "APPLE_NOTARY_KEY_ID",
        "APPLE_NOTARY_ISSUER_ID",
        "APPLE_NOTARY_PRIVATE_KEY_BASE64",
    )
    assert all(item in workflow for item in required)
    assert "Required official-release credential is missing" in workflow
    assert "security set-key-partition-list" in workflow
    assert "trap cleanup_on_exit EXIT" in workflow

    build_position = workflow.index("Build both unsigned exact-SHA profiles")
    sign_position = workflow.index("Sign and notarize with ephemeral protected credentials")
    manifest_position = workflow.index("Verify, self-test, and package both signed profiles")
    publish_position = workflow.index("Publish both profiles only after every gate passes")
    release_position = workflow.index("gh release create")
    assert build_position < sign_position < manifest_position < publish_position < release_position
    assert "if: ${{ env.PUBLISH_RELEASE == 'true' }}" in workflow


def test_apple_credentials_are_step_scoped_away_from_untrusted_project_code():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    signing_job = workflow.index("  sign-notarize-release:")
    secret_step = workflow.index(
        "      - name: Sign and notarize with ephemeral protected credentials"
    )
    post_secret_checkout = workflow.index(
        "      - uses: actions/checkout@", secret_step
    )
    build_job = workflow[:signing_job]
    signing_prefix = workflow[signing_job:secret_step]
    secret_boundary = workflow[secret_step:post_secret_checkout]

    assert "environment: official-release" not in build_job
    assert "secrets.APPLE_" not in build_job
    assert "secrets.APPLE_" not in signing_prefix
    assert "secrets.APPLE_" in secret_boundary
    assert "scripts/" not in secret_boundary
    assert "make " not in secret_boundary
    assert "pytest" not in secret_boundary
    assert "--self-test" not in secret_boundary
    assert "actions/checkout" not in secret_boundary
    assert "workflow YAML is repository-controlled" in workflow
    assert "trap cleanup_on_exit EXIT" in secret_boundary
    assert "security lock-keychain" in secret_boundary
    assert "security delete-keychain" in secret_boundary
    assert "security list-keychains -d user" in secret_boundary
    assert "cleanup_credentials\n          trap - EXIT" in secret_boundary
    assert secret_boundary.rindex("cleanup_credentials") < secret_boundary.rindex(
        "trap - EXIT"
    )
    assert "needs: build-and-test" in workflow


def test_release_builds_both_profiles_and_verifies_downloadable_archives():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "for PROFILE in core classroom-oneroster" in workflow
    assert "Build both unsigned exact-SHA profiles" in workflow
    assert "actions/upload-artifact@" in workflow
    assert "actions/download-artifact@" in workflow
    assert "scripts/package_official_profile.sh" not in workflow
    assert "scripts/release_manifest.py build" in workflow
    assert "scripts/release_manifest.py verify-assets" in workflow
    assert "scripts/verify_official_artifact.sh" in workflow
    assert '--expected-team-id "$EXPECTED_TEAM_ID"' in workflow
    assert "release-manifest.json.sha256" in workflow
    assert ".venv/bin/pytest -q" in workflow
    assert 'ditto -c -k --norsrc --keepParent "$APP" "$ARCHIVE"' in workflow
    assert (
        'ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"'
        not in workflow
    )

    assert not PACKAGER.exists()

    verifier = VERIFIER.read_text(encoding="utf-8")
    assert "EXPECTED_TEAM_ID is required as an independent trust anchor" in verifier
    assert "^TeamIdentifier=$EXPECTED_TEAM_ID$" in verifier
    assert '"Developer ID Application: "*" ($EXPECTED_TEAM_ID)"' in verifier
    assert "verify-artifact" in verifier
    assert "codesign --verify --deep --strict" in verifier
    assert "verify_runtime_compatibility" in verifier
    assert "stapler validate" in verifier
    assert "spctl --assess" in verifier
    assert "--self-test --json" in verifier

    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "preflight_release_archive(path)" in manifest
    assert "MAX_RELEASE_ARCHIVE_ENTRIES" in manifest
    assert "MAX_RELEASE_ARCHIVE_EXPANDED_BYTES" in manifest


def test_release_workflow_actions_are_pinned_to_full_commit_shas():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*-\s+uses:\s+(\S+)", workflow, re.MULTILINE)
    assert uses
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", value) for value in uses)
    assert "pip install" not in workflow
    assert "uv sync --frozen" in workflow


def test_workflow_requires_an_independent_team_id_for_both_trigger_modes():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "expected_team_id:" in workflow
    assert "Independently verified 10-character Apple Developer Team ID" in workflow
    assert (
        "EXPECTED_TEAM_ID: ${{ github.event_name == 'workflow_dispatch' "
        "&& inputs.expected_team_id || vars.APPLE_TEAM_ID }}"
    ) in workflow
    assert "Developer ID identity does not match EXPECTED_TEAM_ID." in workflow
    assert "^TeamIdentifier=$EXPECTED_TEAM_ID$" in workflow
