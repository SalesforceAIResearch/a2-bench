#!/usr/bin/env python3
"""a2a hub — tiny in-container HTTP server (stdlib only), the container's
PID 1, and this agent's A2A endpoint. The host is the A2A client on both sides,
taking a Message out of one hub and issuing SendMessage against the other's.

Two surfaces, deliberately separate:

  A2A wire (agent ↔ agent, spec v1.0 §9 JSONRPC binding)
    GET  /.well-known/agent-card.json   this agent's card (§14.3)
    POST /rpc                           SendMessage | GetTask | CancelTask

  Local (this agent ↔ its own hub; below the protocol, per Appendix B)
    GET  /status              readiness probe → {"status": "ok"}
    GET  /state               → {"ended", "reason", "ended_at", "task_state"}
    POST /outbox              agent tool enqueues an outbound message
    GET  /outbox?since=<seq>  host drains outbound messages (seq > since)
    GET  /inbox?since=<seq>   agent tool long-polls for delivered messages
                              (read-only view; claiming is what consumes)
    POST /inbox/claim         atomically claim unconsumed inbox messages
                              {"from": name?, "max": n?} — relay notices
                              (senderless) match ANY ``from`` filter, so a
                              release notice can never be filtered out of the
                              wait it exists to unblock
    POST /waiting             agent tool declares what it is blocked on
                              {"on": "<agent>"|"*"|null} → /state.waiting_on
    POST /task_complete       this agent declares its task COMPLETED
    POST /agent-card          host publishes the card to serve
    POST /inbox               deprecated host delivery (pre-JSONRPC runs)

Any harness whose tool speaks the local contract works unchanged.
ThreadingHTTPServer so a blocked agent poll never stalls the host drain; a lock
guards the shared state.

Stdlib only, no ``a2_run`` imports — this file is copied into the image, so the
protocol constants are mirrored below and must stay equal to a2_run's.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---- mirrored from a2_run.protocols --------------------------------------
SEND_MESSAGE = "SendMessage"
GET_TASK = "GetTask"
CANCEL_TASK = "CancelTask"
ROLE_AGENT = "ROLE_AGENT"

TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
TASK_STATE_WORKING = "TASK_STATE_WORKING"
TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
TASK_STATE_CANCELED = "TASK_STATE_CANCELED"

UNSUPPORTED_OPERATION = -32004
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600

_LOCK = threading.Lock()
_STATE = {
    "ended": False,
    "reason": None,      # set by /task_complete (this agent completed) …
    "ended_at": None,
    "shutdown": False,   # … or by CancelTask (peer ended; we should wrap up)
    # §4.1.3. Distinguishes "I completed my task" from "something else ended
    # the run" — one `ended` flag could not.
    "task_state": TASK_STATE_SUBMITTED,
    # What this agent's tool is currently blocked on: an agent name, "*"
    # (any sender), or None (not waiting). Written via POST /waiting; the
    # host reads it to aim no-reply/unreachable notices at actual waiters.
    "waiting_on": None,
}
_INBOX: list[dict] = []   # messages delivered to this agent (host → agent)
_OUTBOX: list[dict] = []  # messages this agent sent (agent → host → peer)

#: Served at the well-known URI. The hub is the container entrypoint, so it
#: starts before roles are bound: the host publishes the card, and the URI
#: 404s until it does.
_CARD: dict = {}


def _now() -> float:
    return time.time()


def _text_of(message: dict) -> str:
    """Concatenated text of an A2A Message's text parts (§4.1.6)."""
    out = []
    for part in message.get("parts") or ():
        if isinstance(part, dict) and part.get("text"):
            out.append(str(part["text"]))
    return "".join(out)


def _deliver(message: dict, *, rendered: str, round_: object) -> int:
    """Append an inbound Message to the inbox → its seq.

    ``rendered`` is what the agent reads (the channel's rendering); the full
    message rides alongside so the artifact keeps every protocol field.
    """
    metadata = message.get("metadata") or {}
    sender = str(metadata.get("a2aSenderName") or "") or None
    with _LOCK:
        seq = len(_INBOX) + 1
        _INBOX.append({
            "seq": seq,
            "from": sender,
            "round": round_,
            "content": rendered,
            "message": message,
            "ts": _now(),
            # Claiming (not reading) consumes a row; /inbox stays a view.
            "consumed": False,
            # Senderless mail is from the relay, not an agent; it must satisfy
            # every from-filtered claim or a release notice deadlocks the
            # exact wait it was sent to unblock.
            "notice": bool(metadata.get("a2aRelayNotice")) or sender is None,
        })
        if _STATE["task_state"] == TASK_STATE_SUBMITTED:
            _STATE["task_state"] = TASK_STATE_WORKING
    return seq


def _end(reason: str | None, task_state: str, *, shutdown: bool) -> dict:
    """Move to a terminal state. Idempotent — the first end wins."""
    with _LOCK:
        if shutdown:
            _STATE["shutdown"] = True
        if not _STATE["ended"]:
            _STATE["ended"] = True
            _STATE["reason"] = (reason or "").strip() or None
            _STATE["ended_at"] = _now()
            _STATE["task_state"] = task_state
        return {k: _STATE[k] for k in
                ("ended", "reason", "ended_at", "task_state")}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _since(query: str) -> int:
        try:
            return int((parse_qs(query).get("since") or ["0"])[0])
        except (TypeError, ValueError):
            return 0

    # ---- GET --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/status":
            self._send(200, {"status": "ok"})
            return
        if path == "/.well-known/agent-card.json":
            if not _CARD:
                self._send(404, {"error": "no agent card staged for this hub"})
            else:
                self._send(200, _CARD)
            return
        if path == "/state":
            with _LOCK:
                self._send(200, {k: _STATE[k] for k in
                                 ("ended", "reason", "ended_at", "task_state",
                                  "waiting_on")})
            return
        if path in ("/inbox", "/outbox"):
            since = self._since(parsed.query)
            box = _INBOX if path == "/inbox" else _OUTBOX
            with _LOCK:
                msgs = [m for m in box if m["seq"] > since]
                ended = _STATE["ended"] or _STATE["shutdown"]
            self._send(200, {"messages": msgs, "ended": ended})
            return
        self._send(404, {"error": f"unknown path {path}"})

    # ---- POST -------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload = self._read_body()

        if path == "/rpc":
            self._handle_rpc(payload)
            return

        if path == "/outbox":
            content = str(payload.get("content") or "")
            with _LOCK:
                seq = len(_OUTBOX) + 1
                _OUTBOX.append({
                    "seq": seq,
                    "to": str(payload.get("to") or "") or None,
                    "content": content,
                    # Sender-authored gist; rides beside the content into the
                    # hop log for reviewers, never altering delivery.
                    "summary": str(payload.get("summary") or "") or None,
                    "ts": _now(),
                })
                if _STATE["task_state"] == TASK_STATE_SUBMITTED:
                    _STATE["task_state"] = TASK_STATE_WORKING
            self._send(200, {"ok": True, "seq": seq})
            return

        if path == "/inbox":
            # Pre-JSONRPC host delivery. Kept so a host and an image at
            # different versions still interoperate; the A2A path is /rpc.
            seq = _deliver(
                {"parts": [{"kind": "text", "text": str(payload.get("content") or "")}],
                 "role": ROLE_AGENT,
                 "metadata": {"a2aSenderName": str(payload.get("from") or "")}},
                rendered=str(payload.get("content") or ""),
                round_=payload.get("round"),
            )
            self._send(200, {"ok": True, "seq": seq})
            return

        if path == "/inbox/claim":
            # Atomic claim: return unconsumed rows matching the filter and
            # mark them consumed in the same critical section, so two
            # concurrent waits can never hand the same message to both.
            from_ = str(payload.get("from") or "") or None
            try:
                max_ = int(payload["max"]) if payload.get("max") is not None else None
            except (TypeError, ValueError):
                self._send(400, {"error": "'max' must be an integer"})
                return
            with _LOCK:
                claimed = []
                for m in _INBOX:
                    if m["consumed"]:
                        continue
                    if from_ is not None and m["from"] != from_ and not m["notice"]:
                        continue
                    m["consumed"] = True
                    claimed.append(m)
                    if max_ is not None and len(claimed) >= max_:
                        break
                ended = _STATE["ended"] or _STATE["shutdown"]
            self._send(200, {"messages": claimed, "ended": ended})
            return

        if path == "/waiting":
            on = payload.get("on")
            if on is not None and not isinstance(on, str):
                self._send(400, {"error": "'on' must be an agent name, '*', or null"})
                return
            with _LOCK:
                _STATE["waiting_on"] = (on or "").strip() or None
            self._send(200, {"ok": True})
            return

        if path == "/task_complete":
            state = _end(str(payload.get("reason") or ""),
                         TASK_STATE_COMPLETED, shutdown=False)
            self._send(200, {"ok": True, "state": state})
            return

        if path == "/agent-card":
            card = payload.get("card")
            if not isinstance(card, dict):
                self._send(400, {"error": "expected {\"card\": {...}}"})
                return
            with _LOCK:
                _CARD.clear()
                _CARD.update(card)
            self._send(200, {"ok": True})
            return

        if path == "/shutdown":
            # Pre-JSONRPC host termination; the A2A path is CancelTask.
            state = _end(str(payload.get("reason") or ""),
                         TASK_STATE_CANCELED, shutdown=True)
            self._send(200, {"ok": True, "state": state})
            return

        self._send(404, {"error": f"unknown path {path}"})

    # ---- A2A JSON-RPC binding (§9) ---------------------------------------
    def _handle_rpc(self, payload: dict) -> None:
        """Dispatch one JSON-RPC request (§9.3 base structure)."""
        msg_id = payload.get("id")
        if payload.get("jsonrpc") != "2.0" or not payload.get("method"):
            self._rpc_error(msg_id, INVALID_REQUEST,
                            "expected a JSON-RPC 2.0 request with a method")
            return
        method = str(payload.get("method"))
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}

        if method == SEND_MESSAGE:
            message = params.get("message")
            if not isinstance(message, dict):
                self._rpc_error(msg_id, INVALID_PARAMS,
                                "SendMessage requires params.message")
                return
            with _LOCK:
                ended = _STATE["ended"]
            if ended:
                # Refused rather than silently dropped (§5.4).
                self._rpc_error(msg_id, UNSUPPORTED_OPERATION,
                                "task is in a terminal state; no further "
                                "messages accepted")
                return
            rendered = str(params.get("rendered") or "") or _text_of(message)
            seq = _deliver(message, rendered=rendered,
                           round_=params.get("round"))
            # §9.4.1 allows a Task or a Message; delivery is acked with this
            # agent's task snapshot, since a hub cannot know when its agent
            # will answer. The reply is a later SendMessage the other way.
            with _LOCK:
                state = _STATE["task_state"]
            self._rpc_result(msg_id, {"task": {
                "id": str(message.get("taskId") or ""),
                "contextId": str(message.get("contextId") or ""),
                "status": {"state": state},
                "metadata": {"deliveredSeq": seq},
            }})
            return

        if method == GET_TASK:
            with _LOCK:
                snap = dict(_STATE)
            self._rpc_result(msg_id, {"task": {
                "id": str(params.get("id") or ""),
                "status": {"state": snap["task_state"]},
                "metadata": {
                    "reason": snap["reason"],
                    "endedAt": snap["ended_at"],
                },
            }})
            return

        if method == CANCEL_TASK:
            state = _end(str(params.get("reason") or ""),
                         TASK_STATE_CANCELED, shutdown=True)
            self._rpc_result(msg_id, {"task": {
                "id": str(params.get("id") or ""),
                "status": {"state": state["task_state"]},
            }})
            return

        if method in ("SendStreamingMessage", "SubscribeToTask", "ListTasks"):
            # Declared unsupported rather than absent: a client learns the
            # operation exists in A2A but not on this binding (§5.4).
            self._rpc_error(msg_id, UNSUPPORTED_OPERATION,
                            f"{method} is not supported by this binding")
            return

        self._rpc_error(msg_id, METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _rpc_result(self, msg_id, payload: dict) -> None:
        self._send(200, {"jsonrpc": "2.0", "id": msg_id, "result": payload})

    def _rpc_error(self, msg_id, code: int, message: str) -> None:
        # JSON-RPC carries the error in the body; HTTP stays 200 (§9.5).
        self._send(200, {"jsonrpc": "2.0", "id": msg_id,
                         "error": {"code": code, "message": message}})

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"a2a hub listening on :{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
