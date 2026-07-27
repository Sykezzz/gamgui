"""GAM7 command builders.

Every ``gam`` invocation in the app is constructed here and nowhere else. Two reasons:

1. **Safety** — builders return an ``argv`` *list* (e.g. ``["print", "users", ...]``), which is
   passed straight to ``exec`` with no shell. User-supplied values (emails, signatures) are never
   interpolated into a shell string, so there is no shell-injection surface.
2. **Maintainability** — GAM occasionally tweaks sub-command syntax between releases. Keeping it in
   one file with arg-shape tests means a version bump is a single-file change.

NOTE: the exact sub-syntax of a few mutating commands (group membership, signature flags) should be
re-verified against the pinned GAM version during the real-tenant acceptance pass. The arg-shape
unit tests pin today's intended form so drift is caught early.
"""

from __future__ import annotations

import shlex
from typing import List, Optional, Sequence

# The GAM7 version GamGUI is pinned to and tested against — the SINGLE SOURCE OF TRUTH.
# `scripts/fetch_gam.sh` (TAG), the mock, and the version tests must all match this; that's enforced by
# tests/test_command_contract.py so they can't drift. Bump deliberately via the "Updating GAM" runbook.
# Compared (as a substring) against the running `gam version` for the fail-soft runtime self-check.
EXPECTED_GAM_VERSION = "7.47.00"

# Roles accepted by Google Directory for group membership.
GROUP_ROLES = ("member", "manager", "owner")

# `gam print users` returns ONLY primaryEmail unless fields are requested — these populate the list.
# `organizations` carries the job title (the practical "role" for automations).
USER_LIST_FIELDS = ("primaryEmail", "name", "suspended", "orgUnitPath", "organizations")
# Signature rendering needs profile identity plus only the variables exposed by the editor.
# Keep this separate from the detail/cache projections so a company-wide signature preview does
# not spool aliases, recovery data, admin flags, or other fields that never reach the template.
SIGNATURE_USER_FIELDS = (
    "primaryEmail",
    "name",
    "suspended",
    "orgUnitPath",
    "organizations",
    "locations",
    "phones",
)
# Minimal persisted summary projection used by DirectoryIndex and count-first reports. Detail-only
# recovery, phone, location, alias, and content fields intentionally remain live-only.
DIRECTORY_INDEX_FIELDS = USER_LIST_FIELDS + (
    "isAdmin",
    "isDelegatedAdmin",
    "isEnrolledIn2Sv",
    "lastLoginTime",
)
# Fields for the detail view: identity + role/automation signals + security flags.
USER_DETAIL_FIELDS = (
    "primaryEmail", "name", "suspended", "orgUnitPath", "isAdmin", "isDelegatedAdmin",
    "isEnrolledIn2Sv", "lastLoginTime", "aliases", "organizations", "locations", "phones", "recoveryEmail",
)
# `gam print groups` likewise returns only email unless fields are requested.
GROUP_LIST_FIELDS = ("email", "name", "description", "directMembersCount")
# Tidy column sets for the Builder "Find Chromebooks" / "Find Drive files" searches.
CROS_LIST_FIELDS = ("deviceId", "serialNumber", "status", "orgUnitPath", "annotatedAssetId",
                    "annotatedUser", "lastSync", "model")
FILE_LIST_FIELDS = ("id", "name", "mimeType", "owners", "modifiedTime", "webViewLink")
# Keep the domain-wide Classroom snapshot cheap. ``owneremail``, aliases, and participant flags
# trigger additional API calls per course in GAM, so they belong only on one-course detail reads.
COURSE_INDEX_FIELDS = (
    "id", "name", "section", "room", "ownerId", "courseState",
    "creationTime", "updateTime", "alternateLink",
)
COURSE_DETAIL_FIELDS = COURSE_INDEX_FIELDS + (
    "descriptionHeading", "description",
)
# OneRoster planning resolves every source identity from one Directory snapshot.
# Do not add profile, security, recovery, or organizational fields to this projection.
ONEROSTER_DIRECTORY_FIELDS = ("id", "primaryEmail", "aliases", "suspended")
COURSE_STATES = ("active", "archived", "provisioned", "declined", "suspended")
COURSE_ROSTER_ROLES = ("teachers", "students")
# Superset fetched once and cached to serve the users list (needs title), reports, AND the detail
# view (so opening a user is instant + uses the reliable JSON path, not the `info user` text format).
CACHE_FIELDS = (
    "primaryEmail", "name", "suspended", "orgUnitPath", "organizations",
    "isAdmin", "isDelegatedAdmin", "isEnrolledIn2Sv", "lastLoginTime", "recoveryEmail",
    "aliases", "locations", "phones",
)


class GAMCommands:
    # --- diagnostics / setup ----------------------------------------------------------
    @staticmethod
    def version() -> List[str]:
        return ["version"]

    @staticmethod
    def create_project(admin: str, project_id: Optional[str] = None) -> List[str]:
        argv = ["create", "project", admin]
        if project_id:
            argv += ["project", project_id]
        return argv

    @staticmethod
    def oauth_create(admin: str) -> List[str]:
        return ["oauth", "create", admin]

    @staticmethod
    def create_svcacct(admin: str) -> List[str]:
        return ["create", "svcacct", admin]

    @staticmethod
    def check_svcacct(
        admin: str, scopes: Optional[Sequence[str]] = None
    ) -> List[str]:
        # Verifies domain-wide delegation scopes. NOTE: the noun is `serviceaccount`
        # here (GAM uses `create svcacct` but `check serviceaccount` — not symmetric).
        argv = ["user", admin, "check", "serviceaccount"]
        if scopes:
            argv += ["scopes", ",".join(scopes)]
        return argv

    # --- users (read) -----------------------------------------------------------------
    @staticmethod
    def print_users(query: Optional[str] = None, fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["print", "users"]
        if query:
            argv += ["query", query]
        argv += ["fields", ",".join(fields or USER_LIST_FIELDS)]
        argv.append("formatjson")
        return argv

    @staticmethod
    def print_cros(query: str = "", fields: Optional[Sequence[str]] = None) -> List[str]:
        """Search the ChromeOS device fleet by a CrOS query (read-only). `query` rides as one argv
        element. Emits CSV via formatjson for the result table."""
        argv = ["print", "cros"]
        if query:
            argv += ["query", query]
        argv += ["fields", ",".join(fields or CROS_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def print_filelist(
        email: str,
        query: str = "",
        fields: Optional[Sequence[str]] = None,
        max_files: Optional[int] = None,
    ) -> List[str]:
        """Search a user's Drive files by a Drive v3 query (read-only). `query` rides as one argv
        element (never shell-spliced)."""
        argv = ["user", email, "print", "filelist"]
        if query:
            argv += ["query", query]
        if max_files is not None:
            limit = int(max_files)
            if not 1 <= limit <= 1000:
                raise ValueError("max_files must be between 1 and 1000")
            argv += ["maxfiles", str(limit)]
        argv += ["fields", ",".join(fields or FILE_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def transfer_drive_ownership(source: str, file_id: str, destination: str) -> List[str]:
        """Transfer exactly one file; omitting ``norecursion`` could transfer an entire folder tree."""
        return [
            "user",
            source,
            "transfer",
            "ownership",
            f"id:{file_id}",
            destination,
            "norecursion",
        ]

    @staticmethod
    def claim_drive_ownership(claimant: str, file_id: str, previous_owner: str) -> List[str]:
        """Claim one non-folder file and request removal of the previous owner's retained role."""
        argv = ["user", claimant, "claim", "ownership", f"id:{file_id}"]
        if previous_owner:
            argv += ["onlyusers", previous_owner]
        return argv + ["retainrole", "none"]

    @staticmethod
    def report_users(date: str, params: Sequence[str]) -> List[str]:
        # Admin SDK usage report (storage, mail, drive). Data lags ~2-3 days.
        return ["report", "users", "date", date, "parameters", ",".join(params)]

    @staticmethod
    def info_user(email: str, fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["info", "user", email, "fields", ",".join(fields or USER_DETAIL_FIELDS)]
        argv.append("formatjson")
        return argv

    # --- Classroom --------------------------------------------------------------------
    @staticmethod
    def print_oneroster_directory() -> List[str]:
        """Read only identity fields needed for a district OneRoster plan."""
        return [
            "print",
            "users",
            "fields",
            ",".join(ONEROSTER_DIRECTORY_FIELDS),
            "formatjson",
        ]

    @staticmethod
    def print_courses(
        states: Optional[Sequence[str]] = None,
        teacher: str = "",
        student: str = "",
        fields: Optional[Sequence[str]] = None,
    ) -> List[str]:
        if teacher and student:
            raise ValueError("Classroom teacher and student filters are mutually exclusive")
        argv = ["print", "courses"]
        if teacher:
            argv += ["teacher", teacher]
        if student:
            argv += ["student", student]
        if states:
            argv += ["states", ",".join(_validate_course_state(state) for state in states)]
        argv += ["fields", ",".join(fields or COURSE_INDEX_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def print_oneroster_courses_file(path: str) -> List[str]:
        """Resolve exact managed aliases from one private GAM ``CourseEntity`` file.

        ``aliases`` is intentionally enabled for this exact-set read.  GAM returns
        each successful course's aliases, which lets the connector associate sparse
        results with their requested ``Section_<ID>`` without recursively launching
        more GAM processes.
        """

        value = str(path or "")
        if not value or any(character in value for character in "\r\n\x00"):
            raise ValueError("invalid managed Classroom selector path")
        return [
            "print",
            "courses",
            "course",
            "file",
            value,
            "aliases",
            "fields",
            ",".join(COURSE_INDEX_FIELDS),
            "formatjson",
        ]

    @staticmethod
    def info_course(
        course_id: str,
        fields: Optional[Sequence[str]] = None,
        *,
        include_owner_email: bool = False,
        include_aliases: bool = False,
    ) -> List[str]:
        # Owner-email and alias enrichment can fail when a legacy course owner is unavailable.
        # A selected course must remain manageable from its own Classroom resource fields.
        argv = ["info", "course", course_id]
        if include_owner_email:
            argv.append("owneremail")
        if include_aliases:
            argv.append("aliases")
        return argv + ["fields", ",".join(fields or COURSE_DETAIL_FIELDS), "formatjson"]

    @staticmethod
    def create_course(
        name: str,
        teacher: str,
        *,
        alias: str = "",
        section: str = "",
        room: str = "",
        description_heading: str = "",
        description: str = "",
        state: str = "provisioned",
    ) -> List[str]:
        argv = ["create", "course"]
        if alias:
            argv += ["alias", alias]
        argv += ["name", name, "teacher", teacher]
        if section:
            argv += ["section", section]
        if room:
            argv += ["room", room]
        if description_heading:
            argv += ["descriptionheading", description_heading]
        if description:
            argv += ["description", description]
        argv += ["state", _validate_course_state(state)]
        return argv

    @staticmethod
    def update_course(
        course_id: str,
        *,
        name: str,
        section: str = "",
        room: str = "",
        description_heading: str = "",
        description: str = "",
    ) -> List[str]:
        # Send the complete editable metadata set so clearing a field is intentional and testable.
        return [
            "update", "course", course_id,
            "name", name,
            "section", section,
            "room", room,
            "descriptionheading", description_heading,
            "description", description,
        ]

    @staticmethod
    def update_course_roster_metadata(
        course_id: str,
        *,
        name: str,
        section: str = "",
        room: str = "",
    ) -> List[str]:
        """Update only fields controlled by a OneRoster snapshot.

        Descriptions are intentionally omitted so district rostering never clears
        teacher-authored content.
        """
        return [
            "update",
            "course",
            course_id,
            "name",
            name,
            "section",
            section,
            "room",
            room,
        ]

    @staticmethod
    def update_course_state(course_id: str, state: str) -> List[str]:
        return ["update", "course", course_id, "state", _validate_course_state(state)]

    @staticmethod
    def transfer_course_owner(course_id: str, teacher: str) -> List[str]:
        # GAM adds a non-teacher as co-teacher before promoting them to owner.
        return ["update", "course", course_id, "teacher", teacher]

    @staticmethod
    def print_course_participants(course_id: str, role: str) -> List[str]:
        return [
            "print", "course-participants", "course", course_id,
            "show", _validate_course_role(role), "formatjson",
        ]

    @staticmethod
    def print_course_participants_many(
        course_ids: Sequence[str],
        role: str = "all",
    ) -> List[str]:
        """Read rosters for an exact bounded course set in one GAM process."""
        selected = [str(course_id).strip() for course_id in course_ids]
        if not selected or len(selected) > 50:
            raise ValueError("course_ids must contain between 1 and 50 courses")
        if any(not course_id or any(ch in course_id for ch in "\r\n\x00") for course_id in selected):
            raise ValueError("course_ids contain an invalid Classroom course reference")
        normalized_role = str(role or "").strip().casefold()
        if normalized_role not in {"all", "teachers", "students"}:
            raise ValueError("invalid Classroom roster role")
        argv = ["print", "course-participants"]
        for course_id in selected:
            argv += ["course", course_id]
        return argv + ["show", normalized_role, "formatjson"]

    @staticmethod
    def print_course_participants_file(
        path: str,
        role: str = "all",
    ) -> List[str]:
        """Read exact course rosters from one private GAM ``CourseEntity`` file."""

        value = str(path or "")
        if not value or any(character in value for character in "\r\n\x00"):
            raise ValueError("invalid Classroom roster selector path")
        normalized_role = str(role or "").strip().casefold()
        if normalized_role not in {"all", "teachers", "students"}:
            raise ValueError("invalid Classroom roster role")
        return [
            "print",
            "course-participants",
            "course",
            "file",
            value,
            "show",
            normalized_role,
            "formatjson",
        ]

    @staticmethod
    def add_course_participant(course_id: str, role: str, email: str) -> List[str]:
        return ["course", course_id, "add", _validate_course_role(role), email]

    @staticmethod
    def remove_course_participant(course_id: str, role: str, email: str) -> List[str]:
        return ["course", course_id, "remove", _validate_course_role(role), email]

    @staticmethod
    def batch_file(path: str, *, show_commands: bool = False) -> List[str]:
        """Run one private, locally generated GAM batch file."""
        value = str(path or "")
        if not value or any(character in value for character in "\r\n\x00"):
            raise ValueError("invalid GAM batch path")
        return ["batch", value, "showcmds", "true" if show_commands else "false"]

    @staticmethod
    def batch_line(argv: Sequence[str]) -> str:
        """Serialize one allowlisted argv vector as a GAM batch line.

        ``shlex.join`` quotes each argument independently. Newlines and NUL bytes
        are rejected before serialization so no value can inject a second command.
        """
        command = [str(argument) for argument in argv]
        if not command:
            raise ValueError("a GAM batch command cannot be empty")
        if any(any(character in argument for character in "\r\n\x00") for argument in command):
            raise ValueError("GAM batch arguments cannot contain control-line characters")
        return "gam " + shlex.join(command)

    # --- users (mutating) -------------------------------------------------------------
    @staticmethod
    def create_user(
        email: str,
        first_name: str,
        last_name: str,
        password: str,
        change_password: bool = True,
        org_unit: Optional[str] = None,
    ) -> List[str]:
        argv = [
            "create", "user", email,
            "firstname", first_name,
            "lastname", last_name,
            "password", password,
            "changepassword", "on" if change_password else "off",
        ]
        if org_unit:
            argv += ["org", org_unit]
        return argv

    @staticmethod
    def update_user(email: str, **fields: str) -> List[str]:
        """Generic user update. ``fields`` are GAM attribute/value pairs, e.g. ``firstname='Jo'``."""
        argv = ["update", "user", email]
        for key, value in fields.items():
            argv += [key, str(value)]
        return argv

    @staticmethod
    def update_organization(email: str, title: str = "", department: str = "") -> List[str]:
        """Set the primary organization's title + department (here, department holds the store).

        GAM's ``organization`` replaces the primary org, so we always pass both fields together (the
        editor pre-fills the current values) to avoid clearing one while changing the other.
        """
        return ["update", "user", email, "organization", "title", title, "department", department, "primary"]

    @staticmethod
    def set_suspended(email: str, suspended: bool) -> List[str]:
        # `update user ... suspended on/off` is the canonical, version-stable form.
        return ["update", "user", email, "suspended", "on" if suspended else "off"]

    # --- calendar access ---------------------------------------------------------------
    @staticmethod
    def print_calendar_acls(email: str, calendar: str = "primary") -> List[str]:
        return ["user", email, "print", "calendaracls", calendar, "formatjson"]

    @staticmethod
    def add_calendar_acl(email: str, target: str, role: str = "reader", calendar: str = "primary") -> List[str]:
        # `target` is a scope: a bare email = a user; pass "group:<email>"/"domain"/"default" as-is.
        return ["user", email, "add", "calendaracls", calendar, role, target]

    @staticmethod
    def delete_calendar_acl(email: str, scope: str, calendar: str = "primary") -> List[str]:
        return ["user", email, "delete", "calendaracls", calendar, scope]

    # --- calendars / resources / events ------------------------------------------------
    @staticmethod
    def print_resources(query: str = "") -> List[str]:
        argv = ["print", "resources", "fields", "id,name,email,resourcetype,buildingid"]
        if query:
            argv += ["query", query]
        argv.append("formatjson")
        return argv

    @staticmethod
    def print_user_calendars(email: str) -> List[str]:
        return ["user", email, "print", "calendars", "fields", "id,summary,accessrole,primary", "formatjson"]

    @staticmethod
    def print_all_calendars() -> List[str]:
        # Every user's calendar list (incl. secondary calendars) — filtered by name in Python.
        return ["all", "users", "print", "calendars", "fields", "id,summary,accessrole,primary", "formatjson"]

    @staticmethod
    def print_calendar_acls_cal(calendar_id: str) -> List[str]:
        # Standalone form: ACLs for ANY calendar id (room/secondary) via admin access.
        return ["calendars", calendar_id, "print", "calendaracls", "formatjson"]

    @staticmethod
    def add_calendar_acl_cal(
        calendar_id: str, scope: str, role: str = "reader", send_notifications: bool = False
    ) -> List[str]:
        # Standalone admin form (same auth path as `calendars <id> print calendaracls`) — no owner
        # impersonation, no formatjson. `scope`: bare email = user; pass "group:<email>"/"domain"/
        # "default" through unchanged. Notifications default OFF: the subscribe makes it visibly
        # appear (the whole point is people miss the sharing email).
        argv = ["calendars", calendar_id, "add", "calendaracls", role, scope]
        if send_notifications:
            argv += ["sendnotifications", "true"]
        return argv

    @staticmethod
    def delete_calendar_acl_cal(calendar_id: str, scope: str) -> List[str]:
        return ["calendars", calendar_id, "delete", "calendaracls", scope]

    @staticmethod
    def subscribe_calendar(email: str, calendar_id: str, selected: bool = True) -> List[str]:
        # Makes the calendar appear in the recipient's sidebar; runs as the recipient.
        argv = ["user", email, "add", "calendars", calendar_id]
        if selected:
            argv += ["selected", "true"]
        return argv

    @staticmethod
    def remove_calendar(owner: str, calendar_id: str) -> List[str]:
        # PERMANENTLY delete a secondary calendar, acting as an owner (Calendars.delete).
        # GAM footgun: `remove calendars` deletes the calendar for everyone; `delete calendars`
        # would only unsubscribe this user (CalendarList.delete). Must impersonate an owner.
        return ["user", owner, "remove", "calendars", calendar_id]

    @staticmethod
    def unsubscribe_calendar(email: str, calendar_id: str) -> List[str]:
        # Just remove the calendar from one user's list (CalendarList.delete) — the calendar lives on.
        return ["user", email, "delete", "calendars", calendar_id]

    _EVENT_FIELDS = "id,summary,start,end,recurrence,recurringeventid,organizer,creator,status"

    @staticmethod
    def print_events(calendar_id: str, query: str = "", after: str = "", before: str = "") -> List[str]:
        argv = ["calendars", calendar_id, "print", "events"]
        if query:
            argv += ["query", query]
        if after:
            argv += ["after", after]
        if before:
            argv += ["before", before]
        argv += ["fields", GAMCommands._EVENT_FIELDS, "formatjson"]
        return argv

    @staticmethod
    def get_event(calendar_id: str, event_id: str) -> List[str]:
        # Re-read one event by id for the delete preview.
        return ["calendars", calendar_id, "print", "events", "eventid", event_id,
                "fields", GAMCommands._EVENT_FIELDS, "formatjson"]

    @staticmethod
    def delete_event(calendar_id: str, event_id: str, doit: bool = True) -> List[str]:
        # GAM dry-runs `delete events` without `doit`. Deleting a recurring master id drops the series.
        argv = ["calendars", calendar_id, "delete", "events", "eventid", event_id]
        if doit:
            argv.append("doit")
        argv += ["sendupdates", "none"]
        return argv

    # --- lifecycle (offboarding) -------------------------------------------------------
    @staticmethod
    def reset_password(email: str) -> List[str]:
        # Random password + no change-prompt: locks sign-in while the mailbox stays live.
        return ["update", "user", email, "password", "random", "changepassword", "off"]

    @staticmethod
    def signout_user(email: str) -> List[str]:
        return ["user", email, "signout"]

    @staticmethod
    def create_datatransfer(old_owner: str, service: str, new_owner: str) -> List[str]:
        # `service` is a <DataTransferServiceList>: one service ("drive" | "calendar") OR a
        # comma-joined list ("drive,calendar") that rides as ONE argv element. Passing both in a
        # single transfer avoids Google's 409 "transfer already in progress" when two separate
        # transfers for the same user overlap.
        return ["create", "datatransfer", old_owner, service, new_owner]

    @staticmethod
    def print_datatransfers(old_owner: str = "") -> List[str]:
        # Transfers are async; the CSV carries `overallTransferStatusCode` (completed/inProgress/...).
        argv = ["print", "datatransfers"]
        if old_owner:
            argv += ["olduser", old_owner]
        return argv

    @staticmethod
    def remove_all_calendar_acls(email: str) -> List[str]:
        # Remove the departing user from EVERY other user's primary calendar (GAM loops all users).
        return ["all", "users", "delete", "calendaracls", "primary", email]

    @staticmethod
    def add_calendar_event(
        calendar: str, summary: str, start: str, end: str, description: str = "", attendee: str = ""
    ) -> List[str]:
        argv = ["user", calendar, "add", "event", "primary",
                "summary", summary, "start", "allday", start, "end", "allday", end]
        if description:
            argv += ["description", description]
        if attendee:
            argv += ["attendee", attendee]
        return argv

    @staticmethod
    def delete_user(email: str) -> List[str]:
        return ["delete", "user", email]

    @staticmethod
    def undelete_user(email: str) -> List[str]:
        return ["undelete", "user", email]

    # --- onboarding runbook: a Google Tasks checklist on the assignee + a welcome email ---
    @staticmethod
    def create_tasklist(assignee: str, title: str) -> List[str]:
        # `returnidonly` so we get just the new tasklist id back to attach tasks to.
        return ["user", assignee, "create", "tasklist", "title", title, "returnidonly"]

    @staticmethod
    def create_task(assignee: str, tasklist_id: str, title: str, notes: str = "") -> List[str]:
        argv = ["user", assignee, "create", "task", tasklist_id, "title", title]
        if notes:
            argv += ["notes", notes]
        return argv

    @staticmethod
    def send_email(to: str, subject: str, body: str, html: bool = True) -> List[str]:
        argv = ["sendemail", "to", to, "subject", subject, "message", body]
        if html:
            argv += ["html"]
        return argv

    # --- gmail: signature / delegate / forwarding / vacation --------------------------
    @staticmethod
    def set_signature(email: str, signature: str, html: bool = True) -> List[str]:
        argv = ["user", email, "signature", signature]
        if html:
            argv.append("html")
        return argv

    @staticmethod
    def show_signature(email: str) -> List[str]:
        # `show signature` returns text (no formatjson).
        return ["user", email, "show", "signature"]

    @staticmethod
    def add_delegate(email: str, delegate: str) -> List[str]:
        return ["user", email, "add", "delegate", delegate]

    @staticmethod
    def remove_delegate(email: str, delegate: str) -> List[str]:
        return ["user", email, "delete", "delegate", delegate]

    @staticmethod
    def print_delegates(email: str) -> List[str]:
        # NOTE: `print delegates` does NOT support `formatjson` (it errors "Invalid argument").
        # Output is plain CSV with a `delegateAddress` column.
        return ["user", email, "print", "delegates"]

    @staticmethod
    def set_vacation(
        email: str,
        subject: str,
        message: str,
        html: bool = True,
        start: Optional[str] = None,
        end: Optional[str] = None,
        contacts_only: bool = False,
        domain_only: bool = False,
    ) -> List[str]:
        argv = ["user", email, "vacation", "on", "subject", subject, "message", message]
        if html:
            argv.append("html")
        if contacts_only:
            argv.append("contactsonly")
        if domain_only:
            argv.append("domainonly")
        if start:
            argv += ["start", start]
        if end:
            argv += ["end", end]
        return argv

    @staticmethod
    def vacation_off(email: str) -> List[str]:
        return ["user", email, "vacation", "off"]

    # --- gmail: forwarding ------------------------------------------------------------
    FORWARD_ACTIONS = ("keep", "archive", "markread", "trash", "delete")

    @staticmethod
    def add_forwarding_address(email: str, address: str) -> List[str]:
        return ["user", email, "add", "forwardingaddress", address]

    @staticmethod
    def delete_forwarding_address(email: str, address: str) -> List[str]:
        return ["user", email, "delete", "forwardingaddress", address]

    @staticmethod
    def print_forwarding_addresses(email: str) -> List[str]:
        return ["user", email, "print", "forwardingaddresses"]

    @staticmethod
    def set_forward(email: str, address: str, action: str = "keep") -> List[str]:
        # Forward to an already-added/verified forwarding address; `action` is what to do with the
        # original copy (keep | archive | markread | trash | delete).
        if action not in GAMCommands.FORWARD_ACTIONS:
            raise ValueError(f"invalid forward action: {action!r}")
        return ["user", email, "forward", "on", action, address]

    @staticmethod
    def forward_off(email: str) -> List[str]:
        return ["user", email, "forward", "off"]

    # --- message search (read-only) ---------------------------------------------------
    MESSAGE_DETAIL = ("Headers", "Headers + body", "Summary")
    MESSAGE_CAP = 50   # bound an empty/broad query so it can't dump a whole mailbox into the table

    @staticmethod
    def search_messages(email: str, query: str = "", detail: str = "Headers") -> List[str]:
        """Find messages in one mailbox by Gmail search and show their headers (read-only).

        ``query`` is a Gmail search string (e.g. ``rfc822msgid:<id>``, ``from:… after:2026/06/23
        before:2026/06/24``) and is passed as a *single* argv element — never shell-spliced. ``headers
        all`` surfaces ``Return-Path``/``Received`` so an envelope/bounce address (e.g. an Amazon SES
        sender) is visible. Spam/Trash are included (bounces often land there) and results are capped.
        """
        # NB: `print messages` has NO `formatjson` mode (unlike `print users` et al.) — it emits CSV.
        # Appending formatjson makes GAM reject the command ("format json is invalid"). We let it
        # return CSV and parse that (parse_records handles CSV).
        argv = ["user", email, "print", "messages"]
        if query:
            argv += ["query", query]
        argv += ["includespamtrash", "max_to_print", str(GAMCommands.MESSAGE_CAP)]
        if detail == "Summary":
            argv += ["showlabels", "showdate", "showsize", "showsnippet"]
        elif detail == "Headers + body":
            argv += ["headers", "all", "showbody", "showlabels", "showdate"]
        else:  # "Headers" (default)
            argv += ["headers", "all", "showlabels", "showdate"]
        return argv

    # --- aliases ----------------------------------------------------------------------
    @staticmethod
    def create_user_alias(alias: str, email: str) -> List[str]:
        return ["create", "alias", alias, "user", email]

    @staticmethod
    def delete_alias(alias: str) -> List[str]:
        return ["delete", "alias", alias]

    @staticmethod
    def todrive_args(user: str = "", title: str = "") -> List[str]:
        # Append to a `print …` command to export its CSV to a Google Sheet. `user` = whose Drive
        # owns the sheet (blank = the admin/oauth account's Drive); `title` names it.
        argv = ["todrive"]
        if user:
            argv += ["tduser", user]
        if title:
            argv += ["tdtitle", title]
        return argv

    @staticmethod
    def show_vacation(email: str) -> List[str]:
        # `show vacation` does NOT support formatjson — returns parseable text.
        return ["user", email, "show", "vacation"]

    # --- groups -----------------------------------------------------------------------
    @staticmethod
    def print_groups(fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["print", "groups", "fields", ",".join(fields or GROUP_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def create_group(email: str, name: str = "", description: str = "") -> List[str]:
        argv = ["create", "group", email]
        if name:
            argv += ["name", name]
        if description:
            argv += ["description", description]
        return argv

    @staticmethod
    def print_group_members(
        group: str,
        fields: Optional[Sequence[str]] = None,
    ) -> List[str]:
        argv = ["print", "group-members", "group", group]
        if fields:
            argv += ["fields", ",".join(fields)]
        return argv + ["formatjson"]

    @staticmethod
    def print_groups_member(email: str) -> List[str]:
        # Groups that <email> belongs to. Returns CSV with an `email` column.
        return ["print", "groups", "member", email]

    @staticmethod
    def add_group_member(group: str, member: str, role: str = "member") -> List[str]:
        role = _validate_role(role)
        return ["update", "group", group, "add", role, member]

    @staticmethod
    def remove_group_member(group: str, member: str) -> List[str]:
        return ["update", "group", group, "remove", member]


def _validate_role(role: str) -> str:
    role = (role or "member").strip().lower()
    if role not in GROUP_ROLES:
        raise ValueError(f"invalid group role {role!r}; expected one of {GROUP_ROLES}")
    return role


def _validate_course_state(state: str) -> str:
    value = (state or "").strip().lower()
    if value not in COURSE_STATES:
        raise ValueError(f"invalid Classroom course state {state!r}; expected one of {COURSE_STATES}")
    return value


def _validate_course_role(role: str) -> str:
    value = (role or "").strip().lower()
    if value not in COURSE_ROSTER_ROLES:
        raise ValueError(
            f"invalid Classroom roster role {role!r}; expected one of {COURSE_ROSTER_ROLES}"
        )
    return value


def build_user_query(search: str = "", include_suspended: bool = True) -> Optional[str]:
    """Translate a free-text search box into a Directory API query string.

    Empty search returns ``None`` (list everyone). A bare token matches email/name prefixes.
    """
    clauses: List[str] = []
    search = (search or "").strip()
    if search:
        # Directory API supports prefix matching with '*'. Match common fields.
        token = search.replace("'", "")
        clauses.append(f"email:{token}* givenName:{token}* familyName:{token}*")
    if not include_suspended:
        clauses.append("isSuspended=false")
    return " ".join(clauses) if clauses else None
