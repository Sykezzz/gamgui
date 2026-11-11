"""Release asset names encode minimum deployment targets, not host preference."""
import pytest

from scripts.gam_assets import select_mac_asset, version_tuple


def release(*targets):
    return {"tag_name": "v7.51.00", "assets": [
        {"name": f"gam-7.51.00-macos{target}-{arch}.tar.xz", "digest": "sha256:" + "a" * 64}
        for target, arch in targets
    ]}


def test_selects_newest_compatible_target_not_newest_release_asset():
    payload = release(("15.8", "arm64"), ("26.7", "arm64"), ("27.0", "arm64"), ("26.6", "x86_64"))
    assert "macos26.7-arm64" in select_mac_asset(payload, "arm64", "26.7.2")["name"]
    assert "macos15.8-arm64" in select_mac_asset(payload, "arm64", "26.6")["name"]
    assert "macos26.6-x86_64" in select_mac_asset(payload, "x86_64", "26.7.2")["name"]


def test_selection_compares_minor_versions_numerically():
    payload = release(("26.9", "arm64"), ("26.10", "arm64"))
    assert "macos26.10-" in select_mac_asset(payload, "arm64", "26.10")["name"]
    assert version_tuple("27.0.0") == version_tuple("27")


@pytest.mark.parametrize("host", ["26.6", "12.0"])
def test_no_compatible_asset_fails_closed(host):
    with pytest.raises(ValueError, match="No compatible"):
        select_mac_asset(release(("26.7", "arm64"), ("27.0", "arm64")), "arm64", host)


def test_wrong_architecture_and_release_are_not_selected():
    payload = release(("26.7", "x86_64"))
    payload["assets"].append({"name": "gam-7.50.00-macos15.0-arm64.tar.xz"})
    with pytest.raises(ValueError, match="No compatible"):
        select_mac_asset(payload, "arm64", "26.7")


def test_invalid_host_is_not_treated_as_latest():
    with pytest.raises(ValueError, match="Invalid macOS"):
        select_mac_asset(release(("26.7", "arm64")), "arm64", "unknown")


def test_effective_minimum_includes_bundled_gam_target():
    from scripts.gam_assets import effective_minimum_macos
    asset = "gam-7.51.00-macos26.7-arm64.tar.xz"
    assert effective_minimum_macos(asset, "12.0", "26.7.2") == "26.7"
    assert effective_minimum_macos(asset, "27.0", "27.0") == "27.0"
    with pytest.raises(ValueError, match="Build requires"):
        effective_minimum_macos(asset, "27.0", "26.7.2")
    with pytest.raises(ValueError, match="deployment target"):
        effective_minimum_macos("unrecognized.tar.xz", "12.0", "26.7.2")
