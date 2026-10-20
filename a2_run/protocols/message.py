"""A2AMessage — the inter-agent message envelope (A2A v1.0 ``Message``).

The driver builds one per hop; ``to_a2a_json`` is the JSON-RPC ``SendMessage``
payload. Fields track §4.1.4/§4.1.6 of
https://a2a-protocol.org/v1.0.0/specification/.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

# §4.1.5 defines Role by wire direction (client→server vs server→client), but
# every party in this benchmark is an agent — never a human "user" — so
# messages are authored as ROLE_AGENT. The host's relay hop (HubClient issuing
# SendMessage) is a separate, non-configurable axis: JSON-RPC still calls the
# initiating side the "client" regardless of this field.
ROLE_USER = "ROLE_USER"
ROLE_AGENT = "ROLE_AGENT"


@dataclass(frozen=True)
class Part:
    """One content part (A2A ``Part``). ``data`` carries structured JSON."""

    text: str = ""
    data: dict[str, Any] | None = None

    def to_a2a_json(self) -> dict[str, Any]:
        if self.data is not None:
            return {"kind": "data", "data": self.data}
        return {"kind": "text", "text": self.text}

    @classmethod
    def from_a2a_json(cls, raw: dict[str, Any]) -> "Part":
        if raw.get("kind") == "data" or raw.get("data") is not None:
            return cls(text=str(raw.get("text") or ""), data=raw.get("data"))
        return cls(text=str(raw.get("text") or ""))


def _new_message_id() -> str:
    """A fresh opaque A2A messageId. Not time-derived (cache/test safe)."""
    return f"msg-{uuid.uuid4().hex[:12]}"


@dataclass
class A2AMessage:
    """One message from ``src_role`` to ``dst_role``.

    ``round`` is the 0-based relay hop count (not per-agent turn index).
    ``context_id``/``task_id`` are opaque wire ids grouping this pair run's
    messages — minted fresh, never the host's own run/task identifiers (those
    encode role/model/harness names and would leak the experimental condition
    onto the wire). ``sender`` is the speaker's agent-card name — A2A carries
    peer identity on the transport, so it rides the envelope rather than the
    text.
    """

    src_role: str
    dst_role: str
    round: int
    parts: tuple[Part, ...]
    message_id: str = field(default_factory=_new_message_id)
    context_id: str = ""
    task_id: str | None = None
    sender: str = ""
    role: str = ROLE_AGENT
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of_text(
        cls,
        *,
        src_role: str,
        dst_role: str,
        round: int,
        content: str,
        context_id: str = "",
        task_id: str | None = None,
        sender: str = "",
        role: str = ROLE_AGENT,
        metadata: dict[str, Any] | None = None,
    ) -> "A2AMessage":
        """Build a plain-text message — the common relay case."""
        return cls(
            src_role=src_role,
            dst_role=dst_role,
            round=round,
            parts=(Part(text=content),),
            context_id=context_id,
            task_id=task_id,
            sender=sender,
            role=role,
            metadata=metadata or {},
        )

    @property
    def content(self) -> str:
        """Concatenated text of all text parts — the wire payload."""
        return "".join(p.text for p in self.parts if p.text)

    def replace_content(self, content: str) -> "A2AMessage":
        """This message with its text replaced, identity preserved.

        For channels that rewrite bodies (redaction, lossy wires), so a
        transform cannot drop ``messageId``/``sender`` along with the text.
        """
        return A2AMessage(
            src_role=self.src_role,
            dst_role=self.dst_role,
            round=self.round,
            parts=(Part(text=content),),
            message_id=self.message_id,
            context_id=self.context_id,
            task_id=self.task_id,
            sender=self.sender,
            role=self.role,
            metadata=dict(self.metadata),
        )

    def to_a2a_json(self) -> dict[str, Any]:
        """A spec-shaped ``Message`` (§4.1.4; camelCase per §5.5).

        A2A defines no ``Message`` field for the sender's name, so it is
        carried as declared metadata rather than smuggled into ``parts``.
        """
        out: dict[str, Any] = {
            "messageId": self.message_id,
            "role": self.role,
            "parts": [p.to_a2a_json() for p in self.parts],
        }
        if self.context_id:
            out["contextId"] = self.context_id
        if self.task_id:
            out["taskId"] = self.task_id
        metadata = dict(self.metadata)
        if self.sender:
            metadata["a2aSenderName"] = self.sender
        if metadata:
            out["metadata"] = metadata
        return out

    @classmethod
    def from_a2a_json(
        cls,
        raw: dict[str, Any],
        *,
        src_role: str = "",
        dst_role: str = "",
        round: int = 0,
    ) -> "A2AMessage":
        """Rebuild from the wire form; role plumbing is host-supplied."""
        metadata = dict(raw.get("metadata") or {})
        sender = str(metadata.pop("a2aSenderName", "") or "")
        parts = tuple(
            Part.from_a2a_json(p) for p in (raw.get("parts") or ())
            if isinstance(p, dict)
        )
        return cls(
            src_role=src_role,
            dst_role=dst_role,
            round=round,
            parts=parts or (Part(text=""),),
            message_id=str(raw.get("messageId") or _new_message_id()),
            context_id=str(raw.get("contextId") or ""),
            task_id=(str(raw["taskId"]) if raw.get("taskId") else None),
            sender=sender,
            role=str(raw.get("role") or ROLE_AGENT),
            metadata=metadata,
        )
