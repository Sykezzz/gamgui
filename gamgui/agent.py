"""Headless background entry point for scheduled GamGUI policies."""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional, Sequence

from .core.activity import activity_registry
from .core.classroom_access import (
    EntitlementService,
    EntitlementStore,
    PolicyStatus,
)
from .core.connectors.gam_connector import GAMConnector
from .core.gam.runner import GAMRunner
from .core.secrets.vault import SecretsVault


async def run_classroom_teachers(policy_id: str, *, scheduled: bool = False) -> int:
    store = EntitlementStore()
    policy = await asyncio.to_thread(store.get_policy, policy_id)
    if policy is None:
        return 2
    if policy.status == PolicyStatus.DISABLED.value or not policy.schedule_enabled:
        return 0
    if policy.status != PolicyStatus.ACTIVE.value:
        await asyncio.to_thread(
            store.record_policy_result,
            policy.id,
            status="held",
            message="The policy is not active; preview and approve it in GamGUI.",
        )
        return 2
    if policy.configuration_hash != policy.approved_config_hash:
        await asyncio.to_thread(
            store.record_policy_result,
            policy.id,
            status="held",
            message="The policy configuration changed; preview and approve it in GamGUI.",
        )
        return 2

    lease = activity_registry.try_acquire("classroom-teacher-entitlement")
    if lease is None:
        await asyncio.to_thread(
            store.record_policy_result,
            policy.id,
            status="skipped-busy",
            message="Another administrative activity was active; no membership changed.",
        )
        return 0
    try:
        try:
            vault = SecretsVault()
            runner = GAMRunner(vault=vault)
            connector = GAMConnector(runner=runner, domain=policy.domain)
            connection = await connector.test()
            if not connection.ok:
                await asyncio.to_thread(
                    store.record_policy_result,
                    policy.id,
                    status="held",
                    message="The Workspace connection is not ready for scheduled reconciliation.",
                )
                return 2
            service = EntitlementService(connector, policy.domain, store)
            if policy.pending_plan_id:
                pending = await asyncio.to_thread(
                    store.get_plan, policy.pending_plan_id
                )
                if pending is not None and pending.status == "interrupted":
                    resumed = await service.resume_interrupted(pending.id)
                    if resumed is not None:
                        return 0 if resumed.status == "completed" else 2
            plan = await service.plan(policy, approval_required=False)
            if plan.status == "held":
                await asyncio.to_thread(
                    store.record_policy_result,
                    policy.id,
                    status="held",
                    message=plan.hold_reason,
                    preserve_pending=True,
                )
                return 2
            result = await service.apply(plan.id)
            return 0 if result.status == "completed" else 2
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.to_thread(
                store.record_policy_result,
                policy.id,
                status="held",
                message=(
                    "Scheduled reconciliation failed safely; "
                    "no further membership changes were attempted."
                ),
            )
            return 2
    finally:
        lease.release()


def _arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gamgui-agent")
    subparsers = parser.add_subparsers(dest="task", required=True)
    classroom = subparsers.add_parser("classroom-teachers")
    classroom.add_argument("--policy-id", required=True)
    classroom.add_argument("--scheduled", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _arguments(argv)
    if args.task == "classroom-teachers":
        return asyncio.run(
            run_classroom_teachers(args.policy_id, scheduled=args.scheduled)
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
