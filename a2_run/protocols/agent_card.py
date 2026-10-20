"""Agent Cards — identity + capability descriptors, A2A-schema aligned.

A card is what a deployed A2A agent publishes about itself — how one agent
learns another exists and what it can do, with no evaluation framing. Fields
track the A2A ``AgentCard``/``AgentSkill`` schema
(https://a2a-protocol.org/v1.0.0/specification/); each role's hub serves its
own card at the well-known URI (§14.3).

Discovery is by direct configuration (the task declares each role's card):
A2A defines no directory operation, and its docs state that "the current A2A
specification does not prescribe a standard API for curated registries".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .jsonrpc import PROTOCOL_VERSION, agent_interface

A2A_PROTOCOL_VERSION = PROTOCOL_VERSION


@dataclass(frozen=True)
class AgentSkill:
    """One capability an agent advertises (A2A ``AgentSkill``).

    ``id`` is the stable machine handle; ``name``/``description`` are the
    human-facing summary another agent reads to decide whether this agent is
    the right one to ask. ``examples`` are sample prompts/queries the skill
    handles — useful context for a peer composing its first message.
    """

    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ()
    output_modes: tuple[str, ...] = ()

    def to_a2a_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
        }
        if self.examples:
            out["examples"] = list(self.examples)
        if self.input_modes:
            out["inputModes"] = list(self.input_modes)
        if self.output_modes:
            out["outputModes"] = list(self.output_modes)
        return out


@dataclass(frozen=True)
class AgentCard:
    """Identity + capabilities of one agent (A2A ``AgentCard``).

    ``name`` doubles as the routable id (how a peer addresses this agent).
    ``skills`` is what a peer inspects to know what this agent can do.
    ``capabilities`` mirrors the A2A capability flags (``streaming``,
    ``pushNotifications``, …); empty/default here since the relay wire has none.
    """

    name: str
    description: str
    version: str = "0.1.0"
    protocol_version: str = A2A_PROTOCOL_VERSION
    capabilities: dict[str, Any] = field(default_factory=dict)
    skills: tuple[AgentSkill, ...] = ()

    def to_a2a_json(self, *, url: str = "") -> dict[str, Any]:
        """Render a spec-shaped A2A AgentCard document.

        Given a hub base ``url``, the card declares a ``supportedInterfaces``
        entry (§4.4.6). Omitted for the peer directory, which says who an agent
        may talk to, not where to reach it — the host relays.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "protocolVersion": self.protocol_version,
            "capabilities": dict(self.capabilities),
            "skills": [s.to_a2a_json() for s in self.skills],
        }
        if url:
            out["supportedInterfaces"] = [agent_interface(url)]
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentCard":
        """Build a card from a plain dict (task declarations, config).

        Accepts both snake_case (our dataclass) and the A2A camelCase
        (``protocolVersion``) key for the protocol version, so a task can
        hand-author either shape.
        """
        skills_raw = raw.get("skills") or ()
        skills = tuple(
            AgentSkill(
                id=str(s["id"]),
                name=str(s.get("name") or s["id"]),
                description=str(s.get("description") or ""),
                tags=tuple(s.get("tags") or ()),
                examples=tuple(s.get("examples") or ()),
                input_modes=tuple(s.get("input_modes") or s.get("inputModes") or ()),
                output_modes=tuple(s.get("output_modes") or s.get("outputModes") or ()),
            )
            for s in skills_raw
        )
        return cls(
            name=str(raw["name"]),
            description=str(raw.get("description") or ""),
            version=str(raw.get("version") or "0.1.0"),
            protocol_version=str(
                raw.get("protocol_version") or raw.get("protocolVersion") or A2A_PROTOCOL_VERSION
            ),
            capabilities=dict(raw.get("capabilities") or {}),
            skills=skills,
        )


