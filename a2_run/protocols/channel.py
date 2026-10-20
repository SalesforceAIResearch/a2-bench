"""Channel — the protocol seam between two conversing agents.

The driver owns the loop; the channel owns the message in flight: ``deliver``
transforms it, ``render`` turns it into the text the peer's harness reads.
The two implementations differ ONLY in how much of the envelope reaches the
prompt, so a pair of runs isolates that as a single variable — ``a2a`` carries
the spec's ``Message`` fields, ``direct`` a bare ``from:`` line. Selected by
the experiment yaml's ``a2a.channel`` key.

Further adapters (byte accounting, lossy wires, redaction) subclass
:class:`BaseChannel` and register in ``CHANNEL_REGISTRY``; rewrite bodies via
``A2AMessage.replace_content`` so identity fields survive the transform.
"""
from __future__ import annotations

import abc

from .message import ROLE_AGENT, A2AMessage


class BaseChannel(abc.ABC):
    """Transforms a message between sender and receiver."""

    name: str = ""

    @abc.abstractmethod
    async def deliver(self, msg: A2AMessage) -> A2AMessage:
        """Return the message as the destination role will see it."""

    @abc.abstractmethod
    def render(self, msg: A2AMessage) -> str:
        """The text the destination's harness reads for ``msg``."""


class DirectChannel(BaseChannel):
    """The pre-protocol baseline: the body under a ``from:`` line."""

    name = "direct"

    async def deliver(self, msg: A2AMessage) -> A2AMessage:
        return msg

    def render(self, msg: A2AMessage) -> str:
        if not msg.sender:
            return msg.content
        return f"from: {msg.sender}\n\n{msg.content}"


class A2AChannel(BaseChannel):
    """A2A v1.0 envelope: the peer reads the message's protocol fields.

    The header uses the spec's own field names (§4.1.4), so what the agent
    reads and what crosses the wire are the same document — including for the
    opening work item, which has no ``sender`` but still gets a header (no
    ``from:`` line, since there is no peer to name). ``role`` defaults to
    ``ROLE_AGENT`` for peer-to-peer hops (every party here is an agent, so
    labelling one ``ROLE_USER`` would misattribute it); the opener is built
    with ``ROLE_USER`` by its caller, since it IS the client-originated task
    that starts the work — the one message the role's literal definition fits.
    """

    name = "a2a"

    async def deliver(self, msg: A2AMessage) -> A2AMessage:
        return msg

    def render(self, msg: A2AMessage) -> str:
        head = ["[a2a message]"]
        if msg.sender:
            head.append(f"from: {msg.sender}")
        head.append(f"role: {msg.role or ROLE_AGENT}")
        head.append(f"messageId: {msg.message_id}")
        if msg.context_id:
            head.append(f"contextId: {msg.context_id}")
        if msg.task_id:
            head.append(f"taskId: {msg.task_id}")
        return "\n".join(head) + "\n\n" + msg.content
