"""A2A JSON-RPC binding (v1.0 §9) — the wire between two agents' hubs.

Each hub serves ``POST /rpc`` and its card at ``/.well-known/agent-card.json``
(§14.3); the host is the A2A client on both sides. Method names are PascalCase
per §9.1; ``protocolBinding`` is ``JSONRPC``, one of the three core bindings in
§4.4.6, so no custom-binding URI is needed.

Not on this wire: how a model asks its OWN hub to send. That is a local
affordance (an MCP tool), below the protocol.
"""
from __future__ import annotations

from typing import Any

PROTOCOL_BINDING = "JSONRPC"
PROTOCOL_VERSION = "1.0"
VERSION_HEADER = "A2A-Version"
AGENT_CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/rpc"

# Methods (§5.3). The rest of the table (streaming, push config, ListTasks) has
# no analogue in a two-agent run and answers UnsupportedOperationError.
SEND_MESSAGE = "SendMessage"
GET_TASK = "GetTask"
CANCEL_TASK = "CancelTask"

# Error codes: §5.4, then the JSON-RPC 2.0 base codes.
UNSUPPORTED_OPERATION = -32004
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def request(method: str, params: dict[str, Any], *, id: int | str = 1) -> dict[str, Any]:
    """A JSON-RPC 2.0 request envelope (§9.3)."""
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def send_message_params(message: dict[str, Any]) -> dict[str, Any]:
    """``SendMessageRequest`` params for a rendered ``Message`` (§9.4.1)."""
    return {"message": message}


def agent_interface(url: str) -> dict[str, Any]:
    """An ``AgentInterface`` entry for this hub's card (§4.4.6).

    ``url`` is loopback inside the sandbox; §4.4.6 wants absolute HTTPS "in
    production", and a container-local hub is the documented exception.
    """
    return {
        "url": url.rstrip("/") + RPC_PATH,
        "protocolBinding": PROTOCOL_BINDING,
        "protocolVersion": PROTOCOL_VERSION,
    }
