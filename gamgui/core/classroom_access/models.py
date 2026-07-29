"""Data contracts for Classroom Teachers entitlement policies and plans."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Tuple

from ..classroom.models import normalize_email, parse_desired_roster, valid_email


class PolicyStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    HELD = "held"
    DISABLED = "disabled"


class SourceMode(str, Enum):
    GOOGLE_GROUP = "google_group"
    CSV = "csv"


class CSVMode(str, Enum):
    UPLOAD = "upload"
    WATCH = "watch"


@dataclass(frozen=True)
class EntitlementPolicy:
    id: str
    domain: str
    target_group: str
    source_mode: str
    source_group: str = ""
    source_groups: Tuple[str, ...] = ()
    csv_mode: str = CSVMode.UPLOAD.value
    csv_emails: Tuple[str, ...] = ()
    watch_path: str = ""
    exception_users: Tuple[str, ...] = ()
    exception_groups: Tuple[str, ...] = ()
    connector_identity: str = ""
    status: str = PolicyStatus.DRAFT.value
    schedule_enabled: bool = True
    schedule_hour: int = 2
    schedule_minute: int = 0
    approved_config_hash: str = ""
    approved_source_hash: str = ""
    pending_plan_id: str = ""
    last_run_status: str = ""
    last_run_message: str = ""
    last_run_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def effective_source_groups(self) -> Tuple[str, ...]:
        """Return the canonical plural source while preserving legacy policies."""

        values = self.source_groups
        if not values and self.source_group:
            values = (self.source_group,)
        return tuple(
            sorted(
                {
                    normalize_email(str(value))
                    for value in values
                    if normalize_email(str(value))
                }
            )
        )

    @property
    def configuration_hash(self) -> str:
        csv_identity: Iterable[str]
        if self.source_mode == SourceMode.CSV.value and self.csv_mode == CSVMode.UPLOAD.value:
            csv_identity = self.csv_emails
        else:
            csv_identity = ()
        source_groups = self.effective_source_groups
        payload = {
            "domain": self.domain,
            "target_group": self.target_group,
            "source_mode": self.source_mode,
            # Preserve the original singleton payload so existing approvals and
            # restart-safe manifests do not become stale merely because the
            # persisted policy gained a plural representation.
            "source_group": source_groups[0] if len(source_groups) == 1 else "",
            "csv_mode": self.csv_mode,
            "csv_emails": list(csv_identity),
            "watch_path": self.watch_path,
            "exception_users": list(self.exception_users),
            "exception_groups": list(self.exception_groups),
            "connector_identity": self.connector_identity,
            "schedule_enabled": self.schedule_enabled,
            "schedule_hour": self.schedule_hour,
            "schedule_minute": self.schedule_minute,
        }
        if len(source_groups) > 1:
            payload["source_groups"] = list(source_groups)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntitlementPlan:
    id: str
    policy_id: str
    domain: str
    target_group: str
    desired: Tuple[str, ...]
    source_emails: Tuple[str, ...]
    current: Tuple[str, ...]
    adds: Tuple[str, ...]
    removes: Tuple[str, ...]
    unchanged: Tuple[str, ...]
    source_hash: str
    basis_hash: str
    status: str
    hold_reason: str = ""
    error: str = ""
    residual: Tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def change_count(self) -> int:
        return len(self.adds) + len(self.removes)


def parse_email_lines(text: str) -> Tuple[str, ...]:
    """Parse the same narrow email-list/CSV contract as Classroom rosters."""

    return tuple(parse_desired_roster(text or ""))


def normalize_email_tuple(values: Iterable[str], *, domain: str = "") -> Tuple[str, ...]:
    result = []
    seen = set()
    expected_domain = domain.strip().casefold()
    for raw in values:
        email = normalize_email(str(raw))
        if not email:
            continue
        if not valid_email(email):
            raise ValueError(f"{email} is not a valid email address.")
        if expected_domain and not email.endswith(f"@{expected_domain}"):
            raise ValueError(f"{email} is outside the connected Workspace domain.")
        if email not in seen:
            seen.add(email)
            result.append(email)
    return tuple(sorted(result))


def stable_hash(*parts: Iterable[str] | str) -> str:
    lines = []
    for part in parts:
        if isinstance(part, str):
            lines.append(part)
        else:
            lines.extend(str(value) for value in part)
        lines.append("\0")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def connector_identity_for(connector, domain: str) -> str:
    """Return a non-secret fingerprint of the active GAM credential identity."""

    vault = getattr(getattr(connector, "runner", None), "vault", None)
    getter = getattr(vault, "get", None)
    if not callable(getter):
        return ""
    identity_keys = {
        "client_email",
        "client_id",
        "email",
        "private_key_id",
        "project_id",
        "quota_project_id",
        "universe_domain",
    }
    parts = [domain.strip().casefold()]
    for credential in ("client_secrets", "oauth2", "oauth2service"):
        raw = getter(domain, credential) or ""
        if not raw:
            parts.append(f"{credential}:missing")
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parts.append(
                f"{credential}:opaque:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
            )
            continue
        values = []

        def collect(value) -> None:
            if isinstance(value, dict):
                for key in sorted(value):
                    item = value[key]
                    if str(key).casefold() in identity_keys and item not in (None, ""):
                        values.append(f"{str(key).casefold()}={item}")
                    elif isinstance(item, (dict, list)):
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(parsed)
        parts.append(f"{credential}:{'|'.join(sorted(values)) or 'present'}")
    return stable_hash("gam-connector", parts)


def now() -> float:
    return time.time()
