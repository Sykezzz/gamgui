"""Classroom administration models, indexes, and guarded roster workflows."""

from .index import CourseIndex, CoursePage, CourseSnapshotStatus, default_course_index_path
from .manifests import (
    ManifestTarget,
    RosterManifest,
    RosterManifestStore,
    default_roster_manifest_path,
)
from .models import (
    CourseDetail,
    CourseParticipant,
    CourseRosterSnapshot,
    CourseSummary,
    RosterDiff,
)
from .service import ClassroomService, ClassroomValidationError

__all__ = [
    "ClassroomService",
    "ClassroomValidationError",
    "CourseDetail",
    "CourseIndex",
    "CoursePage",
    "CourseParticipant",
    "CourseRosterSnapshot",
    "CourseSnapshotStatus",
    "CourseSummary",
    "ManifestTarget",
    "RosterDiff",
    "RosterManifest",
    "RosterManifestStore",
    "default_course_index_path",
    "default_roster_manifest_path",
]
