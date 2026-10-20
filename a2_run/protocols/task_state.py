"""A2A ``TaskState`` (v1.0 §4.1.3) — the states a run can reach.

The spec's interrupted states (``INPUT_REQUIRED``, ``AUTH_REQUIRED``) have no
analogue in a two-agent run with no human in the loop, so they are omitted.
"""
from __future__ import annotations

COMPLETED = "TASK_STATE_COMPLETED"   # the assigned agent finished its own task
CANCELED = "TASK_STATE_CANCELED"     # terminated from outside (peer left, host)
FAILED = "TASK_STATE_FAILED"
SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"

#: Benchmark termination reason → task state. The two vocabularies stay
#: separate: the reason records WHY the driver stopped, including cases the
#: protocol has no state for (``max_messages``). ``max_rounds`` is the
#: pre-rename spelling, kept so old artifacts still resolve.
REASON_TO_STATE = {
    "task_complete": COMPLETED,
    "max_messages": CANCELED,
    "max_rounds": CANCELED,
    "wall_time": CANCELED,
    "all_agents_exited": FAILED,
    "all_agents_blocked": FAILED,
    "invalid_agent_response": FAILED,
    "agent_failed": FAILED,
}


def state_for_reason(reason: str | None) -> str:
    return REASON_TO_STATE.get(reason or "", FAILED)
