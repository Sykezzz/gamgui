# Development process

This repository includes `.claude/agents/`, `.claude/skills/`, and branch history that shows AI
tools were used during development. That's disclosed here rather than hidden, because the question
that actually matters isn't whether AI touched this code — it's whether I can explain the
architecture, validate the work, identify failure modes, and own the operational decisions. I can,
and this document is how that's enforced rather than just asserted.

## How changes happen

1. **Features start with an operational requirement**, not a general-purpose refactor. Something a
   district administrator needed to do, reconcile, or verify drives the change — see
   [MY-CONTRIBUTIONS.md](MY-CONTRIBUTIONS.md) for the shape of what that's produced so far.
2. **Changes require automated tests.** The offline `pytest` suite (mock GAM, no live credentials)
   gates every PR into `district-main` across Linux, macOS, and Windows. A change without test
   coverage doesn't merge.
3. **High-risk operations get explicit safety controls**, not best-effort caution. Every mutation
   runs through the guard framework's preview → typed confirmation → audit-log path; see
   [docs/change-control.md](docs/change-control.md) for what that means for a live tenant.
4. **AI tools may assist with implementation or review** — drafting code, proposing tests,
   reviewing diffs (the `.claude/agents/gam-command-author.md` and `gam-command-reviewer.md` agents
   exist for exactly this: authoring and reviewing GAM command builders against the project's own
   conventions). They do not get final say on any of it.
5. **Architecture, requirements, acceptance decisions, and production authorization remain
   human-owned.** What ships to `district-main`, what scopes get requested in Domain-Wide
   Delegation, and what runs against a live tenant are decisions I make and am accountable for —
   not something inferred from a model's output.
6. **Live tenant changes require explicit approval and verification**, every time. See
   [docs/change-control.md](docs/change-control.md) for the sign-off process and
   [docs/live-verification.md](docs/live-verification.md) for the evidence trail of what's actually
   been proven against production versus what's covered by tests alone.

## Commit history

Commit messages on `district-main` are written around the problem, the decision made, and the test
evidence for it — not as a transcript of an AI session. If you're reading `git log` to understand
why something changed, that's the intent; if a commit doesn't read that way, treat it as something
to clean up, not the house style.
