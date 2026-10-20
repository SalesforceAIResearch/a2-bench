"""Host-side conversation driver for N-agent a2a runs.

Each role runs ONE long-lived session; its ``a2a_send_message`` /
``a2a_wait_for_messages`` tools block on its own in-sandbox hub
(``docker/ubuntu-base/a2a_hub.py``) until mail matching their filter is
claimable. The host is the A2A client on every side: this driver drains each
role's ``/outbox``, routes each row by its own ``to:`` field, builds an
:class:`A2AMessage`, applies the channel, and issues ``SendMessage`` against
the recipient hub's JSON-RPC endpoint — launching the recipient's session
lazily on the first message addressed to it, and resuming an exited one when
new (or still-unclaimed) mail is pending.

A dead or silent role does not end the run unless it is the assigned role:
agents blocked on it (read from the hub's ``waiting_on``) get a relay notice
and the rest of the conversation continues.

The host also re-checks every hop against the task's ``visibility`` graph:
the in-container roster is agent-controlled, so this is the trust boundary.
An out-of-graph hop is recorded, refused delivery, and reported to the sender.

Termination: ``task_complete``, ``max_messages``, ``invalid_agent_response``
(the opener went silent), ``all_agents_exited``, ``all_agents_blocked``
(every live agent is stuck in a wait nothing can satisfy), ``agent_failed``,
``wall_time`` — each mapped onto an A2A ``TaskState`` by
:mod:`a2_run.protocols.task_state`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..protocols import ROLE_USER, A2AMessage, BaseChannel, jsonrpc

logger = logging.getLogger(__name__)

# The in-container hub script, reused verbatim on the host for local-executor
# a2a runs (the dummy smoke agent) — same wire, no Docker required.
_HUB_SCRIPT = (Path(__file__).resolve().parents[2]
               / "docker" / "ubuntu-base" / "a2a_hub.py")


class HostHub:
    """A host-side a2a_hub instance for a local-executor role (the dummy).

    Runs the same hub module as a sandbox container, on a free loopback port;
    each instance loads a fresh module so mailboxes stay per-role.
    """

    _counter = 0

    def __init__(self) -> None:
        HostHub._counter += 1
        name = f"hosthub_{HostHub._counter}"
        spec = importlib.util.spec_from_file_location(name, _HUB_SCRIPT)
        self._mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self._mod)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), self._mod._Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass

# A launcher the caller supplies: (role, prompt, turn_index) -> awaitable that
# runs ONE session for that agent and resolves to a result carrying
# .status/.error. ``turn_index`` counts that role's sessions (0 = first); a
# later index asks the deployer to resume the role's existing thread.
LaunchSession = Callable[[str, str, int], "Awaitable[Any]"]


@dataclass
class ConversationResult:
    """Outcome of :func:`run_conversation`."""

    conversation: list[dict[str, Any]] = field(default_factory=list)
    termination_reason: str | None = None
    end_reason: str | None = None
    error: str | None = None
    session_results: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# hub HTTP (stdlib; run blocking calls in a thread so the event loop is free)
# --------------------------------------------------------------------------
def _http_get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _http_post(url: str, body: dict, timeout: float,
               headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class A2AError(RuntimeError):
    """A JSON-RPC error returned by a peer hub (§9.5)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"a2a error {code}: {message}")
        self.code = code


class HubClient:
    """Async A2A client for one agent's hub.

    ``send_message``/``cancel_task`` are the A2A JSON-RPC operations (§5.3);
    the rest is the hub's local surface — how the host learns what its own
    agent did, which is not an agent-to-agent concern.
    """

    def __init__(self, base_url: str, *, http_timeout: float = 15.0) -> None:
        self.base = base_url.rstrip("/")
        self._t = http_timeout

    async def _get(self, path: str) -> dict:
        return await asyncio.to_thread(_http_get, self.base + path, self._t)

    async def _post(self, path: str, body: dict) -> dict:
        return await asyncio.to_thread(_http_post, self.base + path, body, self._t)

    async def _rpc(self, method: str, params: dict) -> dict:
        """One JSON-RPC call; raises :class:`A2AError` on an error result."""
        resp = await asyncio.to_thread(
            _http_post, self.base + jsonrpc.RPC_PATH,
            jsonrpc.request(method, params), self._t,
            {jsonrpc.VERSION_HEADER: jsonrpc.PROTOCOL_VERSION},
        )
        if isinstance(resp.get("error"), dict):
            err = resp["error"]
            raise A2AError(int(err.get("code") or 0),
                           str(err.get("message") or "unknown error"))
        return resp.get("result") or {}

    async def send_message(self, msg: A2AMessage, *, rendered: str) -> dict:
        """``SendMessage`` (§9.4.1). ``rendered`` is what the agent will read,
        travelling beside the spec document so the hub stores both."""
        params = jsonrpc.send_message_params(msg.to_a2a_json())
        params["rendered"] = rendered
        params["round"] = msg.round
        return await self._rpc(jsonrpc.SEND_MESSAGE, params)

    async def cancel_task(self, reason: str | None,
                          task_id: str | None = None) -> None:
        """``CancelTask`` (§9.4.5) — releases a blocked poll and lands the task
        in CANCELED, which is not the same as it having completed."""
        await self._rpc(jsonrpc.CANCEL_TASK,
                        {"id": task_id or "", "reason": reason or ""})

    # ---- local surface (host ↔ its own agent's hub) ----------------------
    async def drain_outbox(self, since: int) -> tuple[list[dict], bool]:
        """New outbound messages (seq > since) + whether the hub has ended."""
        resp = await self._get(f"/outbox?since={since}")
        return (resp.get("messages") or []), bool(resp.get("ended"))

    async def state(self) -> dict:
        return await self._get("/state")

    async def publish_card(self, card: dict) -> None:
        """Publish the card served at the well-known URI."""
        await self._post("/agent-card", {"card": card})

    async def claim_inbox(self) -> list[dict]:
        """Atomically claim ALL unconsumed inbox rows (host-side use: sweep a
        role's pending mail into its resume prompt, or detect that an exited
        role left mail unread)."""
        resp = await self._post("/inbox/claim", {})
        return resp.get("messages") or []

    async def waiting_on(self) -> str | None:
        """What this agent's blocked wait filters on (None = not waiting)."""
        st = await self.state()
        return st.get("waiting_on") or None


# --------------------------------------------------------------------------
# the driver
# --------------------------------------------------------------------------
# A2A's InvalidAgentResponseError (spec §3.3.2) rendered as prose for the
# waiting caller. Deliberately reads like an ordinary delivery failure — no
# pair/turn/evaluation framing.
_NO_REPLY_NOTICE = (
    "[messaging service] The agent {name} did not return a message in response "
    "to yours, so there is no reply to deliver. Nothing was received from that "
    "agent. You can send another message to it, or proceed without its reply."
)


_UNREACHABLE_NOTICE = (
    "[messaging service] The agent {name} is no longer reachable and did not "
    "receive your message. No further messages can be delivered to that agent."
)
"""Last resort: the recipient is gone and its harness cannot resume it. A
harness that can gets relaunched instead (see ``_drain_once``)."""


_DELIVERY_REFUSED_NOTICE = (
    "[messaging service] Your message to {name} was not delivered: that "
    "agent is not reachable from this machine. Use a2a_list_agents to see "
    "the agents you can reach."
)
"""Out-of-visibility hop: the roster never offered the name, so the tool was
bypassed; the host refuses delivery."""


#: How long every-live-agent-blocked must persist before all_agents_blocked —
#: long enough for in-flight claims and /waiting churn to settle.
_STALL_GRACE_S = 10.0


async def run_conversation(
    *,
    roles: list[str],
    assigned_role: str,
    assignment: str,
    hub_urls: dict[str, str],
    launch_session: LaunchSession,
    channel: BaseChannel,
    context_id: str,
    task_id: str | None,
    max_messages: int,
    deadline_monotonic: float,
    sender_names: dict[str, str] | None = None,
    can_resume: dict[str, bool] | None = None,
    poll_interval_s: float = 0.5,
    http_timeout_s: float = 15.0,
    on_hop: Callable[[dict], None] | None = None,
    on_event: Callable[..., None] | None = None,
    visibility: dict[str, list[str]] | None = None,
    stall_grace_s: float = _STALL_GRACE_S,
) -> ConversationResult:
    """Drive one hub-shuttled a2a conversation to termination.

    ``roles`` has one or more entries; ``hub_urls`` maps each to a
    host-reachable hub base URL. A SINGLE role is a solo run: the assigned
    agent works alone (no peer to address), so the driver simply carries it
    to its own ``task_complete`` — or records ``invalid_agent_response`` if it
    exits silently. ``launch_session(role, prompt, turn_index)``
    runs one session for that role (``turn_index`` 0 opens the conversation;
    a later index resumes the role's existing thread, gated by
    ``can_resume`` / ``supports_session_resume`` — spec §3.4.3).
    ``sender_names`` maps role → agent-card name for the ``from:`` header;
    the role key is the fallback so peer mail is always attributed. Outbox
    rows address recipients by either identity (the tool validates card
    names; role keys are accepted for uncarded roles).

    ``visibility`` is the task's ``{role: [roles it may address]}`` graph
    (empty ⇒ full mesh), enforced host-side on every hop. ``on_event``
    receives durability events (e.g. ``undeliverable_mail``); like
    ``on_hop`` it never affects routing.
    """
    if not roles:
        raise ValueError(f"a2a conversation requires at least one role, got {roles}")
    hubs = {r: HubClient(hub_urls[r], http_timeout=http_timeout_s) for r in roles}
    names = dict(sender_names or {})
    resumable = dict(can_resume or {})

    def name_of(role: str) -> str:
        """Card name to attribute ``role``'s messages to; role key if uncarded."""
        return names.get(role) or role

    # Recipient resolution: an outbox row's ``to`` is the card name (what the
    # tool validates against A2A_PEERS) or the bare role key for uncarded
    # roles. A name denoting two roles would misroute mail — refuse up front.
    dst_role_of: dict[str, str] = {r: r for r in roles}
    for _r in roles:
        _n = name_of(_r)
        _owner = dst_role_of.get(_n)
        if _owner is not None and _owner != _r:
            raise ValueError(
                f"ambiguous agent identity {_n!r}: it already denotes role "
                f"{_owner!r} and is also role {_r!r}'s card name — card "
                f"names must be unique and must not shadow another role's key"
            )
        dst_role_of[_n] = _r

    # Visibility graph in role keys. The tool-side roster only binds honest
    # agents; the hub's /outbox is agent-reachable, so the host re-checks.
    vis: dict[str, frozenset[str]] = {
        r: frozenset(v) for r, v in (visibility or {}).items()
    }

    conversation: list[dict[str, Any]] = []
    turn_index = {r: 0 for r in roles}
    outbox_seen = {r: 0 for r in roles}
    sessions: dict[str, asyncio.Task] = {}
    launched_at: dict[str, float] = {}
    # Sessions launched per role. Distinct from ``turn_index`` (messages SENT):
    # a silent session consumed an index without producing a message, and
    # reusing it would overwrite that session's transcript.
    session_count = {r: 0 for r in roles}

    def _start_session(role: str, prompt: str) -> None:
        """Launch one session for ``role`` at its next session index."""
        idx = session_count[role]
        session_count[role] = idx + 1
        sessions[role] = asyncio.ensure_future(launch_session(role, prompt, idx))
        launched_at[role] = time.monotonic()
        # A fresh session sweeps and waits anew: notices delivered to a dead
        # incarnation must not be withheld from this one.
        exit_swept.discard(role)
        for told in departure_notified.values():
            told.discard(role)
        for told in no_reply_reported.values():
            told.discard(role)
    # Departed role → waiters its departure notice has reached. Per WAITER,
    # not one-shot: an agent that blocks on the dead role later still gets one.
    departure_notified: dict[str, set[str]] = {}
    # Same per-waiter tracking for the no-reply error; and roles whose session
    # has exited — mail addressed there has no reader until a resume.
    no_reply_reported: dict[str, set[str]] = {}
    session_ended: set[str] = set()
    # Roles whose clean exit was already drained + inbox-swept; after the
    # sweep nothing new can land in a dead role's hub, so later polls skip it.
    exit_swept: set[str] = set()

    # The opening work item is a real A2AMessage too (no ``sender`` — it's an
    # assignment, not peer mail), so it gets the same channel treatment as
    # every later hop rather than bypassing it as a bare string. Its role is
    # ROLE_USER: unlike a peer hop (both parties agents, so ROLE_AGENT), this
    # is the client-originated task that starts the work — the one message in
    # the conversation that fills the role's literal definition.
    opening = channel.render(A2AMessage.of_text(
        src_role="", dst_role=assigned_role, round=-1, content=assignment,
        context_id=context_id, task_id=task_id, role=ROLE_USER,
    ))

    # The exact text last delivered TO each role — recorded as ``incoming``.
    delivered_render: dict[str, str] = {assigned_role: opening}

    result = ConversationResult()

    # The assigned agent opens with the work item as its first prompt.
    _start_session(assigned_role, opening)

    def _record_hop(src: str, dst: str, content: str,
                    summary: str | None = None) -> dict:
        """Build the conversation entry for one outbound hop from ``src``."""
        rnd = len(conversation)
        entry = {
            "round": rnd,
            "role": src,
            "to": dst,
            # The sender's own 5-10 word gist (optional tool arg) — reviewer
            # affordance only, never part of what the recipient reads.
            "summary": summary,
            "turn_index": turn_index[src],
            # What this speaker read, byte for byte — kept verbatim, never
            # re-derived: a reconstruction silently drops whatever delivery
            # carried and it did not. ``reply`` stays bare; the judge
            # attributes it via its own from= field.
            "incoming": delivered_render.get(src, assignment),
            "reply": content,
            "status": "completed",
            "ended": False,
            "duration_s": None,
            "error": None,
        }
        turn_index[src] += 1
        return entry

    async def _finish(reason: str, end_reason: str | None = None,
                      error: str | None = None) -> None:
        result.termination_reason = reason
        result.end_reason = end_reason
        result.error = error
        # Tell any live peer session to stop waiting. CancelTask, not complete:
        # the survivor did not finish its own work.
        for r in roles:
            if r in sessions and not sessions[r].done():
                try:
                    await hubs[r].cancel_task(end_reason, task_id)
                except Exception:  # noqa: BLE001
                    pass

    async def _notice(role: str, text: str, *, round: int,
                      raising: bool = False) -> None:
        """Deliver a messaging-service notice to ``role``.

        Sent with no sender identity — a notice is from the relay, not an
        agent, so no channel attributes it to a peer. ``raising`` for a notice
        the caller is BLOCKED on, which must not be lost silently.
        """
        msg = A2AMessage.of_text(
            src_role="", dst_role=role, round=round, content=text,
            context_id=context_id, task_id=task_id,
            metadata={"a2aRelayNotice": True},
        )
        try:
            await hubs[role].send_message(msg, rendered=text)
        except Exception as e:  # noqa: BLE001
            if raising:
                raise
            logger.debug("notice to %s failed: %s", role, e)
        else:
            delivered_render[role] = text

    def _live_waiters_on(role: str,
                         waiting_now: dict[str, str | None]) -> list[str]:
        """Live roles whose blocked wait filters specifically on ``role``,
        from this poll's snapshot. Wildcard waiters are excluded on purpose:
        another live sender can still satisfy them."""
        return [
            w for w, won in waiting_now.items()
            if w != role and won and won != "*"
            and dst_role_of.get(won) == role
        ]

    async def _notify_departure(r: str,
                                waiting_now: dict[str, str | None]) -> bool:
        """Tell every live agent blocked on ``r`` that it left — once per
        WAITER, so a later waiter still gets its own notice. Resumable roles
        get the no-reply wording (writing again resumes them). Returns True
        if the run must terminate: a blocked waiter that cannot be notified
        would hang to the wall clock.
        """
        told = departure_notified.setdefault(r, set())
        waiters = [w for w in _live_waiters_on(r, waiting_now)
                   if w not in told]
        if not waiters:
            return False
        text = (_NO_REPLY_NOTICE if resumable.get(r)
                else _UNREACHABLE_NOTICE).format(name=name_of(r))
        for w in waiters:
            logger.info(
                "conversation: %s is gone; releasing %s's blocked wait with "
                "a departure notice", r, w,
            )
            try:
                await _notify_waiter(w, text)
            except Exception as e:  # noqa: BLE001
                await _finish(
                    "agent_failed",
                    error=f"could not notify {w} that {name_of(r)} departed: {e}",
                )
                return True
            told.add(w)
        return False

    async def _notify_waiter(w: str, text: str) -> None:
        """Deliver a wait-releasing notice to ``w``. The snapshot may be
        stale — a drain since then can have delivered exactly what ``w`` was
        blocked on, letting it complete or exit — so a failure against a
        waiter that is no longer live and blocked is moot; against a
        genuinely blocked one it re-raises (that waiter would hang).
        """
        if w not in sessions or sessions[w].done():
            return  # nothing is blocked anymore
        try:
            await _notice(w, text, round=len(conversation), raising=True)
        except Exception as e:  # noqa: BLE001
            try:
                st = await hubs[w].state()
                moot = bool(st.get("ended"))
            except Exception:  # noqa: BLE001
                moot = False
            if moot or w not in sessions or sessions[w].done():
                logger.debug("notice to %s moot (%s); it is no longer "
                             "blocked", w, e)
                return
            raise

    async def _drain_once() -> bool:
        """Drain every hub's outbox once, recording + routing each message.

        Routing honors the row's own ``to:`` field — the tool validated it
        in-container, so an unresolvable recipient here is an invariant break
        and fails the run loudly rather than guessing a destination.

        Returns True if the conversation should terminate (max_messages or a
        delivery failure); sets ``result`` accordingly via ``_finish``.
        """
        for src in roles:
            if src not in sessions:
                continue  # role not launched yet — nothing to drain
            try:
                msgs, _hub_ended = await hubs[src].drain_outbox(outbox_seen[src])
            except Exception as e:  # noqa: BLE001 — hub may not be up yet
                logger.debug("outbox drain failed for %s: %s", src, e)
                continue

            for m in msgs:
                outbox_seen[src] = max(outbox_seen[src], int(m.get("seq") or 0))
                to_field = str(m.get("to") or "")
                dst = dst_role_of.get(to_field)
                if dst is None or dst == src:
                    await _finish(
                        "agent_failed",
                        error=(f"{src} addressed a message to {to_field!r}, "
                               f"which is not another agent in this run — the "
                               f"tool-side roster should have rejected it"),
                    )
                    return True
                content = str(m.get("content") or "")
                summary = str(m.get("summary") or "") or None
                if vis and dst not in vis.get(src, frozenset()):
                    # Out-of-graph hop: the roster never offered this name,
                    # so the tool was bypassed. Record as evidence, refuse
                    # delivery, tell the sender.
                    entry = _record_hop(src, dst, content, summary=summary)
                    entry["status"] = "blocked"
                    entry["error"] = (
                        f"delivery refused: {src} has no channel to {dst} in "
                        f"this run's visibility graph"
                    )
                    conversation.append(entry)
                    if on_hop is not None:
                        try:
                            on_hop(entry)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("on_hop callback failed: %s", e)
                    logger.warning(
                        "conversation: refused out-of-graph message %s -> %s "
                        "(visibility); the in-container roster was bypassed",
                        src, dst,
                    )
                    if len(conversation) >= max_messages:
                        await _finish("max_messages")
                        return True
                    await _notice(src, _DELIVERY_REFUSED_NOTICE.format(
                        name=to_field), round=entry["round"])
                    continue
                entry = _record_hop(src, dst, content, summary=summary)
                # The A2A envelope for this hop; the channel transforms it in
                # flight. ``sender`` is the speaker's card name.
                outbound = A2AMessage.of_text(
                    src_role=src, dst_role=dst, round=entry["round"],
                    content=entry["reply"], context_id=context_id,
                    task_id=task_id, sender=name_of(src),
                )
                delivered = await channel.deliver(outbound)
                entry["reply"] = delivered.content
                entry["message"] = delivered.to_a2a_json()
                conversation.append(entry)
                if on_hop is not None:
                    # Streaming is durability, not correctness: a hop that
                    # fails to stream still routes.
                    try:
                        on_hop(entry)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("on_hop callback failed: %s", e)

                if len(conversation) >= max_messages:
                    await _finish("max_messages")
                    return True

                # What the recipient reads. Every delivery path below uses this
                # one value, so a message means the same thing whether it
                # starts a peer's session or lands in a live inbox.
                inbound = channel.render(delivered)
                delivered_render[dst] = inbound

                if dst not in sessions:
                    # lazy peer startup: this message is the peer's first prompt
                    _start_session(dst, inbound)
                elif dst in session_ended:
                    # Recipient's session exited: per §3.4.3 a new message
                    # resumes it (next session index), gated on
                    # supports_session_resume; otherwise tell the sender it
                    # is unreachable.
                    if resumable.get(dst):
                        logger.info(
                            "conversation: %s exited; resuming it at session %d "
                            "to deliver %s's follow-up", dst, session_count[dst],
                            src,
                        )
                        session_ended.discard(dst)
                        # It may go silent again; let that be reported afresh.
                        no_reply_reported.pop(dst, None)
                        departure_notified.pop(dst, None)
                        _start_session(dst, inbound)
                    else:
                        await _notice(src, _UNREACHABLE_NOTICE.format(
                            name=name_of(dst)), round=entry["round"])
                else:
                    try:
                        await hubs[dst].send_message(delivered, rendered=inbound)
                    except Exception as e:  # noqa: BLE001
                        await _finish("agent_failed",
                                      error=f"could not deliver to {dst}: {e}")
                        return True
        return False

    async def _completed_meanwhile() -> bool:
        """Did a hub reach ``ended`` while we were deciding something else?

        The loop polls hub state once per iteration, so a ``task_complete``
        posted after that check is invisible until the next one. Any verdict that
        would BLAME an agent must re-check first, or a clean run is recorded as a
        failure. Drains again too — the closing message may still be in flight.
        """
        for src in roles:
            if src not in sessions:
                continue
            try:
                st = await hubs[src].state()
            except Exception:  # noqa: BLE001
                continue
            if st.get("ended"):
                await _drain_once()
                if conversation:
                    conversation[-1]["ended"] = True
                await _finish("task_complete", end_reason=st.get("reason"))
                return True
        return False

    terminated = False
    # First observation of the all-blocked state (see the loop's tail).
    stalled_since: float | None = None
    while not terminated:
        if time.monotonic() >= deadline_monotonic:
            await _finish("wall_time")
            break

        if await _drain_once():
            break

        # One /state pass per launched role: the terminal flag plus each
        # LIVE role's wait filter (``waiting_on`` rides the same response).
        hub_ended = False
        ended_reason = None
        waiting_now: dict[str, str | None] = {}
        for src in roles:
            if src not in sessions:
                continue
            try:
                st = await hubs[src].state()
            except Exception:  # noqa: BLE001
                continue
            if not sessions[src].done():
                waiting_now[src] = st.get("waiting_on") or None
            if st.get("ended") and not hub_ended:
                hub_ended = True
                ended_reason = st.get("reason")
        if hub_ended:
            # Drain once MORE first: a closing send_message posted just before
            # task_complete may still be sitting in the outbox — losing it
            # would drop the agent's final message and flag the wrong hop as
            # ended.
            await _drain_once()
            if conversation:
                conversation[-1]["ended"] = True
            await _finish("task_complete", end_reason=ended_reason)
            terminated = True
            break

        # Sessions that exited on their own (agent CLI finished) without the
        # task being marked complete. Only the ASSIGNED role's failure ends
        # the run — anyone else's departure is announced to the agents
        # blocked on it and the conversation continues (a market survives one
        # dead participant).
        for r, task in list(sessions.items()):
            # A relaunch may have REPLACED sessions[r] since this list was taken;
            # only judge the current one, or a resumed role reads as departed.
            if sessions.get(r) is not task or not task.done():
                continue
            try:
                rr = task.result()
            except Exception as e:  # noqa: BLE001
                if r == assigned_role:
                    await _finish("agent_failed", error=f"{r} session raised: {e}")
                    terminated = True
                    break
                logger.warning(
                    "conversation: %s session raised (%s); the run continues", r, e,
                )
                result.session_results[r] = e
                session_ended.add(r)
                resumable[r] = False  # a crashed harness is not resumed
                if await _notify_departure(r, waiting_now):
                    terminated = True
                    break
                continue
            result.session_results[r] = rr
            # This role has no live process from here on: anything addressed
            # to it lands in its hub inbox until a resume delivers it (see the
            # session_ended branch in _drain_once).
            session_ended.add(r)
            status = getattr(rr, "status", "completed")
            if status != "completed":
                if r == assigned_role:
                    await _finish(
                        "agent_failed",
                        error=f"{r} session {status}: {getattr(rr, 'error', None)}",
                    )
                    terminated = True
                    break
                logger.warning(
                    "conversation: %s session %s (%s); the run continues",
                    r, status, getattr(rr, "error", None),
                )
                resumable[r] = False
                if await _notify_departure(r, waiting_now):
                    terminated = True
                    break
                continue
            # Completed cleanly but nothing terminal happened. On the FIRST
            # poll seeing this exit, drain once more (a message posted just
            # before the process exited may still sit in the outbox — losing
            # it would blame the agent for a host race) and sweep its inbox;
            # later polls skip the HTTP, nothing new can land there.
            if r not in exit_swept:
                exit_swept.add(r)
                if await _drain_once():
                    terminated = True
                    break
                if sessions.get(r) is not task:
                    continue  # that drain relaunched r — it's live again
                # Mail delivered while alive but never claimed: a resumable
                # role reads it in a resumed session; otherwise it is lost.
                try:
                    pending = await hubs[r].claim_inbox()
                except Exception as e:  # noqa: BLE001
                    logger.debug("pending-mail claim for %s failed: %s", r, e)
                    pending = []
                if pending and resumable.get(r):
                    batch = "\n\n---\n\n".join(
                        str(p.get("content") or "") for p in pending
                    )
                    logger.info(
                        "conversation: %s exited with %d unread message(s); "
                        "resuming it at session %d to deliver them",
                        r, len(pending), session_count[r],
                    )
                    session_ended.discard(r)
                    no_reply_reported.pop(r, None)
                    departure_notified.pop(r, None)
                    delivered_render[r] = batch
                    _start_session(r, batch)
                    continue
                if pending:
                    # Routed but never read, and no reader will come — leave
                    # an explicit artifact rather than dropping silently.
                    senders = sorted(
                        {str(p.get("from") or "relay") for p in pending}
                    )
                    logger.warning(
                        "conversation: %s exited non-resumable with %d "
                        "unread message(s) from %s; they will never be read",
                        r, len(pending), ", ".join(senders),
                    )
                    if on_event is not None:
                        try:
                            on_event("undeliverable_mail", role=r,
                                     count=len(pending), senders=senders)
                        except Exception as e:  # noqa: BLE001
                            logger.warning("on_event callback failed: %s", e)
            if turn_index[r] == 0:
                # No addressed message = InvalidAgentResponseError (§3.3.2),
                # reported to each waiter (per WAITER, so a later one still
                # gets it); each may re-ask (§3.4.3) or wrap up.
                waiters = _live_waiters_on(r, waiting_now)
                told = no_reply_reported.setdefault(r, set())
                for w in waiters:
                    if w in told:
                        continue
                    logger.info(
                        "conversation: %s returned no addressed "
                        "message; reporting the error to %s", r, w,
                    )
                    try:
                        await _notify_waiter(
                            w, _NO_REPLY_NOTICE.format(name=name_of(r)))
                    except Exception as e:  # noqa: BLE001
                        await _finish(
                            "agent_failed",
                            error=f"could not report no-reply to {w}: {e}",
                        )
                        terminated = True
                        break
                    told.add(w)
                if terminated:
                    break
                if waiters:
                    continue  # a waiter holds the ball; don't terminate
                # Nobody is waiting on it. A silent ASSIGNED role can never
                # complete its task, so the run is over — but re-check first:
                # it may have completed since the last hub poll, and blaming
                # it then would fail a run that actually succeeded.
                if r == assigned_role:
                    if await _completed_meanwhile():
                        terminated = True
                        break
                    await _finish("invalid_agent_response",
                                  error=f"{r} ended without sending a message")
                    terminated = True
                    break
                continue  # a parked, silent extra — future mail may resume it
            # It did send, then left. Anyone blocked on a reply from it gets
            # the departure notice; everyone else keeps trading.
            if await _notify_departure(r, waiting_now):
                terminated = True
                break
        if terminated:
            break

        # Nothing live is left: pending-mail resumes were handled above, and
        # a message can only originate from a live session, so the run cannot
        # make progress again. (Roles never launched have no session entry —
        # they only ever start on someone's message.)
        if sessions and all(t.done() for t in sessions.values()):
            if await _completed_meanwhile():
                terminated = True
                break
            if turn_index[assigned_role] == 0:
                await _finish(
                    "invalid_agent_response",
                    error=f"{assigned_role} ended without sending a message",
                )
            else:
                await _finish(
                    "all_agents_exited",
                    error="every agent exited without marking the task complete",
                )
            terminated = True
            break

        # Deadlock: every live agent is blocked in the messaging service, so
        # none can originate another message. This includes wait cycles among
        # live roles (A waits on B, B on C, C on A), not only waits targeting
        # a departed role. A matching in-flight delivery clears ``waiting_on``;
        # the grace period lets that claim settle before declaring the cycle.
        live = [w for w, t in sessions.items() if not t.done()]
        stalled = bool(live) and all(waiting_now.get(w) for w in live)
        if stalled:
            if stalled_since is None:
                stalled_since = time.monotonic()
            elif time.monotonic() - stalled_since >= stall_grace_s:
                if await _completed_meanwhile():
                    break
                detail = "; ".join(
                    f"{w} waiting on {waiting_now[w]}" for w in live
                )
                await _finish(
                    "all_agents_blocked",
                    error=("every live agent is blocked on a wait no agent "
                           "can ever satisfy: " + detail),
                )
                break
        else:
            stalled_since = None

        await asyncio.sleep(poll_interval_s)

    # Reap sessions (they should exit once shutdown/complete propagates).
    # sessions[r] is the role's LAST session, so its result wins over any earlier
    # one: a resumed role is described by how it finished.
    for r, task in sessions.items():
        if task.done():
            try:
                result.session_results[r] = task.result()
            except Exception as e:  # noqa: BLE001
                result.session_results[r] = e
            continue
        try:
            grace = max(5.0, deadline_monotonic - time.monotonic())
            result.session_results[r] = await asyncio.wait_for(task, timeout=grace)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            result.session_results[r] = None

    result.conversation = conversation
    return result
