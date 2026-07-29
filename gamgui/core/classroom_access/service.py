"""Live planning and guarded application for Classroom Teachers membership."""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..classroom.models import normalize_email
from .models import (
    CSVMode,
    EntitlementPlan,
    EntitlementPolicy,
    SourceMode,
    connector_identity_for,
    normalize_email_tuple,
    parse_email_lines,
    stable_hash,
)
from .store import EntitlementStore

CHANGE_CAP = 200
REMOVAL_COUNT_HOLD = 25
REMOVAL_RATIO_HOLD = 0.10
WATCH_FILE_MAX_BYTES = 10 * 1024 * 1024


class EntitlementValidationError(ValueError):
    pass


class EntitlementService:
    def __init__(self, connector, domain: str, store: EntitlementStore) -> None:
        self.connector = connector
        self.domain = domain.strip().casefold()
        self.store = store
        self.connector_identity = connector_identity_for(connector, self.domain)
        self._owner = f"entitlement:{os.getpid()}:{secrets.token_urlsafe(8)}"

    async def plan(
        self,
        policy: EntitlementPolicy,
        *,
        approval_required: bool,
    ) -> EntitlementPlan:
        self._require_policy_domain(policy)
        if (
            policy.connector_identity
            and policy.connector_identity != self.connector_identity
        ):
            return self._held(
                policy,
                "The Workspace connector identity changed. Preview and approve the policy again.",
            )
        try:
            groups = await self.connector.list_groups()
        except Exception:
            return self._held(policy, "Workspace groups could not be read.")
        group_map = {
            normalize_email(getattr(group, "email", "")): group
            for group in groups
            if normalize_email(getattr(group, "email", ""))
        }
        if policy.target_group not in group_map:
            return self._held(policy, "The confirmed Classroom Teachers group was not found.")
        if policy.source_group and policy.source_group == policy.target_group:
            return self._held(policy, "The source group cannot also be the target group.")

        source_emails: tuple[str, ...] = ()
        desired_users: tuple[str, ...] = ()
        desired_groups = list(policy.exception_groups)
        hold_reason = ""
        if policy.source_mode == SourceMode.GOOGLE_GROUP.value:
            if not policy.source_group or policy.source_group not in group_map:
                return self._held(policy, "The synchronized staff source group was not found.")
            try:
                source_members = await self.connector.list_group_members(policy.source_group)
            except Exception:
                return self._held(policy, "The synchronized staff source group could not be read.")
            source_identities = tuple(
                sorted(
                    f"{str(getattr(member, 'member_type', 'USER')).upper()}:{normalize_email(getattr(member, 'email', ''))}"
                    for member in source_members
                    if normalize_email(getattr(member, "email", ""))
                )
            )
            if not source_identities:
                hold_reason = "The synchronized staff source group is empty."
            desired_groups.append(policy.source_group)
            source_hash = stable_hash("google_group", policy.source_group, source_identities)
        elif policy.source_mode == SourceMode.CSV.value:
            try:
                source_emails = await self._csv_source(policy)
            except (OSError, ValueError) as exc:
                return self._held(policy, str(exc))
            if not source_emails:
                hold_reason = "The CSV source is empty."
            try:
                desired_users = await self._require_active_users(
                    (*source_emails, *policy.exception_users)
                )
            except EntitlementValidationError as exc:
                return self._held(policy, str(exc))
            except Exception:
                return self._held(
                    policy, "The Workspace user directory could not be read."
                )
            source_hash = stable_hash("csv", source_emails)
        else:
            return self._held(policy, "Choose a synchronized group or CSV source.")

        invalid_groups = [
            group for group in desired_groups if normalize_email(group) not in group_map
        ]
        if invalid_groups:
            return self._held(
                policy,
                f"An exception group was not found: {normalize_email(invalid_groups[0])}",
            )
        if policy.source_mode == SourceMode.GOOGLE_GROUP.value:
            try:
                desired_users = await self._require_active_users(
                    policy.exception_users
                )
            except EntitlementValidationError as exc:
                return self._held(policy, str(exc))
            except Exception:
                return self._held(
                    policy, "The Workspace user directory could not be read."
                )

        desired = normalize_email_tuple(
            (*desired_users, *desired_groups), domain=self.domain
        )
        if not desired:
            hold_reason = hold_reason or "The desired Classroom Teachers membership is empty."

        try:
            current_members = await self.connector.list_group_members(policy.target_group)
        except Exception:
            return self._held(policy, "The Classroom Teachers group could not be read.")
        current = normalize_email_tuple(
            (
                getattr(member, "email", "")
                for member in current_members
                if getattr(member, "email", "")
            )
        )
        desired_set = set(desired)
        current_set = set(current)
        adds = tuple(sorted(desired_set - current_set))
        removes = tuple(sorted(current_set - desired_set))
        unchanged = tuple(sorted(desired_set & current_set))
        basis_hash = stable_hash(
            policy.id,
            policy.configuration_hash,
            source_hash,
            ("ADD", *adds),
            ("REMOVE", *removes),
        )

        if approval_required:
            hold_reason = "Review and approve this exact membership plan."
        elif not hold_reason and len(adds) + len(removes) > CHANGE_CAP:
            hold_reason = (
                f"The plan has {len(adds) + len(removes)} changes; "
                f"scheduled runs hold above {CHANGE_CAP}."
            )
        elif not hold_reason and len(removes) > REMOVAL_COUNT_HOLD:
            hold_reason = (
                f"The plan removes {len(removes)} members; "
                f"scheduled runs hold above {REMOVAL_COUNT_HOLD}."
            )
        elif (
            not hold_reason
            and current
            and len(removes) / len(current) > REMOVAL_RATIO_HOLD
        ):
            hold_reason = "The plan removes more than 10% of direct target membership."

        plan = EntitlementPlan(
            id="",
            policy_id=policy.id,
            domain=self.domain,
            target_group=policy.target_group,
            desired=desired,
            source_emails=source_emails,
            current=current,
            adds=adds,
            removes=removes,
            unchanged=unchanged,
            source_hash=source_hash,
            basis_hash=basis_hash,
            status="held" if hold_reason else "planned",
            hold_reason=hold_reason,
        )
        persisted = await asyncio.to_thread(self.store.create_plan, plan)
        self._audit(
            "classroom_teacher_entitlement_plan",
            policy.target_group,
            ok=not bool(hold_reason),
            extra={
                "policy_id": policy.id,
                "adds": len(adds),
                "removes": len(removes),
                "held": bool(hold_reason),
            },
        )
        return persisted

    async def apply(self, plan_id: str) -> EntitlementPlan:
        plan = await asyncio.to_thread(self.store.get_plan, plan_id)
        if plan is None or plan.domain != self.domain:
            raise EntitlementValidationError("That entitlement plan is no longer available.")
        if plan.status not in {"approved", "planned"}:
            raise EntitlementValidationError(
                "Review a fresh entitlement plan before applying."
            )
        policy = await asyncio.to_thread(self.store.get_policy, plan.policy_id)
        if policy is None or policy.domain != self.domain:
            raise EntitlementValidationError("The entitlement policy is no longer available.")
        if (
            policy.connector_identity
            and policy.connector_identity != self.connector_identity
        ):
            raise EntitlementValidationError(
                "The Workspace connector identity changed. Review a fresh plan."
            )

        fresh = await self._live_basis(policy, plan.source_hash)
        if fresh != plan.basis_hash:
            if await asyncio.to_thread(self.store.claim_plan, plan.id, self._owner):
                await asyncio.to_thread(
                    self.store.finish_plan,
                    plan.id,
                    status="stale",
                    error="The live membership changed after preview.",
                    owner_id=self._owner,
                )
            await asyncio.to_thread(
                self.store.record_policy_result,
                policy.id,
                status="stale",
                message="The live membership changed after preview.",
            )
            raise EntitlementValidationError(
                "The live membership changed after preview. Review a fresh plan."
            )

        if not await asyncio.to_thread(self.store.claim_plan, plan.id, self._owner):
            raise EntitlementValidationError("That entitlement plan was already claimed.")

        additions_succeeded = True
        try:
            for chunk in _chunks(plan.adds, CHANGE_CAP):
                for email in chunk:
                    self._require_claim(plan.id)
                    try:
                        result = await self.connector.add_group_member(
                            plan.target_group, email
                        )
                        additions_succeeded = additions_succeeded and bool(result.ok)
                        await asyncio.to_thread(
                            self.store.mark_target,
                            plan.id,
                            email,
                            "add",
                            ok=bool(result.ok),
                            detail=str(result.detail or ""),
                            owner_id=self._owner,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        additions_succeeded = False
                        await asyncio.to_thread(
                            self.store.mark_target,
                            plan.id,
                            email,
                            "add",
                            ok=False,
                            detail=str(exc),
                            owner_id=self._owner,
                        )
            for chunk in _chunks(plan.removes, CHANGE_CAP):
                for email in chunk:
                    self._require_claim(plan.id)
                    if not additions_succeeded:
                        await asyncio.to_thread(
                            self.store.mark_target,
                            plan.id,
                            email,
                            "remove",
                            ok=False,
                            detail="Skipped because at least one required addition failed.",
                            owner_id=self._owner,
                        )
                        continue
                    try:
                        result = await self.connector.remove_group_member(
                            plan.target_group, email
                        )
                        await asyncio.to_thread(
                            self.store.mark_target,
                            plan.id,
                            email,
                            "remove",
                            ok=bool(result.ok),
                            detail=str(result.detail or ""),
                            owner_id=self._owner,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await asyncio.to_thread(
                            self.store.mark_target,
                            plan.id,
                            email,
                            "remove",
                            ok=False,
                            detail=str(exc),
                            owner_id=self._owner,
                        )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.finish_plan,
                plan.id,
                status="interrupted",
                error="The reconciliation was interrupted.",
                owner_id=self._owner,
            )
            await asyncio.to_thread(
                self.store.record_policy_result,
                policy.id,
                status="interrupted",
                message="The reconciliation was interrupted.",
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.store.finish_plan,
                plan.id,
                status="failed",
                error=str(exc),
                owner_id=self._owner,
            )
            await asyncio.to_thread(
                self.store.record_policy_result,
                policy.id,
                status="failed",
                message=str(exc),
            )
            raise

        try:
            after = normalize_email_tuple(
                (
                    getattr(member, "email", "")
                    for member in await self.connector.list_group_members(plan.target_group)
                    if getattr(member, "email", "")
                )
            )
            residual = tuple(
                sorted(
                    {f"add:{email}" for email in set(plan.desired) - set(after)}
                    | {f"remove:{email}" for email in set(after) - set(plan.desired)}
                )
            )
            status = "completed" if not residual and additions_succeeded else "partial"
            message = (
                "Classroom Teachers membership matches the approved source."
                if status == "completed"
                else "Some entitlement changes did not apply; review the residual list."
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.finish_plan,
                plan.id,
                status="interrupted",
                error="The reconciliation stopped before verification finished.",
                owner_id=self._owner,
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.store.finish_plan,
                plan.id,
                status="failed",
                error=f"Post-operation verification failed: {exc}",
                owner_id=self._owner,
            )
            await asyncio.to_thread(
                self.store.record_policy_result,
                policy.id,
                status="failed",
                message="Post-operation verification failed; review live membership.",
            )
            raise
        await asyncio.to_thread(
            self.store.finish_plan,
            plan.id,
            status=status,
            residual=residual,
            error="" if status == "completed" else message,
            owner_id=self._owner,
        )
        await asyncio.to_thread(
            self.store.record_policy_result,
            policy.id,
            status=status,
            message=message,
            source_hash=plan.source_hash,
            source_emails=plan.source_emails,
            activate=status == "completed",
        )
        self._audit(
            "classroom_teacher_entitlement_apply",
            plan.target_group,
            ok=status == "completed",
            extra={
                "policy_id": policy.id,
                "plan_id": plan.id,
                "adds": len(plan.adds),
                "removes": len(plan.removes),
                "residual": len(residual),
            },
        )
        final = await asyncio.to_thread(self.store.get_plan, plan.id)
        if final is None:
            raise RuntimeError("The entitlement result was not persisted.")
        return final

    async def resume_interrupted(
        self, plan_id: str
    ) -> Optional[EntitlementPlan]:
        """Resume only the remaining portion of an unchanged approved manifest."""

        original = await asyncio.to_thread(self.store.get_plan, plan_id)
        if original is None or original.domain != self.domain:
            raise EntitlementValidationError("That entitlement plan is no longer available.")
        if original.status != "interrupted":
            return None
        policy = await asyncio.to_thread(self.store.get_policy, original.policy_id)
        if policy is None or policy.pending_plan_id != original.id:
            return None
        self._require_policy_domain(policy)

        expected_basis = stable_hash(
            policy.id,
            policy.configuration_hash,
            original.source_hash,
            ("ADD", *original.adds),
            ("REMOVE", *original.removes),
        )
        if expected_basis != original.basis_hash:
            return None

        groups = {
            normalize_email(getattr(group, "email", ""))
            for group in await self.connector.list_groups()
            if normalize_email(getattr(group, "email", ""))
        }
        required_groups = {
            policy.target_group,
            *policy.exception_groups,
        }
        if policy.source_mode == SourceMode.GOOGLE_GROUP.value:
            required_groups.add(policy.source_group)
            source_members = await self.connector.list_group_members(policy.source_group)
            source_identities = tuple(
                sorted(
                    f"{str(getattr(member, 'member_type', 'USER')).upper()}:{normalize_email(getattr(member, 'email', ''))}"
                    for member in source_members
                    if normalize_email(getattr(member, "email", ""))
                )
            )
            source_emails: tuple[str, ...] = ()
            source_hash = stable_hash(
                "google_group", policy.source_group, source_identities
            )
        else:
            source_emails = await self._csv_source(policy)
            source_hash = stable_hash("csv", source_emails)
        if not required_groups.issubset(groups) or source_hash != original.source_hash:
            return None

        desired = await self._desired_for_basis(policy, source_emails)
        if desired != original.desired:
            return None
        current = normalize_email_tuple(
            (
                getattr(member, "email", "")
                for member in await self.connector.list_group_members(policy.target_group)
                if getattr(member, "email", "")
            )
        )
        current_set = set(current)
        original_set = set(original.current)
        desired_set = set(desired)
        if current_set - (original_set | desired_set):
            return None
        if set(original.unchanged) - current_set:
            return None

        adds = tuple(sorted(desired_set - current_set))
        removes = tuple(sorted(current_set - desired_set))
        resumed = await asyncio.to_thread(
            self.store.create_plan,
            EntitlementPlan(
                id="",
                policy_id=policy.id,
                domain=self.domain,
                target_group=policy.target_group,
                desired=desired,
                source_emails=source_emails,
                current=current,
                adds=adds,
                removes=removes,
                unchanged=tuple(sorted(desired_set & current_set)),
                source_hash=source_hash,
                basis_hash=stable_hash(
                    policy.id,
                    policy.configuration_hash,
                    source_hash,
                    ("ADD", *adds),
                    ("REMOVE", *removes),
                ),
                status="planned",
            ),
        )
        self._audit(
            "classroom_teacher_entitlement_resume",
            policy.target_group,
            ok=True,
            extra={
                "policy_id": policy.id,
                "interrupted_plan_id": original.id,
                "resumed_plan_id": resumed.id,
                "remaining_adds": len(adds),
                "remaining_removes": len(removes),
            },
        )
        return await self.apply(resumed.id)

    async def _live_basis(self, policy: EntitlementPolicy, expected_source_hash: str) -> str:
        source: Sequence[str] = ()
        if policy.source_mode == SourceMode.GOOGLE_GROUP.value:
            source_members = await self.connector.list_group_members(policy.source_group)
            identities = tuple(
                sorted(
                    f"{str(getattr(member, 'member_type', 'USER')).upper()}:{normalize_email(getattr(member, 'email', ''))}"
                    for member in source_members
                    if normalize_email(getattr(member, "email", ""))
                )
            )
            source_hash = stable_hash("google_group", policy.source_group, identities)
        else:
            source = await self._csv_source(policy)
            source_hash = stable_hash("csv", source)
        if source_hash != expected_source_hash:
            return ""
        current = normalize_email_tuple(
            (
                getattr(member, "email", "")
                for member in await self.connector.list_group_members(policy.target_group)
                if getattr(member, "email", "")
            )
        )
        desired = await self._desired_for_basis(policy, source)
        desired_set = set(desired)
        current_set = set(current)
        return stable_hash(
            policy.id,
            policy.configuration_hash,
            source_hash,
            ("ADD", *sorted(desired_set - current_set)),
            ("REMOVE", *sorted(current_set - desired_set)),
        )

    async def _desired_for_basis(
        self, policy: EntitlementPolicy, source_emails: Sequence[str]
    ) -> tuple[str, ...]:
        if policy.source_mode == SourceMode.GOOGLE_GROUP.value:
            users = await self._require_active_users(policy.exception_users)
            groups = (*policy.exception_groups, policy.source_group)
        else:
            users = await self._require_active_users(
                (*source_emails, *policy.exception_users)
            )
            groups = policy.exception_groups
        return normalize_email_tuple((*users, *groups), domain=self.domain)

    async def _csv_source(self, policy: EntitlementPolicy) -> tuple[str, ...]:
        if policy.csv_mode == CSVMode.UPLOAD.value:
            return normalize_email_tuple(policy.csv_emails, domain=self.domain)
        path = Path(policy.watch_path).expanduser()
        if not path.is_absolute():
            raise ValueError("The watched CSV path must be absolute.")
        try:
            before = path.stat()
        except FileNotFoundError as exc:
            raise ValueError("The watched CSV was not found.") from exc
        except OSError as exc:
            raise ValueError("The watched CSV could not be read safely.") from exc
        if not path.is_file() or path.is_symlink():
            raise ValueError("The watched CSV must be a regular file.")
        if before.st_size > WATCH_FILE_MAX_BYTES:
            raise ValueError("The watched CSV is larger than 10 MB.")
        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
            after = path.stat()
        except FileNotFoundError as exc:
            raise ValueError("The watched CSV disappeared while it was being read.") from exc
        except OSError as exc:
            raise ValueError("The watched CSV could not be read safely.") from exc
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("The watched CSV changed while it was being read.")
        return normalize_email_tuple(parse_email_lines(content), domain=self.domain)

    async def _require_active_users(self, emails: Iterable[str]) -> tuple[str, ...]:
        desired = normalize_email_tuple(emails, domain=self.domain)
        if not desired:
            return ()
        bulk = getattr(self.connector, "list_oneroster_directory", None)
        if callable(bulk):
            directory = await bulk()
            users = {}
            for value in directory.values():
                canonical = normalize_email(getattr(value, "primary_email", ""))
                if canonical:
                    users[canonical] = value
            missing = [email for email in desired if email not in users]
            suspended = [
                email for email in desired if email in users and getattr(users[email], "suspended", False)
            ]
        else:
            users = {}
            missing = []
            suspended = []
            for email in desired:
                try:
                    user = await self.connector.get_user(
                        email, fields=("primaryEmail", "suspended")
                    )
                except Exception:
                    missing.append(email)
                    continue
                canonical = normalize_email(getattr(user, "primary_email", ""))
                if not canonical:
                    missing.append(email)
                elif getattr(user, "suspended", False):
                    suspended.append(canonical)
                else:
                    users[canonical] = user
        if missing:
            raise EntitlementValidationError(
                f"{missing[0]} was not found in the connected Workspace directory."
            )
        if suspended:
            raise EntitlementValidationError(
                f"{suspended[0]} is suspended and cannot receive Classroom teacher access."
            )
        return desired

    def _held(self, policy: EntitlementPolicy, reason: str) -> EntitlementPlan:
        plan = EntitlementPlan(
            id="",
            policy_id=policy.id,
            domain=self.domain,
            target_group=policy.target_group,
            desired=(),
            source_emails=(),
            current=(),
            adds=(),
            removes=(),
            unchanged=(),
            source_hash="",
            basis_hash="",
            status="held",
            hold_reason=reason,
        )
        return self.store.create_plan(plan)

    def _require_policy_domain(self, policy: EntitlementPolicy) -> None:
        if policy.domain != self.domain:
            raise EntitlementValidationError(
                "The policy belongs to a different Workspace domain."
            )
        for email in (
            policy.target_group,
            policy.source_group,
            *policy.exception_users,
            *policy.exception_groups,
        ):
            if email and not normalize_email(email).endswith(f"@{self.domain}"):
                raise EntitlementValidationError(
                    f"{normalize_email(email)} is outside the connected Workspace domain."
                )

    def _require_claim(self, plan_id: str) -> None:
        if not self.store.owns_claim(plan_id, self._owner):
            raise EntitlementValidationError(
                "The entitlement operation lease was lost; no further changes were made."
            )

    def _audit(self, action: str, target: str, *, ok: bool, extra: dict) -> None:
        audit = getattr(self.connector, "audit", None)
        record = getattr(audit, "record", None)
        if callable(record):
            record(action, target=target, ok=ok, extra=extra)


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]
