#!/usr/bin/env python3
"""a2a tools MCP server — stdio, stdlib only. Spawned by the agent CLI
inside the sandbox container.

This is the LOCAL affordance: how the model asks its own hub to act, not the
A2A wire (that is the hub's JSON-RPC binding, spoken hub-to-hub with the host
as client). These names are client-side, not protocol operations — A2A covers
agent-to-agent communication, MCP covers model-to-tool (A2A Appendix B).

Tools are prefixed ``a2a_`` because several harnesses ship their OWN native
``send_message``/``list_agents`` for agent-team collaboration (Claude Code's
built-in `SendMessage`, Codex's collaboration-mode tools) — a bare name
collides and the model's calls get routed to the wrong implementation
entirely, never reaching this server.

    a2a_list_agents()               — the peer directory (from
                                      A2A_PEER_CARDS); A2A defines no
                                      directory operation, so this is
                                      direct-configuration discovery
    a2a_send_message(to, content,   — POST to the hub ``/outbox``. With
        summary?, wait_for_reply?,    ``wait_for_reply`` (the default) it then
        timeout_s?)                   blocks on ``/inbox/claim`` for that
                                      agent's reply — mail from anyone else
                                      stays queued — but only up to
                                      ``timeout_s`` (default
                                      ``_DEFAULT_WAIT_TIMEOUT_S``); on expiry
                                      it returns control with a non-error
                                      note instead of blocking forever. With
                                      ``wait_for_reply=false`` it returns an
                                      ack immediately, so an agent can fan
                                      out to several peers concurrently.
                                      ``to="*"`` broadcasts: expanded HERE
                                      into one row per rostered peer, so the
                                      wire only carries concrete recipients
                                      (waiting on a broadcast is refused).
                                      The host turns each outbox row into an
                                      A2A ``Message`` and issues SendMessage
                                      against the recipient's hub.
    a2a_wait_for_messages(          — block until at least one queued/new
        from_agent?, timeout_s?)      message is available (optionally only
                                      from one agent), claim and return the
                                      batch, up to ``timeout_s`` (default
                                      ``_DEFAULT_WAIT_TIMEOUT_S``).
                                      ``timeout_s=0`` is a non-blocking inbox
                                      check.
    a2a_task_complete(outcome)      — POST ``/task_complete`` (→ COMPLETED);
                                      registered ONLY for the assigned agent
                                      (A2A_CAN_COMPLETE=1)

Correlation is per-sender via the hub's atomic ``/inbox/claim``; senderless
relay notices match every filter (they exist to unblock stuck waits). While
blocked, the tool publishes what it waits on via ``/waiting`` so the host can
aim failure notices at actual waiters. When the conversation ends first, a
blocked poll returns a "conversation ended" sentinel instead of hanging.

Every blocking wait is bounded by default (see ``_DEFAULT_WAIT_TIMEOUT_S``).
A2A's own answer to "the callee is slow" is async-first, not a heartbeat: a
caller can request ``returnImmediately`` and then poll ``tasks/get``,
subscribe to streamed ``TaskStatusUpdateEvent``s, or register a push
notification, rather than sit inside one indefinitely blocking call (A2A
spec §1.2 guiding principles; §3.2.2 SendMessageConfiguration.
returnImmediately; §3.5.1 overview of polling / streaming / push). Our tool
calls are synchronous and cannot stream an interim status back to the model
mid-call, so a bounded wait is the analogue that fits: past the deadline the
call returns — handing control back — with an informative, non-error note
(the peer may still be TASK_STATE_WORKING, or itself blocked on someone
else) so the model can re-wait, work on something else, or try a different
peer, instead of one tool call staying open until every live agent is
deadlocked.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

HUB_URL = os.environ.get("A2A_HUB_URL", "http://127.0.0.1:8765")

# How long a single /inbox poll request waits, and how often we re-poll.
# The wall-clock ceiling is the CLI's MCP_TOOL_TIMEOUT; we just avoid a busy
# loop. Kept well under the stdio idle timeout so the CLI never aborts us.
_POLL_INTERVAL_S = 1.0

# Default ceiling on a2a_send_message's reply wait and a2a_wait_for_messages'
# blocking wait when the caller does not name its own timeout_s. Chosen well
# above a normal reply turn (single-hop specialist replies run well under a
# minute; a lead's own long tool-heavy turns can run several) so a healthy
# exchange is never cut off mid-thought, but well below a conversation's
# overall budget, so a stuck pair (or a cycle of several stuck pairs) is
# handed back to the model — not left blocking — long before every live
# agent ends up parked on a wait nothing will ever satisfy.
_DEFAULT_WAIT_TIMEOUT_S = 240.0
_HTTP_TIMEOUT_S = 15.0

# Roster hints, set by the deployer's MCP config when known:
#   A2A_SELF          — this agent's own name (rejected as a recipient)
#   A2A_PEERS         — comma-separated names this agent may address
#   A2A_PEER_CARDS    — JSON array of the peers' agent cards (name,
#                       description, skills); the directory a2a_list_agents
#                       returns. Absent ⇒ bare A2A_PEERS names.
#   A2A_CAN_COMPLETE  — "1" iff this agent owns the assigned task and may
#                       call a2a_task_complete (the tool is not registered
#                       otherwise)
# When A2A_PEERS is unset, recipient validation is lenient (any non-empty
# name) — the host relay routes to the single peer regardless.
_SELF = (os.environ.get("A2A_SELF") or "").strip()
_PEERS = tuple(
    p.strip() for p in (os.environ.get("A2A_PEERS") or "").split(",") if p.strip()
)
_CAN_COMPLETE = (os.environ.get("A2A_CAN_COMPLETE") or "").strip() == "1"


def _load_peer_cards() -> list[dict]:
    """Peer agent cards from A2A_PEER_CARDS, else bare cards from A2A_PEERS.

    Malformed content raises rather than falling back to bare names: the var
    is machine-written, and a silent fallback would strip the scenario's
    discovery surface with nothing in any artifact to show it.
    """
    raw = os.environ.get("A2A_PEER_CARDS")
    if not raw:
        return [{"name": name} for name in _PEERS]
    try:
        cards = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"A2A_PEER_CARDS is not valid JSON: {e}") from e
    if not isinstance(cards, list):
        raise RuntimeError(
            f"A2A_PEER_CARDS must be a JSON array, got {type(cards).__name__}"
        )
    bad = [c for c in cards if not isinstance(c, dict)]
    if bad:
        raise RuntimeError(
            f"A2A_PEER_CARDS has {len(bad)} non-object entr"
            f"{'y' if len(bad) == 1 else 'ies'}; every card must be an object"
        )
    return cards


_PEER_CARDS = _load_peer_cards()

LIST_AGENTS_TOOL = {
    "name": "a2a_list_agents",
    "description": (
        "List the agents you can reach with a2a_send_message. Returns a "
        "directory: each agent's name, what it does, and the skills it "
        "advertises. Call this to discover who to contact before messaging — "
        "the name you get back is exactly what you pass as a2a_send_message's "
        "`to`. Takes no arguments."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

SEND_MESSAGE_TOOL = {
    "name": "a2a_send_message",
    "description": (
        "Send a message to another agent.\n\n"
        '    {"to": "dealer_a", "content": "Best out-the-door price on the '
        'hybrid?", "summary": "request OTD quote"}\n\n'
        "`to` is an agent name from a2a_list_agents, or \"*\" to broadcast to "
        "every agent you can reach (costs one message per recipient — use "
        "only when everyone genuinely needs it; broadcasts cannot wait for a "
        "reply).\n\n"
        "By default the call waits for the addressed agent's reply and "
        "returns it as the result; messages from other agents that arrive "
        "while you wait are held for you. The wait is capped at timeout_s "
        "(default 240 seconds) — if nothing arrives in time, the call "
        "returns a not-an-error timeout note instead of blocking forever; "
        "the other agent may still be working, or itself waiting on someone "
        "else. You can call a2a_wait_for_messages(from_agent=to) to keep "
        "waiting without resending, do other work first, or try a different "
        "agent. With wait_for_reply=false the call returns as soon as the "
        "message is queued — there is no busy state; a message to a busy "
        "agent is delivered when that agent next reads. Use "
        "wait_for_reply=false to contact several agents, then collect their "
        "answers with a2a_wait_for_messages.\n\n"
        "Your plain-text output is NOT visible to other agents — to "
        "communicate, you MUST call this tool. To ANSWER a message that "
        "arrived for you, copy the name on its `from:` line as your `to`. "
        "Make each message self-contained: the recipient sees only your "
        "`content`, never your files, workspace, or reasoning."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": (
                    "Recipient: an agent name from a2a_list_agents, or \"*\" "
                    "to broadcast to all reachable agents."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "The message to send (plain text). Self-contained — the "
                    "agent you write to sees only this."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Optional 5-10 word gist of the message, recorded beside "
                    "it for whoever reviews the exchange later."
                ),
            },
            "wait_for_reply": {
                "type": "boolean",
                "description": (
                    "Default true: wait for the addressed agent's reply and "
                    "return it. Set false to return once the message is "
                    "queued (required for broadcasts); collect replies with "
                    "a2a_wait_for_messages."
                ),
            },
            "timeout_s": {
                "type": "number",
                "description": (
                    "Only used when waiting for a reply (wait_for_reply is "
                    "not false). Maximum seconds to wait before returning a "
                    "timeout note. Defaults to 240 seconds; pass a larger "
                    "value if you expect this agent's reply to take longer."
                ),
            },
        },
        "required": ["to", "content"],
    },
}

WAIT_FOR_MESSAGES_TOOL = {
    "name": "a2a_wait_for_messages",
    "description": (
        "Wait for messages from other agents and return them.\n\n"
        '    {"from_agent": "dealer_a"}   — wait for one specific agent\n'
        '    {"timeout_s": 0}             — check the queue without waiting\n'
        "    {}                           — wait for mail from anyone\n\n"
        "Blocks until at least one message is available, then returns every "
        "queued message; each carries a `from:` line naming its sender (copy "
        "it as `to` when you answer). Use it after a2a_send_message with "
        "wait_for_reply=false to collect the replies, or any time you expect "
        "an agent to contact you. With `from_agent`, other agents' messages "
        "stay queued for a later wait. The wait is capped at timeout_s "
        "(default 240 seconds, even if you omit it); expiry is reported in "
        "the result and is not an error — call again to keep waiting."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "from_agent": {
                "type": "string",
                "description": (
                    "Only return messages from this agent (name as listed by "
                    "a2a_list_agents). Omit to accept messages from any agent."
                ),
            },
            "timeout_s": {
                "type": "number",
                "description": (
                    "Maximum seconds to wait. Defaults to 240 seconds even "
                    "if omitted; 0 checks the queue once and returns; pass a "
                    "larger value to wait longer in one call."
                ),
            },
        },
    },
}

TASK_COMPLETE_TOOL = {
    "name": "a2a_task_complete",
    "description": (
        "Mark the task assigned to you as complete and record its outcome.\n\n"
        "Use it only when the work is fully finished and nothing further is "
        "needed from you or from any other agent — completing the task closes "
        "it for everyone and no further messages can be exchanged. Never use "
        "it to wait for a reply: a2a_send_message waits when wait_for_reply "
        "is true, and a2a_wait_for_messages waits otherwise. "
        "The outcome you record is the final summary of what was accomplished."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "description": "Short summary of the task's outcome.",
            },
        },
        "required": ["outcome"],
    },
}

TOOLS = (
    [LIST_AGENTS_TOOL, SEND_MESSAGE_TOOL, WAIT_FOR_MESSAGES_TOOL]
    + ([TASK_COMPLETE_TOOL] if _CAN_COMPLETE else [])
)


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _get(path: str) -> dict:
    with urllib.request.urlopen(
        HUB_URL.rstrip("/") + path, timeout=_HTTP_TIMEOUT_S
    ) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _post_complete(outcome: str) -> str:
    """Signal task completion to the hub."""
    _post("/task_complete", {"reason": outcome})
    return "task recorded as complete; the conversation is now closed."


def _handle_list_agents() -> tuple[str, bool]:
    """Render the peer directory the agent can message. (text, is_error)."""
    if not _PEER_CARDS:
        return "No other agents are reachable.", False
    lines = ["Agents you can reach with a2a_send_message:"]
    for card in _PEER_CARDS:
        name = str(card.get("name") or "").strip()
        if not name:
            continue
        desc = str(card.get("description") or "").strip()
        lines.append(f"- {name}" + (f": {desc}" if desc else ""))
        for skill in card.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            sname = str(skill.get("name") or skill.get("id") or "").strip()
            sdesc = str(skill.get("description") or "").strip()
            if sname:
                lines.append(f"    • {sname}" + (f" — {sdesc}" if sdesc else ""))
    return "\n".join(lines), False


def _validate_recipient(to: str, content: str) -> str | None:
    """Return an error string if the send is malformed, else None."""
    if not to:
        return (
            "a2a_send_message failed: 'to' must be a non-empty agent name "
            "(or \"*\" to broadcast)."
        )
    if not content.strip():
        return "a2a_send_message failed: 'content' must be a non-empty message."
    if to == "*":
        return None  # broadcast; recipients resolved from the roster
    if _SELF and to == _SELF:
        return "a2a_send_message failed: cannot send a message to yourself."
    if _PEERS and to not in _PEERS:
        return (
            f"a2a_send_message failed: unknown agent {to!r}. "
            f"Agents you can reach: {', '.join(_PEERS)}."
        )
    return None


# End-of-conversation sentinels a blocked wait returns instead of hanging.
_ENDED_WHILE_AWAITING_REPLY = (
    "[conversation ended: the other agent closed the task before "
    "replying. No further messages can be exchanged.]"
)
_ENDED_WHILE_WAITING = (
    "[conversation ended: the task was closed while you were waiting. "
    "No further messages can be exchanged.]"
)


def _set_waiting(on: str | None) -> None:
    """Publish what this agent's blocked wait is filtering on (None clears).

    The host reads /state.waiting_on to aim no-reply/unreachable notices at
    the agents actually blocked on a dead or silent peer.
    """
    _post("/waiting", {"on": on})


def _clear_waiting() -> None:
    try:
        _post("/waiting", {"on": None})
    except Exception:  # noqa: BLE001
        # A stale waiting_on risks at worst a spurious relay notice later,
        # never a hang — and a hub that cannot clear it is likely gone.
        pass


def _parse_timeout(args: dict, tool_name: str) -> tuple[float | None, str | None]:
    """Resolve ``timeout_s``: omitted defaults to ``_DEFAULT_WAIT_TIMEOUT_S``.

    Returns ``(value, None)`` or ``(None, error_text)``.
    """
    timeout_s = args.get("timeout_s")
    if timeout_s is None:
        return _DEFAULT_WAIT_TIMEOUT_S, None
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        return None, f"{tool_name} failed: 'timeout_s' must be a number."
    if timeout_s < 0:
        return None, f"{tool_name} failed: 'timeout_s' must be >= 0."
    return timeout_s, None


def _wait_loop(from_: str | None, timeout_s: float | None,
               max_: int | None, ended_text: str) -> tuple[list[dict] | None, str | None]:
    """Long-poll ``/inbox/claim`` until messages, end, or timeout.

    Returns ``(messages, None)`` when messages were claimed, ``(None,
    ended_text)`` if the conversation ended first, ``(None, None)`` on
    timeout. Correlation is the hub's: unconsumed rows from ``from_`` (relay
    notices match any filter), claimed atomically. Transient hub errors are
    retried; an HTTP error status is a tool/hub version skew and raises —
    silently retrying a 404 forever would hang the agent with no artifact.
    """
    body: dict = {}
    if from_:
        body["from"] = from_
    if max_ is not None:
        body["max"] = max_
    deadline = (time.monotonic() + timeout_s) if timeout_s is not None else None
    while True:
        try:
            resp = _post("/inbox/claim", body)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"the messaging hub rejected /inbox/claim (HTTP {exc.code}); "
                "hub and tool versions are out of sync"
            ) from exc
        except Exception:  # noqa: BLE001 — transient; retry
            resp = None
        if resp is not None:
            msgs = resp.get("messages") or []
            if msgs:
                # The host already stamps the sender onto the content it
                # delivers (``from: <name>``), so the row's own ``from`` field
                # is not re-applied here — doing so would double the header.
                return msgs, None
            if resp.get("ended"):
                return None, ended_text
        if deadline is not None and time.monotonic() >= deadline:
            return None, None
        time.sleep(_POLL_INTERVAL_S)


def _handle_send_message(args: dict) -> tuple[str, bool]:
    """Enqueue an outbound message; block for the recipient's reply unless
    ``wait_for_reply`` is false.

    Returns (text, is_error). ``text`` is the recipient's reply (or a queued
    ack when not waiting), an end-of-conversation sentinel if the
    conversation closed while waiting, or a validation error.
    """
    to = str(args.get("to") or "").strip()
    content = str(args.get("content") or "")
    summary = str(args.get("summary") or "").strip() or None
    err = _validate_recipient(to, content)
    if err is not None:
        return err, True
    wait = args.get("wait_for_reply")
    wait = True if wait is None else bool(wait)
    wait_timeout_s: float | None = None
    if wait:
        wait_timeout_s, timeout_err = _parse_timeout(args, "a2a_send_message")
        if timeout_err is not None:
            return timeout_err, True

    if to == "*":
        # Broadcast: one individually-addressed message per reachable peer —
        # expanded HERE so the wire and the hop log only ever carry concrete
        # recipients. Waiting is refused rather than guessed at: "the reply
        # to a broadcast" has no single sender to correlate on.
        if not _PEERS:
            return (
                "a2a_send_message failed: cannot broadcast — no agent roster "
                "is configured for this machine.",
                True,
            )
        if wait:
            return (
                "a2a_send_message failed: a broadcast cannot wait for a "
                "reply. Send with wait_for_reply=false, then collect the "
                "answers with a2a_wait_for_messages.",
                True,
            )
        queued: list[str] = []
        try:
            for peer in _PEERS:
                _post("/outbox", {"to": peer, "content": content,
                                  "summary": summary})
                queued.append(peer)
        except Exception as exc:  # noqa: BLE001
            # Say exactly who was already queued, or a retry double-sends.
            failed = [p for p in _PEERS if p not in queued]
            return (
                f"a2a_send_message partially failed: the message was queued "
                f"for {', '.join(queued) if queued else 'no agents'} but "
                f"could not be queued for {', '.join(failed)} ({exc}). Do "
                f"not repeat the broadcast — the queued agents would receive "
                f"it twice; if needed, send individually to the failed "
                f"agents only.",
                True,
            )
        return (
            f"message queued for delivery to {len(_PEERS)} agents: "
            f"{', '.join(_PEERS)}. Collect replies with a2a_wait_for_messages.",
            False,
        )

    try:
        _post("/outbox", {"to": to, "content": content, "summary": summary})
    except Exception as exc:  # noqa: BLE001
        return f"a2a_send_message failed: could not reach the messaging hub ({exc}).", True

    if not wait:
        return (
            f"message queued for delivery to {to}. Replies arrive via "
            "a2a_wait_for_messages (or a later a2a_send_message that waits).",
            False,
        )

    # Block for the first unconsumed message FROM the addressed agent (mail
    # from other agents stays queued for its own wait). The CLI holds the
    # tool call open the whole time, bounded by MCP_TOOL_TIMEOUT.
    try:
        _set_waiting(to)
    except Exception as exc:  # noqa: BLE001
        return (
            f"a2a_send_message failed: message was queued but the wait could "
            f"not be registered with the hub ({exc}).",
            True,
        )
    try:
        msgs, text = _wait_loop(to, wait_timeout_s, 1, _ENDED_WHILE_AWAITING_REPLY)
    except RuntimeError as exc:
        return f"a2a_send_message failed: {exc}", True
    finally:
        _clear_waiting()
    if msgs:
        return str(msgs[0].get("content") or ""), False
    if text is not None:
        return str(text), False
    return (
        f"[no reply from {to} within {wait_timeout_s:g} seconds — this is "
        f"not an error. {to} may still be working, or may itself be "
        f"waiting on a reply from someone else. To keep waiting without "
        f"resending, call a2a_wait_for_messages with from_agent={to!r} "
        f"(pass timeout_s for another bounded wait). You can also make "
        f"progress on other work, or message a different agent, in the "
        f"meantime."
    ), False


def _handle_wait_for_messages(args: dict) -> tuple[str, bool]:
    """Block until queued/new messages are available; claim and return them.

    Returns (text, is_error): the message batch (single message verbatim;
    several joined with a separator — each already carries its ``from:``
    line), a timeout note, or the end-of-conversation sentinel.
    """
    from_ = str(args.get("from_agent") or "").strip() or None
    if from_ and _SELF and from_ == _SELF:
        return "a2a_wait_for_messages failed: cannot wait for messages from yourself.", True
    if from_ and _PEERS and from_ not in _PEERS:
        return (
            f"a2a_wait_for_messages failed: unknown agent {from_!r}. "
            f"Agents you can reach: {', '.join(_PEERS)}.",
            True,
        )
    timeout_s, timeout_err = _parse_timeout(args, "a2a_wait_for_messages")
    if timeout_err is not None:
        return timeout_err, True

    try:
        _set_waiting(from_ or "*")
    except Exception as exc:  # noqa: BLE001
        return f"a2a_wait_for_messages failed: could not reach the messaging hub ({exc}).", True
    try:
        msgs, text = _wait_loop(from_, timeout_s, None, _ENDED_WHILE_WAITING)
    except RuntimeError as exc:
        return f"a2a_wait_for_messages failed: {exc}", True
    finally:
        _clear_waiting()
    if msgs:
        parts = [str(m.get("content") or "") for m in msgs]
        return parts[0] if len(parts) == 1 else "\n\n---\n\n".join(parts), False
    if text is not None:
        return str(text), False
    who = f" from {from_}" if from_ else ""
    return (
        f"[no messages{who} arrived within {timeout_s:g} seconds — this is "
        f"not an error; call a2a_wait_for_messages again to keep waiting.]"
    ), False


def _reply(msg_id, result=None, error=None) -> None:
    out: dict = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def _reply_tool(msg_id, text: str, is_error: bool) -> None:
    _reply(msg_id, {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    })


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _reply(msg_id, {
                "protocolVersion": (
                    (msg.get("params") or {}).get("protocolVersion")
                    or "2024-11-05"
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "a2a", "version": "0.4.1"},
            })
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "a2a_list_agents":
                text, is_error = _handle_list_agents()
                _reply_tool(msg_id, text, is_error)
            elif name == "a2a_send_message":
                text, is_error = _handle_send_message(args)
                _reply_tool(msg_id, text, is_error)
            elif name == "a2a_wait_for_messages":
                text, is_error = _handle_wait_for_messages(args)
                _reply_tool(msg_id, text, is_error)
            elif name == "a2a_task_complete" and _CAN_COMPLETE:
                try:
                    text = _post_complete(str(args.get("outcome") or ""))
                    _reply_tool(msg_id, text, False)
                except Exception as exc:  # noqa: BLE001
                    _reply_tool(msg_id, f"a2a_task_complete failed: {exc}", True)
            else:
                _reply(msg_id, error={
                    "code": -32602,
                    "message": f"unknown tool {name!r}",
                })
        elif method == "ping":
            _reply(msg_id, {})
        elif msg_id is not None:
            # Unknown *request* — must answer. Notifications are ignored.
            _reply(msg_id, error={"code": -32601, "message": f"unknown method {method!r}"})


if __name__ == "__main__":
    main()
