# Live verification status

Every write is audited, so this list is derived from real audit logs rather than memory. "Confirmed
live" means the operation has succeeded at least once against a production Google Workspace domain.

**Confirmed live:** calendar share (ACL) · calendar auto-subscribe (making a shared calendar appear
in someone's sidebar) · add calendar event · delete calendar · add delegate · remove group member ·
reset password · set organization fields · set signature · set vacation · transfer data · plus all
reads (a read-only pass over the parsers ships as `scripts/acceptance.py`).

**Not yet confirmed live** — treat as unproven and test on a throwaway target first: unshare a
calendar (remove ACL) · remove delegate · clear vacation · add group member · sign out everywhere ·
delete event · delete user · the group fan-out of a calendar share (the individual calls it makes —
ACL add and subscribe — are each confirmed live, but the group expansion itself is not) · and the two
offboarding repairs described below.

**Known-good repairs awaiting live re-run.** Two offboarding bugs were found in real audit logs and
fixed, but the fixes have not themselves been exercised live yet: Drive and calendar are now
transferred in a *single* data-transfer call (two separate calls collided with a `409 conflict`),
and "remove from everyone's calendars" now tolerates the `cannotChangeOwnAcl` error that used to
abort the sweep.
