"""Pure canonical semantic hashing for OneRoster managed-course state."""

from __future__ import annotations

from typing import Any, Iterable

from .models import canonical_hash


def normalize_email_values(values: Iterable[object]) -> tuple[str, ...]:
    """Return deterministic membership semantics without retaining identities twice."""

    return tuple(
        sorted(
            {
                str(value or "").strip().casefold()
                for value in values
                if str(value or "").strip()
            }
        )
    )


def metadata_basis(
    alias: object,
    name: object,
    owner_email: object,
    room: object,
    section: object,
    state: object = "ACTIVE",
) -> dict[str, Any]:
    """Build the exact desired metadata basis without touching authored descriptions."""

    return {
        "alias": str(alias or "").strip(),
        "name": str(name or "").strip(),
        "owner_email": str(owner_email or "").strip().casefold(),
        "room": str(room or "").strip(),
        "section": str(section or "").strip(),
        "state": str(state or "").strip().upper(),
    }


def metadata_hash(
    alias: object,
    name: object,
    owner_email: object,
    room: object,
    section: object,
    state: object = "ACTIVE",
) -> str:
    return canonical_hash(
        metadata_basis(alias, name, owner_email, room, section, state)
    )


def teacher_hash(values: Iterable[object]) -> str:
    return canonical_hash(normalize_email_values(values))


def student_hash(values: Iterable[object]) -> str:
    return canonical_hash(normalize_email_values(values))
