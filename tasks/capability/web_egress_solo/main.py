"""a2a task: ``capability/web_egress_solo`` — the web-egress sweep at n=1.

A genuine single-role run: analyst_a alone, no peer. The intrinsic-behavior
baseline for the paired sweep points — it isolates whether the confused-deputy
egress escape emerges on its own, absent any teammate to route work to or
misreport to. All scoring/lifecycle logic is shared — see
:mod:`tasks.capability._web_egress`. This module only binds the task's identity
and its sibling ``task.yaml`` (role, egress, variants).
"""
from __future__ import annotations

from tasks.capability._web_egress import evaluate, start  # noqa: F401  (loader hooks)
from tasks.task_spec import load_tasks

DOMAIN_NAME = "capability"
TASK_NAME = "web_egress_solo"


def load():
    return load_tasks(__file__)
