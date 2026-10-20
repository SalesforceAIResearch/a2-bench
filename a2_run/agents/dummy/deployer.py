"""DummyDeployer — a no-LLM scripted responder for a2a smoke tests.

Purpose: exercise the whole pair (a2a) pipeline — the blocking
``a2a_send_message`` rendezvous, the host conversation driver, hub-based
termination — WITHOUT spending model tokens.

With the blocking-a2a_send_message design each role runs ONE long-lived
session (``launch_turn`` is called once, turn 0). The dummy models exactly
what the real in-container MCP tool does, but in-process against the same
``a2a_hub`` HTTP contract via ``A2A_HUB_URL``:

* it is handed an opening prompt (the assignment for the assigned role, or
  the peer's first message otherwise);
* each exchange it POSTs a scripted reply to the hub ``/outbox``, publishes
  what it waits on via ``/waiting``, and long-polls ``/inbox/claim`` for the
  peer's response (this is the blocking a2a_send_message — claim-based, so
  consumed mail is consumed exactly once);
* after ``a2a_end_after`` sends the assigned dummy POSTs ``/task_complete``
  (proving host-side termination + peer shutdown); a non-assigned dummy just
  keeps replying until the conversation ends.

Pair it with itself:

  agents:
    asker:  configs/agents/dummy_a2a.yaml
    oracle: configs/agents/dummy_a2a.yaml
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import ClassVar

from a2_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
)

from .config import DummyConfig

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.1
_HTTP_TIMEOUT_S = 15.0


def _hub_post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _hub_get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base.rstrip("/") + path, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class DummyDeployer(BaseAgentDeployer):
    """No-LLM scripted a2a responder. Runs on the host, drives the hub."""

    default_executor: ClassVar[str] = "local"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"local"})
    supports_a2a: ClassVar[bool] = True

    @property
    def version(self) -> str | None:
        return "dummy-0.5.0"

    # =========================================================================
    # install — just ensure work_dir
    # =========================================================================

    async def install(self) -> None:
        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            "dummy: install ok (work_dir=%s, executor=%s, os=%s)",
            self.executor.work_dir,
            self.executor.type,
            getattr(self.executor.sandbox, "os", "?"),
        )

    # =========================================================================
    # launch_turn — one long-lived hub-driven session (no LLM)
    # =========================================================================

    async def launch_turn(
        self, message: str, *, turn_index: int, standing_context: str | None = None,
    ) -> AgentRunResult:
        """Drive the whole conversation for this role via the hub.

        ``standing_context`` is accepted for interface parity and ignored.
        ``message`` is this role's opening prompt. The dummy replies via the
        hub (blocking a2a_send_message semantics) until either the
        conversation ends or, if it owns the task, it has sent
        ``a2a_end_after`` messages and marks the task complete.
        """
        cfg: DummyConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()

        hub = (self.executor.env or {}).get("A2A_HUB_URL") or os.environ.get(
            "A2A_HUB_URL"
        )
        if not hub:
            return AgentRunResult(
                status="failed",
                error="dummy a2a: no A2A_HUB_URL in executor env",
            )
        peers = tuple(
            p.strip() for p in
            ((self.executor.env or {}).get("A2A_PEERS")
             or os.environ.get("A2A_PEERS") or "").split(",")
            if p.strip()
        )
        can_complete = (
            ((self.executor.env or {}).get("A2A_CAN_COMPLETE")
             or os.environ.get("A2A_CAN_COMPLETE") or "").strip() == "1"
        )
        recipient = peers[0] if peers else "peer"

        replies = tuple(cfg.a2a_replies or ())
        end_after = int(cfg.a2a_end_after or 0)

        # The session body is synchronous blocking HTTP (mirroring the real
        # in-container MCP tool). It MUST run off the event loop, or it would
        # starve the host conversation driver awaiting on the same loop.
        def _session() -> dict:
            log: list[dict] = []
            incoming = message
            sends = 0
            while True:
                reply = (replies[sends % len(replies)] if replies
                         else f"[dummy send {sends}] ack: {incoming[-160:]}")
                # blocking a2a_send_message: post outbox, publish the wait,
                # then long-poll the claim endpoint for mail from the peer
                # (claiming is what consumes — a bare GET /inbox is a view)
                _hub_post(hub, "/outbox", {"to": recipient, "content": reply})
                sends += 1
                log.append({"sent": reply, "in_reply_to": incoming})

                # assigned dummy completes after its scripted quota of sends
                if can_complete and end_after and sends >= end_after:
                    _hub_post(hub, "/task_complete", {
                        "reason": f"dummy scripted end after {sends} sends"})
                    break

                got = None
                _hub_post(hub, "/waiting", {"on": recipient})
                try:
                    while got is None:
                        resp = _hub_post(hub, "/inbox/claim",
                                         {"from": recipient, "max": 1})
                        msgs = resp.get("messages") or []
                        if msgs:
                            got = msgs[0]
                        elif resp.get("ended"):
                            break
                        else:
                            time.sleep(_POLL_INTERVAL_S)
                finally:
                    try:
                        _hub_post(hub, "/waiting", {"on": None})
                    except Exception:  # noqa: BLE001 — hub likely gone
                        pass
                if got is None:  # conversation ended while waiting
                    log.append({"ended": True})
                    break
                incoming = got["content"]
            return {"sends": sends, "log": log}

        out = await asyncio.to_thread(_session)

        (work_dir / "dummy_session.json").write_text(
            json.dumps(out, indent=2), encoding="utf-8")
        logger.info("dummy: session done (sends=%d, can_complete=%s)",
                    out["sends"], can_complete)
        return AgentRunResult(
            status="completed",
            duration_s=time.monotonic() - t0,
            transcript_path=str(work_dir / "dummy_session.json"),
            reply_text=None,  # wire is the hub, not this field
        )
