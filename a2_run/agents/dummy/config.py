"""DummyConfig — knobs for the scripted a2a smoke-test agent.

Standalone dataclass (no shared base), per a2-bench convention. The dummy agent
runs no LLM, so ``model`` is accepted only because the yaml loader maps a
top-level ``model:`` into ``config["model"]`` (and ``build_config`` would
otherwise drop it); it is never used.

What the agent does: acts as a scripted responder in a pair (a2a) run —
each turn it acks/replays a reply and, after ``a2a_end_after`` turns, writes
the end-conversation marker — exercising the pair lifecycle without spending
model tokens.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class DummyConfig:
    """Tunables for :class:`DummyDeployer`."""

    name: ClassVar[str] = "dummy"

    model: str = "none"
    """Unused. Present so a ``model:`` line in the agent yaml doesn't surprise
    anyone — the dummy agent makes no model calls."""

    # ---- a2a (pair-mode) knobs ----

    a2a_replies: tuple[str, ...] = ()
    """Scripted turn replies for a2a runs, cycled by turn index. Empty
    (default) ⇒ generate an ack that quotes the received message, which
    makes host-side relay verification trivial."""

    a2a_end_after: int = 0
    """After this many of its own sends, the assigned dummy POSTs
    ``/task_complete`` to its hub. ``0`` (default) ⇒ never end — the pair
    run stops on max_messages instead."""
