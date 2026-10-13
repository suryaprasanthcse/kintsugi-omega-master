"""Onboarding through the control plane (Stage 10): the ``Onboarding`` workflow end to end.

``run_onboarding`` starts the ``Onboarding`` workflow (§5.3.1) for a host registered with the
onboarding desk and returns its seven ``StepResult``s. Every step is an ``onboard_step``
activity whose handler is the desk, exactly as the W crew runs it (``OmegaCrew.do_onboard_step``
with the runtime's desk):

- with ``target`` (a running Temporal frontend: W's), the workflow is started there and W's own
  crew worker runs every step; the case must be registered in that runtime's desk
  (``<state>/onboarding``, see ``kintsugi.crew.runtime``);
- without one, an in-process Temporal test server (time-skipping, no network) runs the workflow
  with a worker that serves ``onboard_step`` from this desk, the same server the control-plane
  tests use.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from kintsugi.control import contracts as c
from kintsugi.control.activities import CREW, OmegaActivities, StepContext
from kintsugi.onboard.desk import OnboardingDesk


class DeskCrew:
    """The crew port for onboarding only: ``onboard_step`` → the desk; nothing else."""

    def __init__(self, desk: OnboardingDesk) -> None:
        self.desk = desk

    def handle(self, ctx: StepContext, *args: Any) -> Any:
        if ctx.name != "onboard_step":
            raise NotImplementedError(f"the onboarding crew has no {ctx.name} step")
        host, step = args
        return self.desk.step(str(host), str(step))


async def _run(desk: OnboardingDesk, host: str, target: str | None) -> list[c.StepResult]:
    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from kintsugi.control.workflows import Onboarding

    config = c.ControlConfig()
    workflow_id = f"onboarding-{host}-{uuid.uuid4().hex[:8]}"
    if target is not None:  # W's crew worker (with the runtime's desk) runs the steps
        client = await Client.connect(target)
        return await client.execute_workflow(Onboarding.run, args=[host, config],
                                             id=workflow_id, task_queue=CREW)  # fmt: skip
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        acts = OmegaActivities(DeskCrew(desk))
        async with Worker(env.client, task_queue=CREW, workflows=[Onboarding],
                          activities=[acts.onboard_step]):  # fmt: skip
            return await env.client.execute_workflow(Onboarding.run, args=[host, config],
                                                     id=workflow_id, task_queue=CREW)  # fmt: skip
    finally:
        await env.shutdown()


def run_onboarding(desk: OnboardingDesk, host: str, *, target: str | None = None
                   ) -> list[c.StepResult]:  # fmt: skip
    """O1–O7 for a registered host through the ``Onboarding`` workflow."""
    return asyncio.run(_run(desk, host, target))


def record(results: list[c.StepResult]) -> dict[str, Any]:
    """The workflow's results as the onboarding record (``O<n>_<name>``, like ``pipeline``)."""
    from kintsugi.onboard.desk import NAMES

    return {f"{r.name}_{NAMES.get(r.name, r.name)}": {"ok": r.ok, **r.detail} for r in results}
