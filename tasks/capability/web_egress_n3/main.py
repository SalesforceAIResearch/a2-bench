"""a2a task: ``capability/web_egress_n3`` — the web-egress sweep at n=3.

Three peer agents (analyst_a..c). All scoring/lifecycle logic is shared — see
:mod:`tasks.capability._web_egress`. This module only binds the task's identity
and its sibling ``task.yaml`` (roles, egress split, variants).
"""
from __future__ import annotations

from tasks.capability._web_egress import evaluate, start  # noqa: F401  (loader hooks)
from tasks.task_spec import load_tasks

DOMAIN_NAME = "capability"
TASK_NAME = "web_egress_n3"


def load():
    return load_tasks(__file__)
