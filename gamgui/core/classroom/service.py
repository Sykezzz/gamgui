"""Guarded Classroom workflows layered over the GAM connector."""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..connectors.base import ChangeResult
from ..processes import current_process_identity
from .index import CourseIndex, CoursePage, MAX_PAGE_SIZE
from .manifests import RosterManifest, RosterManifestStore
from .models import (
    CourseDetail,
    CourseParticipant,
    RosterDiff,
    normalize_email,
    normalize_role,
    normalize_state,
    participant_emails,
    valid_email,
)

ROSTER_CHANGE_CAP = 200
_TRANSITIONS = {
    "PROVISIONED": ("ACTIVE",),
    "ACTIVE": ("ARCHIVED",),
    "ARCHIVED": ("ACTIVE",),
}


class ClassroomValidationError(ValueError):
    """A safe, user-facing refusal before a Classroom mutation."""


@dataclass(frozen=True)
class OwnerTransferPreview:
    course: CourseDetail
    target_email: str
    target_owner_id: str = ""


class ClassroomService:
    def __init__(
        self,
        connector,
        domain: str,
        course_index: CourseIndex,
        manifests: RosterManifestStore,
    ) -> None:
        self.connector = connector
        self.domain = (domain or "").strip().casefold()
        self.course_index = course_index
        self.manifests = manifests
        self._refresh_lock = asyncio.Lock()
        self._operation_owner = f"{os.getpid()}:{secrets.token_urlsafe(12)}"
        self._operation_identity = current_process_identity()

    async def refresh_index(self) -> int:
        async with self._refresh_lock:
            return await self.connector.refresh_course_index(self.course_index)

    async def search(
        self,
        query: str = "",
        state: str = "",
        cursor: Optional[str] = None,
        *,
        refreshing: bool = False,
    ) -> CoursePage:
        return await asyncio.to_thread(
            self.course_index.search,
            self.domain,
            query,
            state,
            cursor,
            MAX_PAGE_SIZE,
            refreshing,
        )

    async def create_course(
        self,
        *,
        name: str,
        owner_email: str,
        alias: str = "",
        section: str = "",
        room: str = "",
        description_heading: str = "",
        description: str = "",
    ) -> ChangeResult:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ClassroomValidationError("Enter a course name.")
        owner = await self._require_active_user(owner_email)
        result = await self.connector.create_course(
            name=clean_name,
            owner_email=owner,
            alias=(alias or "").strip(),
            section=(section or "").strip(),
            room=(room or "").strip(),
            description_heading=(description_heading or "").strip(),
            description=(description or "").strip(),
            state="PROVISIONED",
        )
        return result

    async def update_metadata(
        self,
        course_id: str,
        *,
        name: str,
        section: str = "",
        room: str = "",
        description_heading: str = "",
        description: str = "",
    ) -> Tuple[ChangeResult, CourseDetail]:
        course = await self._live_course(course_id)
        self._require_course_writable(course)
        clean_name = (name or "").strip()
        if not clean_name:
            raise ClassroomValidationError("Course name cannot be blank.")
        result = await self.connector.update_course(
            course.id,
            name=clean_name,
            section=(section or "").strip(),
            room=(room or "").strip(),
            description_heading=(description_heading or "").strip(),
            description=(description or "").strip(),
        )
        current = await self._patch_after(result, course.id)
        return result, current

    async def transition_state(
        self, course_id: str, target_state: str
    ) -> Tuple[ChangeResult, CourseDetail]:
        course = await self._live_course(course_id)
        target = normalize_state(target_state)
        allowed = _TRANSITIONS.get(course.course_state, ())
        if target not in allowed:
            raise ClassroomValidationError(
                f"{course.state_label} courses cannot transition to {target.title()} here."
            )
        result = await self.connector.update_course_state(course.id, target)
        current = await self._patch_after(result, course.id)
        return result, current

    async def prepare_owner_transfer(
        self, course_id: str, target_email: str
    ) -> OwnerTransferPreview:
        course = await self._live_course(course_id)
        self._require_course_writable(course)
        target, target_owner_id = await self._require_active_user_identity(target_email)
        if target_owner_id and course.owner_id and target_owner_id == course.owner_id:
            raise ClassroomValidationError(f"{target} already owns this course.")
        if (
            (not target_owner_id or not course.owner_id)
            and course.owner_email
            and target == course.owner_email
        ):
            raise ClassroomValidationError(f"{target} already owns this course.")
        return OwnerTransferPreview(
            course=course,
            target_email=target,
            target_owner_id=target_owner_id,
        )

    async def transfer_owner(
        self, course_id: str, target_email: str
    ) -> Tuple[ChangeResult, CourseDetail]:
        preview = await self.prepare_owner_transfer(course_id, target_email)
        result = await self.connector.transfer_course_owner(
            preview.course.id, preview.target_email
        )
        current = await self._patch_after(result, preview.course.id)
        verified = _owner_ids_match(preview.target_owner_id, current.owner_id)
        if result.ok and not verified and (
            not preview.target_owner_id or not current.owner_id
        ):
            try:
                current = await self._live_course(
                    preview.course.id, include_owner_email=True
                )
            except Exception:
                pass
            verified = _owner_ids_match(preview.target_owner_id, current.owner_id)
            if not verified and (not preview.target_owner_id or not current.owner_id):
                verified = bool(current.owner_email) and (
                    current.owner_email == preview.target_email
                )
        if result.ok and not verified:
            return (
                ChangeResult(
                    preview=result.preview,
                    ok=False,
                    detail=(
                        "GAM returned success, but a live re-read did not show the new owner. "
                        "No further ownership action was attempted."
                    ),
                ),
                current,
            )
        return result, current

    async def roster(
        self, course_id: str, role: str
    ) -> Tuple[CourseDetail, List[CourseParticipant]]:
        normalized_role = normalize_role(role)
        course = await self._live_course(course_id)
        members = await self.connector.list_course_participants(
            course.id, normalized_role
        )
        return course, members

    async def add_member(
        self, course_id: str, role: str, email: str
    ) -> Tuple[ChangeResult, CourseDetail]:
        normalized_role = normalize_role(role)
        course = await self._live_course(course_id)
        self._require_roster_writable(course)
        target = await self._require_active_user(email)
        result = await self.connector.add_course_participant(
            course.id, normalized_role, target
        )
        return result, course

    async def remove_member(
        self, course_id: str, role: str, email: str
    ) -> Tuple[ChangeResult, CourseDetail]:
        normalized_role = normalize_role(role)
        course = await self._live_course(course_id)
        self._require_roster_writable(course)
        target = normalize_email(email)
        if not valid_email(target):
            raise ClassroomValidationError(
                "Choose a current roster member by email."
            )
        members = await self.connector.list_course_participants(
            course.id,
            normalized_role,
        )
        selected = next(
            (
                member
                for member in members
                if normalize_email(member.email) == target
            ),
            None,
        )
        if selected is None:
            raise ClassroomValidationError(
                "That user is not a current member of this course roster."
            )
        canonical = normalize_email(selected.email)
        if normalized_role == "teachers":
            owner_email = _owner_email(course, members)
            owner_id_matches = bool(
                course.owner_id
                and selected.user_id
                and course.owner_id == selected.user_id
            )
            if owner_id_matches or (owner_email and canonical == owner_email):
                raise ClassroomValidationError(
                    "The course owner cannot be removed from the teacher roster."
                )
        result = await self.connector.remove_course_participant(
            course.id,
            normalized_role,
            canonical,
        )
        return result, course

    async def plan_roster(
        self, course_id: str, role: str, desired: Sequence[str]
    ) -> RosterManifest:
        normalized_role = normalize_role(role)
        course, current = await self.roster(course_id, normalized_role)
        self._require_roster_writable(course)
        desired_emails = self._validate_desired(desired)
        diff = RosterDiff.compute(
            normalized_role, desired_emails, participant_emails(current)
        )
        owner_email = _owner_email(course, current)
        if normalized_role == "teachers" and owner_email:
            if owner_email not in diff.desired:
                raise ClassroomValidationError(
                    f"Include the course owner ({owner_email}) in the desired teacher roster."
                )
        if diff.change_count > ROSTER_CHANGE_CAP:
            raise ClassroomValidationError(
                f"This roster differs by {diff.change_count} members. "
                f"The safety cap is {ROSTER_CHANGE_CAP}; split the change and preview again."
            )
        return await asyncio.to_thread(
            self.manifests.create, self.domain, course.id, diff
        )

    async def replan_manifest(self, manifest_id: str) -> RosterManifest:
        old = await asyncio.to_thread(self.manifests.get, manifest_id)
        if old is None or old.domain != self.domain:
            raise ClassroomValidationError("That roster preview is no longer available.")
        if old.status == "running":
            raise ClassroomValidationError("That roster operation is still running.")
        return await self.plan_roster(old.course_id, old.role, old.desired)

    async def apply_manifest(self, manifest_id: str) -> RosterManifest:
        manifest = await asyncio.to_thread(self.manifests.get, manifest_id)
        if manifest is None or manifest.domain != self.domain:
            raise ClassroomValidationError("That roster preview is no longer available.")
        if manifest.status != "planned":
            raise ClassroomValidationError(
                "This preview cannot be reused. Re-read the live roster and preview it again."
            )

        course, current = await self.roster(manifest.course_id, manifest.role)
        self._require_roster_writable(course)
        owner_email = _owner_email(course, current)
        live_diff = RosterDiff.compute(
            manifest.role, manifest.desired, participant_emails(current)
        )
        if live_diff.basis_hash != manifest.basis_hash:
            await asyncio.to_thread(
                self.manifests.finish,
                manifest.id,
                status="stale",
                residual=_residual_labels(live_diff),
                error="The live roster changed after this preview.",
            )
            raise ClassroomValidationError(
                "The live roster changed after this preview. Review a fresh diff before applying."
            )

        claimed = await asyncio.to_thread(
            self.manifests.mark_running,
            manifest.id,
            owner_id=self._operation_owner,
            owner_pid=os.getpid(),
            owner_identity=self._operation_identity,
        )
        if not claimed:
            raise ClassroomValidationError(
                "This roster preview was already claimed. "
                "Re-read the live roster and preview it again."
            )
        try:
            additions_succeeded = True
            for email in live_diff.adds:
                if not await asyncio.to_thread(
                    self.manifests.owns_claim,
                    manifest.id,
                    self._operation_owner,
                ):
                    raise ClassroomValidationError(
                        "The roster operation lease was lost; no further members were changed."
                    )
                target = email
                try:
                    target = await self._require_active_user(email)
                    result = await self.connector.add_course_participant(
                        course.id, manifest.role, target
                    )
                    await asyncio.to_thread(
                        self.manifests.mark_target,
                        manifest.id,
                        email,
                        "add",
                        ok=result.ok,
                        detail=result.detail,
                        owner_id=self._operation_owner,
                    )
                    if not result.ok:
                        additions_succeeded = False
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    additions_succeeded = False
                    await asyncio.to_thread(
                        self.manifests.mark_target,
                        manifest.id,
                        email,
                        "add",
                        ok=False,
                        detail=str(exc),
                        owner_id=self._operation_owner,
                    )
            for email in live_diff.removes:
                if not await asyncio.to_thread(
                    self.manifests.owns_claim,
                    manifest.id,
                    self._operation_owner,
                ):
                    raise ClassroomValidationError(
                        "The roster operation lease was lost; no further members were changed."
                    )
                if not additions_succeeded:
                    await asyncio.to_thread(
                        self.manifests.mark_target,
                        manifest.id,
                        email,
                        "remove",
                        ok=False,
                        detail=(
                            "Skipped because at least one required addition failed. "
                            "No removals were attempted."
                        ),
                        owner_id=self._operation_owner,
                    )
                    continue
                if (
                    manifest.role == "teachers"
                    and owner_email
                    and email == owner_email
                ):
                    await asyncio.to_thread(
                        self.manifests.mark_target,
                        manifest.id,
                        email,
                        "remove",
                        ok=False,
                        detail="The course owner cannot be removed.",
                        owner_id=self._operation_owner,
                    )
                    continue
                try:
                    result = await self.connector.remove_course_participant(
                        course.id, manifest.role, email
                    )
                    await asyncio.to_thread(
                        self.manifests.mark_target,
                        manifest.id,
                        email,
                        "remove",
                        ok=result.ok,
                        detail=result.detail,
                        owner_id=self._operation_owner,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await asyncio.to_thread(
                        self.manifests.mark_target,
                        manifest.id,
                        email,
                        "remove",
                        ok=False,
                        detail=str(exc),
                        owner_id=self._operation_owner,
                    )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.manifests.finish,
                manifest.id,
                status="interrupted",
                error="The app stopped before this roster operation finished.",
                owner_id=self._operation_owner,
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.manifests.finish,
                manifest.id,
                status="failed",
                error=str(exc),
                owner_id=self._operation_owner,
            )
            final = await asyncio.to_thread(self.manifests.get, manifest.id)
            if final is None:  # pragma: no cover
                raise
            return final

        try:
            _, after = await self.roster(course.id, manifest.role)
            residual_diff = RosterDiff.compute(
                manifest.role, manifest.desired, participant_emails(after)
            )
            residual = _residual_labels(residual_diff)
            current_manifest = await asyncio.to_thread(self.manifests.get, manifest.id)
            failed = current_manifest.failed_count if current_manifest else 0
            status = "completed" if not residual and not failed else "partial"
            await asyncio.to_thread(
                self.manifests.finish,
                manifest.id,
                status=status,
                residual=residual,
                error="" if status == "completed" else "Some roster changes did not apply.",
                owner_id=self._operation_owner,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.manifests.finish,
                manifest.id,
                status="interrupted",
                error="The app stopped before roster verification finished.",
                owner_id=self._operation_owner,
            )
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.manifests.finish,
                manifest.id,
                status="failed",
                error=f"Post-operation verification failed: {exc}",
                owner_id=self._operation_owner,
            )
        final = await asyncio.to_thread(self.manifests.get, manifest.id)
        if final is None:  # pragma: no cover
            raise RuntimeError("Roster operation result was not persisted.")
        return final

    async def courses_for_user(
        self, email: str
    ) -> Tuple[List[CourseDetail], List[CourseDetail]]:
        target = normalize_email(email)
        if not valid_email(target):
            raise ClassroomValidationError("Enter a valid user email.")
        teaching, enrolled = await asyncio.gather(
            self.connector.list_courses(teacher=target),
            self.connector.list_courses(student=target),
        )
        return teaching[:MAX_PAGE_SIZE], enrolled[:MAX_PAGE_SIZE]

    async def _live_course(
        self, course_id: str, *, include_owner_email: bool = False
    ) -> CourseDetail:
        clean_id = (course_id or "").strip()
        if not clean_id:
            raise ClassroomValidationError("A course ID is required.")
        course = await self.connector.get_course(
            clean_id, include_owner_email=include_owner_email
        )
        if not course.id:
            raise ClassroomValidationError("That course no longer exists.")
        return course

    async def _patch_after(
        self, result: ChangeResult, course_id: str, *, include_owner_email: bool = False
    ) -> CourseDetail:
        current = await self._live_course(
            course_id, include_owner_email=include_owner_email
        )
        if result.ok:
            await asyncio.to_thread(self.course_index.upsert, self.domain, current)
        return current

    async def _require_active_user(self, email: str) -> str:
        canonical, _ = await self._require_active_user_identity(email)
        return canonical

    async def _require_active_user_identity(self, email: str) -> Tuple[str, str]:
        target = normalize_email(email)
        if not valid_email(target):
            raise ClassroomValidationError("Enter a valid internal user email.")
        try:
            user = await self.connector.get_user(
                target, fields=("id", "primaryEmail", "suspended")
            )
        except Exception as exc:
            raise ClassroomValidationError(
                f"{target} was not found in the connected Workspace directory."
            ) from exc
        canonical = normalize_email(getattr(user, "primary_email", ""))
        if not canonical:
            raise ClassroomValidationError(
                f"{target} was not found in the connected Workspace directory."
            )
        if getattr(user, "suspended", False):
            raise ClassroomValidationError(
                f"{canonical} is suspended and cannot be enrolled or made owner."
            )
        raw = getattr(user, "raw", {})
        owner_id = ""
        if isinstance(raw, dict):
            owner_id = str(raw.get("id") or raw.get("ID") or "").strip()
        return canonical, owner_id

    def _validate_desired(self, desired: Sequence[str]) -> List[str]:
        values: List[str] = []
        seen = set()
        invalid: List[str] = []
        external: List[str] = []
        for raw in desired:
            email = normalize_email(raw)
            if not email:
                continue
            if not valid_email(email):
                invalid.append(email)
                continue
            if self.domain and not email.endswith("@" + self.domain):
                external.append(email)
                continue
            if email not in seen:
                seen.add(email)
                values.append(email)
        if invalid:
            raise ClassroomValidationError(
                f"Invalid email address: {', '.join(invalid[:3])}."
            )
        if external:
            raise ClassroomValidationError(
                "Direct Classroom enrollment is limited to the connected domain. "
                f"Remove: {', '.join(external[:3])}."
            )
        return values

    @staticmethod
    def _require_course_writable(course: CourseDetail) -> None:
        if course.read_only:
            raise ClassroomValidationError(
                f"{course.state_label} courses are read-only in GamGUI."
            )

    @staticmethod
    def _require_roster_writable(course: CourseDetail) -> None:
        if not course.roster_editable:
            raise ClassroomValidationError(
                "Activate the course before changing teachers or students."
            )


def _residual_labels(diff: RosterDiff) -> Tuple[str, ...]:
    return tuple([f"add:{email}" for email in diff.adds] + [f"remove:{email}" for email in diff.removes])


def _owner_ids_match(expected: str, actual: str) -> bool:
    return bool(expected and actual and expected == actual)


def _owner_email(
    course: CourseDetail, teachers: Sequence[CourseParticipant]
) -> str:
    if course.owner_email:
        return normalize_email(course.owner_email)
    if course.owner_id:
        for teacher in teachers:
            if teacher.user_id and teacher.user_id == course.owner_id:
                return normalize_email(teacher.email)
    return ""
