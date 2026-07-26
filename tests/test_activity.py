from __future__ import annotations

import pytest

from gamgui.core.activity import ActivityBusyError, ActivityRegistry


def test_activity_registry_is_exclusive_and_release_is_idempotent():
    registry = ActivityRegistry(clock=lambda: 123.0)
    lease = registry.acquire("component-profile-build")

    assert registry.snapshot().kind == "component-profile-build"
    assert registry.snapshot().started_at == 123.0
    with pytest.raises(ActivityBusyError) as caught:
        registry.acquire("app-update")
    assert caught.value.error_code == "CMP-ACTIVE-JOB"
    assert "profile" not in str(caught.value).lower()

    lease.release()
    lease.release()
    with registry.acquire("app-update"):
        assert registry.is_active()
    assert not registry.is_active()


@pytest.mark.parametrize("kind", ["", "has spaces", "../escape", "x" * 65])
def test_activity_registry_rejects_unbounded_or_unsafe_kinds(kind):
    with pytest.raises(ValueError):
        ActivityRegistry().acquire(kind)
