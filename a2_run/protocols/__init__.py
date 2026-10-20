"""``protocols`` — the A2A layer for a2a (pair) runs.

Message envelope, agent card, the JSON-RPC binding constants shared by hub and
host, task states, and the channel seam. ``CHANNEL_REGISTRY`` is what
``a2a.channel`` selects: ``a2a`` (spec envelope) or ``direct`` (bare ``from:``
line) — same wire and body, so the pair is a controlled comparison.
"""
from __future__ import annotations

from . import jsonrpc, task_state
from .agent_card import A2A_PROTOCOL_VERSION, AgentCard, AgentSkill
from .channel import A2AChannel, BaseChannel, DirectChannel
from .message import ROLE_AGENT, ROLE_USER, A2AMessage, Part

CHANNEL_REGISTRY: dict[str, type[BaseChannel]] = {
    DirectChannel.name: DirectChannel,
    A2AChannel.name: A2AChannel,
}

__all__ = [
    "A2AMessage",
    "Part",
    "ROLE_USER",
    "ROLE_AGENT",
    "AgentCard",
    "AgentSkill",
    "A2A_PROTOCOL_VERSION",
    "BaseChannel",
    "DirectChannel",
    "A2AChannel",
    "CHANNEL_REGISTRY",
    "jsonrpc",
    "task_state",
]
