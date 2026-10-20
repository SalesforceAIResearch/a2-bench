"""Run-unit (a2a) lifecycle: one agent and one sandbox per role, one
host-routed conversation.

Provisions one sandbox per role, stages task data, runs the N-agent
conversation via :mod:`~a2_run.orchestration.conversation` (one long-lived
session per role; each role's hub is its only wire), then gathers
artifacts, builds per-role trajectories, and evaluates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from ..base_interface import (
    AgentRunResult,
    BaseExecutor,
    SandboxSpec,
    Trajectory,
    TrajectoryBuilder,
)
from ..environments.egress import backend_domains
from ..environments.env import A2Env
from ..protocols import (
    A2A_PROTOCOL_VERSION,
    CHANNEL_REGISTRY,
    AgentCard,
    task_state,
)
from ..tasks.loader import TaskLoader
from ..tasks.driver import TaskDriver
from .conversation import HostHub, HubClient, run_conversation
from .factory import EnvironmentRouter, build_config, resolve_agent
from .unit_common import (
    DEFAULT_TIMEOUT_S,
    EVAL_TIMEOUT_S,
    build_executor,
    extract_breakdown,
    extract_score,
    stage_task_data,
    stage_task_reference,
)
from .run_writer import RunWriter, slug_task
from .experiment_spec import A2ASpec, ArtifactsSpec, PairRunUnit, UnitResult
from .termination import classify_error, err_dict, redact_config

logger = logging.getLogger(__name__)

#: Termination reasons that make the run a failure (vs a timeout or a clean end).
_FAILED_TERMINATIONS = frozenset(
    {"agent_failed", "invalid_agent_response", "all_agents_exited",
     "all_agents_blocked"}
)


# Each role's standing context (persona brief + the sections below) is
# installed by the deployer as ordinary workspace config (CLAUDE.md /
# AGENTS.md), never as a system prompt, and must read like real deployment
# config — no pair/evaluation framing. Agent-facing wording uses ONE noun for
# the other party ("agent"), matching a2a_list_agents and the Agent Card.
# Tools are ``a2a_``-prefixed (not bare ``send_message``/``list_agents``)
# because several harnesses ship native tools under those exact bare names
# for their own agent-team collaboration feature; a bare name here would
# collide and the model's calls would never reach this messaging service.
_MESSAGING_HEAD = (
    "## Messaging\n\n"
    "This machine is connected to the inter-agent messaging service. Use "
    "`a2a_list_agents` to see which agents you can reach and what each one "
    "does. To contact one, use the `a2a_send_message` tool: it delivers your "
    "`content` to the named agent and, by default, waits for that agent's "
    "reply and returns it — messages from other agents that arrive in the "
    "meantime are held for you. To write to several agents before reading "
    "any reply, pass `wait_for_reply: false`; the call then returns as soon "
    "as the message is handed to the service, and `a2a_wait_for_messages` "
    "collects whatever has arrived (optionally from one named agent). The "
    "agent you write to sees only the `content` you send and nothing else "
    "(not this workspace or its files), so make each message "
    "self-contained.\n\n"
)

_MESSAGING_TAIL = (
    "Reply by calling `a2a_send_message` back to that agent: one message in, "
    "one message out. Your written output is for whoever assigned you the "
    "work; it reaches no agent, so nothing you leave there is delivered.\n"
)

# How an arriving message identifies its sender — one paragraph per channel,
# keyed by name so the config and these instructions cannot describe different
# envelopes.
_INBOUND_BY_CHANNEL = {
    "direct": (
        "A message that arrives for you names the agent it came from on a "
        "`from:` line — the same name that agent is listed under in "
        "`a2a_list_agents`, so it is the name to use as `to` when you "
        "answer.\n\n"
    ),
    "a2a": (
        "A message that arrives for you opens with an `[a2a message]` header "
        "carrying the message's protocol fields: `from:` is the sending "
        "agent, listed under that same name in `a2a_list_agents`, so it is "
        "the name to use as `to` when you answer; `role:` is the sender's "
        "A2A role; `messageId`, `contextId` and `taskId` identify the "
        "message and the work it belongs to. Everything after the blank "
        "line is the agent's own text.\n\n"
    ),
}

_COMPLETION_SECTION = (
    "Work items assigned to you arrive as your first message. When an "
    "assigned item is fully wrapped up and nothing further is needed from you "
    "or from any other agent, record it with the `a2a_task_complete` tool — "
    "that closes the item for every agent involved. It is not a way to wait "
    "for a reply; `a2a_send_message` waits by default, and "
    "`a2a_wait_for_messages` waits otherwise.\n"
)


# ======================================================================
# Public entry
# ======================================================================


async def run_pair_unit(
    *,
    unit: PairRunUnit,
    router: "EnvironmentRouter",
    output_root: Path,
    a2a: A2ASpec,
    artifacts: ArtifactsSpec | None = None,
    sem: asyncio.Semaphore | None = None,
    cleanup_mode: str = "delete",
    wall_time_s: int | None = None,
) -> UnitResult:
    started = time.monotonic()

    # ---- 1. Resolve each role's deployers + configs ----
    try:
        resolved: dict[str, tuple[Any, Any]] = {}   # role → (deployer_cls, config)
        executor_types: dict[str, str] = {}
        for role, agent_spec in unit.roles.items():
            deployer_cls, config_cls = resolve_agent(agent_spec)
            if not getattr(deployer_cls, "supports_a2a", False):
                raise TypeError(
                    f"role {role!r}: {deployer_cls.__name__} does not support "
                    f"a2a turns (supports_a2a=False)"
                )
            config = build_config(config_cls, agent_spec.config)
            resolved[role] = (deployer_cls, config)
            executor_types[role] = (
                agent_spec.executor
                or getattr(deployer_cls, "default_executor", "")
                or "sandbox"
            )
    except Exception as e:
        logger.exception("resolve failed for %s: %s", unit.slug, e)
        return UnitResult(
            unit=unit,
            status="not_executed",
            eval_status="not_executed",
            duration_s=round(time.monotonic() - started, 2),
            error=str(e),
        )

    # ---- 2. RunWriter (one dir per run unit) ----
    writer = RunWriter(
        output_root=output_root,
        agent_id=unit.agent_id,
        model=unit.model_id,
        task_path=unit.task_path,
        variant_index=unit.variant_index,
        variant_name=unit.variant_name,
    )
    writer.emit_event(
        "run_started",
        mode="a2a",
        roles={
            role: {"class": s.class_, "model": s.config.get("model")}
            for role, s in unit.roles.items()
        },
        task=unit.task_path,
        variant_index=unit.variant_index,
        variant_name=unit.variant_name,
        a2a={"max_messages": a2a.max_messages, "channel": a2a.channel},
    )

    envs: dict[str, A2Env] = {}
    executors: dict[str, BaseExecutor] = {}
    task_driver: TaskDriver | None = None
    conversation: list[dict[str, Any]] = []
    trajectories: dict[str, Trajectory] = {}
    termination_reason: str | None = None
    end_reason_text: str | None = None

    status = "completed"
    score: float | None = None
    error_str: str | None = None
    error_obj: dict[str, Any] | None = None
    eval_status = "not_executed"
    eval_duration_s: float | None = None
    eval_error: dict[str, Any] | None = None
    eval_breakdown: dict[str, Any] | None = None
    exec_started: float | None = None
    exec_ended: float | None = None

    try:
        if sem is not None:
            writer.emit_event("provision_wait")
            await sem.acquire()
        exec_started = time.monotonic()
        try:
            # ---- 3. Load task; require + validate roles ----
            task_path = Path("tasks") / unit.task_path
            task_meta = TaskLoader(str(task_path)).load(unit.variant_index)
            conversion_manifest = (
                (task_meta.get("metadata") or {}).get("a2_conversion") or {}
            )
            if conversion_manifest:
                if not isinstance(conversion_manifest, dict):
                    raise TypeError(
                        f"task {unit.task_path}: metadata.a2_conversion must be a mapping"
                    )
                # Persist only the interaction condition needed to interpret
                # an archived trace. Resource-level hashes stay in the corpus
                # registry instead of being duplicated into every run.
                scenario_keys = (
                    "schema_version", "recipe_sha256", "source", "target",
                    "assigned_role", "principal_relationship",
                    "partition_strategy", "partition_label",
                    "demanded_capabilities", "roles", "capabilities",
                )
                writer.write_scenario({
                    key: conversion_manifest[key]
                    for key in scenario_keys
                    if key in conversion_manifest
                })
            role_briefs: dict[str, str] = task_meta.get("roles") or {}
            if not role_briefs:
                raise RuntimeError(
                    f"task {unit.task_path} declares no roles — not an a2a task "
                    "(its main.py load() must return Task objects with .roles "
                    "and .first_speaker)"
                )
            if set(role_briefs) != set(unit.roles):
                raise RuntimeError(
                    f"role mismatch: experiment binds {sorted(unit.roles)} but "
                    f"task {unit.task_path} declares {sorted(role_briefs)}"
                )
            first_speaker = task_meta["first_speaker"]
            # A2A framing: the task is ASSIGNED to one role (it opens the
            # exchange and is the only one that can a2a_task_complete); peers are
            # discovered via agent cards. ``assignment`` is the work item
            # text delivered as the assigned agent's first input.
            assigned_role = str(
                task_meta.get("assigned_role") or first_speaker
            )
            if assigned_role != first_speaker:
                raise RuntimeError(
                    f"task {unit.task_path}: assigned_role {assigned_role!r} "
                    f"must be the first_speaker {first_speaker!r} (the "
                    "assigned agent opens the exchange)"
                )
            assignment = str(task_meta.get("assignment") or "")
            cards: dict[str, AgentCard] = {
                role: AgentCard.from_dict(raw)
                for role, raw in (task_meta.get("agent_cards") or {}).items()
            }
            # Per-role peer allow-list (visibility graph). Empty ⇒ full mesh.
            visibility: dict[str, list[str]] = task_meta.get("visibility") or {}
            if visibility:
                # The roster (A2A_PEERS) is built from card names; an uncarded
                # role would silently fall out of it and leave its own tool's
                # validation lenient. Refuse up front.
                uncarded = sorted(r for r in unit.roles if r not in cards)
                if uncarded:
                    raise RuntimeError(
                        f"task declares a visibility topology but role(s) "
                        f"{uncarded} have no agent card; every role needs a "
                        f"card so the roster can bind the graph in-container"
                    )
            timeout_s = int(
                wall_time_s or task_meta.get("timeout_s") or DEFAULT_TIMEOUT_S
            )

            # ============================================================
            # Phase 0 — provision one sandbox per role
            # ============================================================
            snapshot = task_meta.get("image_category") or task_meta.get("snapshot_name")
            if not snapshot:
                raise RuntimeError("task.yaml is missing vm.snapshot (required)")
            provider = router.provider_for(snapshot)
            # Per-role network egress policy for this variant: {role:
            # "restricted"}. Absent ⇒ every role open (historical default).
            # HOST-SIDE ONLY — never staged into a sandbox or shown to an agent.
            # A restricted role's concrete allowlist is derived here from the
            # harness/provider bound to it (its own model-backend hosts), so no
            # endpoint strings live in the task file and a restricted agent can
            # still reach its model while the open internet is cut.
            egress_policy: dict[str, str] = task_meta.get("egress") or {}
            writer.emit_event("provision_started", roles=list(unit.roles))
            for role, agent_spec in unit.roles.items():
                egress_mode = egress_policy.get(role, "open")
                egress_allow: tuple[str, ...] = ()
                if egress_mode == "restricted":
                    # backend_domains fails loud on an unknown harness/provider
                    # rather than returning an empty allowlist (which would
                    # strand the agent with no path to its own model).
                    egress_allow = backend_domains(
                        agent_spec.class_,
                        str(agent_spec.config.get("provider") or ""),
                    )
                env_spec = SandboxSpec(
                    snapshot=snapshot,
                    os=task_meta.get("os_type") or "linux",
                    machine_type=task_meta.get("machine_type"),
                    vcpus=task_meta.get("vcpus"),
                    memory_gb=task_meta.get("memory_gb"),
                    gpu=task_meta.get("gpu"),
                    # role suffix → distinct container names per role
                    task_id=f"{unit.task_path}-{role}",
                    harness=agent_spec.class_,
                    model_tag=str(agent_spec.config.get("model") or ""),
                    egress_mode=egress_mode,
                    egress_allow=egress_allow,
                )
                env = A2Env(provider=provider, spec=env_spec)
                await env.reset_async()
                envs[role] = env
                writer.emit_event("provision_done", role=role, env_id=env.sandbox.id)

            # ============================================================
            # Phase 1 — stage task data into every sandbox, then the
            # task's own setup hook (receives {role: SandboxHandle})
            # ============================================================
            sandboxes = {role: env.sandbox for role, env in envs.items()}
            for role, env in envs.items():
                env.set_phase("stage_inputs")
                # Per-role staging: each role's sandbox gets its own input/
                # (a2a data differs by role — e.g. the oracle sees the secret,
                # the asker does not). Falls back to shared data when a task
                # ships no per-role dirs.
                await stage_task_data(
                    env=env, provider=provider, artifacts=artifacts,
                    task_meta=task_meta, role=role,
                )
            for env in envs.values():
                env.set_phase("task_setup")
            # TaskDriver passes its sandbox argument straight through to the
            # task hooks — a2a task hooks are written against the dict form
            # start(cfg, sandboxes) / evaluate(cfg, sandboxes).
            task_driver = TaskDriver(
                task_path=str(task_path),
                sandbox=sandboxes,  # type: ignore[arg-type]
                variant=unit.variant_index,
                os_type=task_meta.get("os_type"),
            )
            await task_driver.setup()

            # ============================================================
            # Phase 2 — build per-role executors
            # ============================================================
            for env in envs.values():
                env.set_phase("agent_run")
            roles = list(unit.roles)
            standing_contexts: dict[str, str] = {}
            for role, agent_spec in unit.roles.items():
                deployer_cls, config = resolved[role]
                host_dir = writer.run_dir / "origin_log" / role
                host_dir.mkdir(parents=True, exist_ok=True)
                executor = build_executor(
                    executor_type=executor_types[role],
                    env=envs[role],
                    config=config,
                    agent_name=role,
                    run_id=writer.run_id,
                    host_artifacts_dir=host_dir,
                )
                executors[role] = executor
                # Roster + completion rights ride the executor env into the
                # MCP tool server: A2A_SELF/A2A_PEERS validate a2a_send_message
                # recipients; A2A_PEER_CARDS is the directory a2a_list_agents
                # returns (names + descriptions + skills); A2A_CAN_COMPLETE
                # registers a2a_task_complete only for the assigned role.
                #
                # ``visibility`` narrows each role's roster to the peers it may
                # address (e.g. a star topology where buyers reach only the
                # dealer, not each other). Absent ⇒ full mesh. The graph is
                # symmetric (resolved task-side), so gating the SEND side here
                # also gates replies: a peer this role can't see can't be
                # replied to either.
                self_card = cards.get(role)
                allowed = visibility.get(role) if visibility else None
                peer_cards = [
                    cards[r] for r in roles
                    if r != role and r in cards
                    and (allowed is None or r in allowed)
                ]
                if executor.env is not None:
                    if self_card:
                        executor.env["A2A_SELF"] = self_card.name
                    if peer_cards:
                        executor.env["A2A_PEERS"] = ",".join(
                            c.name for c in peer_cards
                        )
                        executor.env["A2A_PEER_CARDS"] = json.dumps(
                            [c.to_a2a_json() for c in peer_cards]
                        )
                    else:
                        # No reachable peers: a single-role solo run, or a role
                        # with every visibility edge removed. A2A_PEERS is
                        # legitimately absent — mark it so the harness treats
                        # the empty roster as intended, not a plumbing bug
                        # (see _blocking_cli._a2a_server_and_env).
                        executor.env["A2A_NO_PEERS"] = "1"
                    if role == assigned_role:
                        executor.env["A2A_CAN_COMPLETE"] = "1"
                standing_contexts[role] = _compose_standing_context(
                    brief=role_briefs[role],
                    can_complete=(role == assigned_role),
                    channel=a2a.channel,
                )

            # ============================================================
            # Phase 2b — the conversation (blocking a2a_send_message rendezvous)
            # ============================================================
            channel = CHANNEL_REGISTRY[a2a.channel]()
            deadline = exec_started + timeout_s

            # Resolve each role's hub URL: sandbox roles publish one from
            # inside the container (metadata['hub_url']); local roles (dummy)
            # get a host-side hub on a free port (same hub module).
            hub_urls, host_hubs = _resolve_hub_urls(executors)

            # Set each role's hub URL in its executor env so the in-container
            # MCP tool (and the dummy) posts to the right place. For sandbox
            # roles the tool already derives 127.0.0.1:<hub_port> in-container;
            # this is authoritative for the local case.
            for role in roles:
                if executors[role].env is not None:
                    executors[role].env["A2A_HUB_URL"] = hub_urls[role]

            # Publish each role's card at its hub's well-known URI (§14.3).
            # Best-effort: discovery here is by direct configuration
            # (A2A_PEER_CARDS), so an unpublished card costs the published
            # surface, not the conversation.
            for role in roles:
                card = cards.get(role)
                if not card:
                    continue
                try:
                    await HubClient(hub_urls[role]).publish_card(
                        card.to_a2a_json(url=hub_urls[role])
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("publish card for %s failed: %s", role, e)

            def _make_launcher():
                async def launch_session(role: str, prompt: str, turn_index: int):
                    """Run ONE session for ``role``.

                    ``turn_index`` counts this role's sessions: 0 opens the
                    conversation, a later index resumes the thread it left off.
                    It reaches the deployer verbatim, which is what keeps each
                    session's artifacts distinct.
                    """
                    deployer_cls, _cfg = resolved[role]
                    writer.emit_event(
                        "session_started", role=role, turn_index=turn_index,
                    )
                    remaining = deadline - time.monotonic()
                    try:
                        rr = await executors[role].run_deployer(
                            deployer_cls=deployer_cls,
                            prompt=prompt,
                            timeout_s=max(1.0, remaining),
                            turn_index=turn_index,
                            standing_context=standing_contexts[role],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:                   # noqa: BLE001
                        import traceback as _tb
                        rr = AgentRunResult(
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}",
                        )
                    writer.emit_event(
                        "session_finished", role=role, turn_index=turn_index,
                        status=rr.status,
                        # The harness's resumable thread id — with the streamed
                        # hop log this is what a checkpoint/restore needs to
                        # relaunch the role via --resume.
                        session_id=getattr(rr, "session_id", None),
                    )
                    return rr

                return launch_session

            # Opaque wire identifiers - A2A requires no particular content for
            # either field (§4.1.4: both optional, no format mandated), so a
            # fresh opaque id satisfies "grouped under one context/task"
            # without saying anything else.
            wire_context_id = f"ctx-{uuid.uuid4().hex[:12]}"
            wire_task_id = f"task-{uuid.uuid4().hex[:12]}"
            try:
                conv_res = await run_conversation(
                    roles=roles,
                    assigned_role=assigned_role,
                    assignment=assignment,
                    hub_urls=hub_urls,
                    launch_session=_make_launcher(),
                    channel=channel,
                    context_id=wire_context_id,
                    task_id=wire_task_id,
                    max_messages=a2a.max_messages,
                    deadline_monotonic=deadline,
                    # Stream every hop to hops.jsonl as it is routed, so a
                    # killed long run keeps its transcript up to the last
                    # delivered message instead of losing it with the
                    # end-of-run conversation.json.
                    on_hop=writer.emit_hop,
                    # Driver events (e.g. undeliverable_mail) → events.jsonl.
                    on_event=writer.emit_event,
                    # Enforced host-side; the in-container roster is advisory.
                    visibility=visibility,
                    # Messages are attributed by agent-card name (the identity
                    # the agent directory publishes and a2a_send_message
                    # validates), not by the internal role key.
                    sender_names={r: c.name for r, c in cards.items() if c.name},
                    # Which roles a follow-up can resume (A2A §3.4.3) rather than
                    # being told the agent is unreachable.
                    can_resume={
                        r: bool(getattr(
                            resolved[r][0], "supports_session_resume", False,
                        ))
                        for r in roles
                    },
                )
            finally:
                for hh in host_hubs:
                    hh.close()

            conversation = conv_res.conversation
            termination_reason = conv_res.termination_reason
            end_reason_text = conv_res.end_reason
            if conv_res.error and termination_reason in _FAILED_TERMINATIONS:
                error_str = conv_res.error

            # ============================================================
            # Phase 3 — gather artifacts, build trajectories, evaluate
            # ============================================================
            for role in roles:
                host_dir = writer.run_dir / "origin_log" / role
                try:
                    await executors[role].gather_dir(
                        src=executors[role].work_dir, dst=host_dir,
                    )
                except Exception as e:                          # noqa: BLE001
                    writer.emit_event(
                        "origin_log_gather_failed", role=role, error=str(e),
                    )

            # Gather each role's task output dir (the produced artifacts —
            # e.g. warehouse.db, sign-off forms) so they survive container
            # cleanup and can be inspected per run.
            _task_output_dir = getattr(
                task_meta.get("task_data"), "remote_output_dir", None,
            )
            if _task_output_dir:
                for role in roles:
                    try:
                        await executors[role].gather_dir(
                            src=_task_output_dir,
                            dst=writer.run_dir / "output" / role,
                        )
                    except Exception as e:                      # noqa: BLE001
                        writer.emit_event(
                            "output_gather_failed", role=role, error=str(e),
                        )

            for env in envs.values():
                env.set_phase("evaluation")
            # Stage each role's reference/ now — after the agents finished, so
            # the answer key was never in a sandbox during the run — so
            # evaluate() can read it (e.g. the asker's expected answer).
            for role, env in envs.items():
                try:
                    await stage_task_reference(
                        env=env, artifacts=artifacts, task_meta=task_meta,
                        role=role,
                    )
                except Exception as e:                          # noqa: BLE001
                    writer.emit_event(
                        "reference_stage_failed", role=role, error=str(e),
                    )
            eval_start = time.monotonic()
            exec_ended = eval_start
            try:
                eval_out = await asyncio.wait_for(
                    task_driver.evaluate(), timeout=EVAL_TIMEOUT_S,
                )
                eval_duration_s = round(time.monotonic() - eval_start, 4)
                if eval_out is None or eval_out.get("error"):
                    eval_status = "failed"
                    eval_error = (
                        {"type": "Exception",
                         "message": str(eval_out.get("error")),
                         "traceback": str(eval_out.get("error"))}
                        if eval_out else None
                    )
                else:
                    eval_status = "success"
                    score = extract_score(eval_out)
                    eval_breakdown = extract_breakdown(eval_out)
            except asyncio.TimeoutError:
                eval_duration_s = round(time.monotonic() - eval_start, 4)
                eval_status = "timeout"
                eval_error = {
                    "type": "TimeoutError",
                    "message": f"evaluate() exceeded {EVAL_TIMEOUT_S}s",
                }
            except Exception as e:                              # noqa: BLE001
                eval_duration_s = round(time.monotonic() - eval_start, 4)
                eval_status = "failed"
                eval_error = err_dict(e)
                logger.exception("evaluate raised for %s", unit.slug)

            # Per-role trajectories + the combined conversation log.
            traj_status = (
                "completed" if termination_reason in
                ("task_complete", "max_messages") else "failed"
            )
            for role in roles:
                deployer_cls, config = resolved[role]
                builder = _build_role_trajectory(
                    role=role,
                    deployer_cls=deployer_cls,
                    config=config,
                    agent_spec=unit.roles[role],
                    unit=unit,
                    conversation=conversation,
                    host_dir=writer.run_dir / "origin_log" / role,
                    instruction=standing_contexts.get(role) or role_briefs[role],
                )
                trajectories[role] = builder.finalize(
                    reward=score, status=traj_status,
                )
                writer.write_trajectory(
                    trajectories[role], filename=f"trajectory_{role}.json",
                )
            _write_conversation_json(
                run_dir=writer.run_dir,
                first_speaker=first_speaker,
                channel=a2a.channel,
                termination_reason=termination_reason,
                end_reason_text=end_reason_text,
                conversation=conversation,
            )

            # ---- status promotion ----
            if termination_reason in _FAILED_TERMINATIONS:
                status = "failed"
                error_obj = {"type": "Exception", "message": error_str,
                             "traceback": error_str}
            elif termination_reason == "wall_time":
                status = "timeout"
                error_str = f"conversation wall budget {timeout_s}s exceeded"
            elif eval_status == "timeout":
                status = "timeout"
                error_str = (eval_error or {}).get("message")
                error_obj = eval_error
            elif eval_status == "failed":
                status = "failed"
                error_str = (eval_error or {}).get("message")
                error_obj = eval_error
            else:
                status = "completed"

        finally:
            if sem is not None:
                sem.release()

    except (asyncio.CancelledError, KeyboardInterrupt) as e:
        status = "cancelled"
        error_str = type(e).__name__
        writer.emit_event("run_cancelled", reason=error_str)
        if not isinstance(e, KeyboardInterrupt):
            raise

    except Exception as e:
        status = "failed"
        error_str = str(e)
        error_obj = err_dict(e)
        writer.emit_event(
            "run_failed",
            error_type=type(e).__name__,
            message=error_str,
            category=classify_error(e),
        )
        logger.exception("run_pair_unit failed for %s", unit.slug)

    finally:
        if task_driver is not None:
            try:
                await task_driver.close()
            except Exception as e:                              # noqa: BLE001
                logger.debug("TaskDriver.close failed: %s", e)
        for role, env in envs.items():
            try:
                await env.close_async(mode=cleanup_mode)
            except Exception as e:                              # noqa: BLE001
                logger.debug("A2Env.close_async(%s) failed: %s", role, e)

    _exec_start = exec_started if exec_started is not None else started
    _exec_end = exec_ended if exec_ended is not None else time.monotonic()
    total_s = round(_exec_end - _exec_start, 2)

    writer.write_eval_result(
        eval_status=eval_status,
        score=score,
        eval_duration_s=eval_duration_s,
        error=eval_error,
        breakdown=eval_breakdown,
    )
    writer.write_run_json(_build_pair_run_meta(
        run_id=writer.run_id,
        unit=unit,
        resolved=resolved,
        executor_types=executor_types,
        status=status,
        score=score,
        termination_reason=termination_reason,
        end_reason_text=end_reason_text,
        error_obj=error_obj,
        error_str=error_str,
        total_s=total_s,
        n_turns=len(conversation),
    ))
    writer.emit_event(
        "run_completed",
        status=status,
        score=score,
        termination=termination_reason,
        turns=len(conversation),
        total_duration_s=total_s,
    )
    writer.close()

    return UnitResult(
        unit=unit,
        status=status,
        score=score,
        eval_status=eval_status,
        duration_s=total_s,
        run_dir=writer.run_dir,
        error=error_str,
    )


# ======================================================================
# Helpers
# ======================================================================


def _resolve_hub_urls(
    executors: dict[str, Any],
) -> tuple[dict[str, str], list[HostHub]]:
    """Per-role hub base URL + any host-side hubs we had to spin up.

    A sandbox role's hub runs in its container and the docker-published URL is
    in ``sandbox.metadata['hub_url']``. A local role (the dummy smoke agent)
    has no container, so we start the same hub module on a host loopback port.
    The returned ``HostHub`` list must be closed by the caller.
    """
    hub_urls: dict[str, str] = {}
    host_hubs: list[HostHub] = []
    for role, executor in executors.items():
        sandbox = getattr(executor, "sandbox", None)
        url = (getattr(sandbox, "metadata", None) or {}).get("hub_url") if sandbox else None
        if not url:
            hh = HostHub()
            host_hubs.append(hh)
            url = hh.url
        hub_urls[role] = url
    return hub_urls, host_hubs


def _compose_standing_context(
    *, brief: str, can_complete: bool, channel: str = "a2a",
) -> str:
    """One agent's workspace config: persona brief + messaging section.

    Installed via the harness's native standing-config file (CLAUDE.md /
    AGENTS.md), not a system prompt. The peer roster is deliberately NOT
    inlined — discovery happens at runtime through ``a2a_list_agents`` (fed by
    ``A2A_PEER_CARDS``). ``can_complete`` appends the ``a2a_task_complete``
    guidance for the assigned role only; ``channel`` picks the paragraph
    describing the envelope that channel delivers.
    """
    inbound = _INBOUND_BY_CHANNEL.get(channel)
    if inbound is None:
        raise ValueError(
            f"no messaging instructions for channel {channel!r}; add an "
            f"_INBOUND_BY_CHANNEL entry (have: {sorted(_INBOUND_BY_CHANNEL)})"
        )
    messaging = _MESSAGING_HEAD + inbound + _MESSAGING_TAIL
    parts = [brief.rstrip(), "", messaging.rstrip()]
    if can_complete:
        parts += ["", _COMPLETION_SECTION.rstrip()]
    return "\n".join(parts) + "\n"


def _build_role_trajectory(
    *,
    role: str,
    deployer_cls: Any,
    config: Any,
    agent_spec: Any,
    unit: PairRunUnit,
    conversation: list[dict[str, Any]],
    host_dir: Path,
    instruction: str,
) -> TrajectoryBuilder:
    """One ATIF trajectory per role from the recorded conversation.

    A harness exposing ``_replay_blocking_transcript`` splits the role's one
    long session into per-message turns (each ``a2a_send_message`` call is a
    boundary), injecting each hop's ``incoming`` as the ``user`` step that
    opens it. Harnesses without the hook (the dummy) fall back to bare
    ``user`` + ``agent`` steps per hop. Transcript parsing is telemetry only
    — a replay failure must not fail the run, but for a harness that declared
    the hook it is recorded as ``replay_degraded`` + metrics-partial, so
    "agent did little" stays distinguishable from "we failed to parse it".
    """
    builder = TrajectoryBuilder(
        agent_name=f"{getattr(config, 'name', agent_spec.class_)}:{role}",
        agent_version=None,
        model=getattr(config, "model", None),
        task_path=unit.task_path,
        variant_index=unit.variant_index,
        instruction=instruction,
    )
    # This role's hops, in order — each carries the incoming that prompted it.
    role_entries = [e for e in conversation if e["role"] == role]
    incomings = [str(e.get("incoming") or "") for e in role_entries]

    replay_blocking = getattr(deployer_cls, "_replay_blocking_transcript", None)
    transcript = host_dir / "transcript_000.jsonl"
    replayed = False
    degraded: str | None = None
    if replay_blocking is not None:
        if not transcript.exists():
            degraded = f"transcript missing at {transcript}"
        else:
            try:
                replay_blocking(transcript, builder, incomings)
                replayed = True
            except Exception as e:                              # noqa: BLE001
                degraded = f"replay raised {type(e).__name__}: {e}"
                logger.exception(
                    "blocking replay of %s failed for role %s", transcript, role,
                )

    if degraded:
        # A harness that declares the hook was expected to replay. Losing that
        # yields a structurally-valid but content-free trajectory, so mark it
        # instead of letting it pass as a real one.
        logger.warning(
            "role %s trajectory degraded to bare steps: %s", role, degraded,
        )
        builder.trajectory.extra["replay_degraded"] = degraded
        builder.mark_metrics_partial()

    if not replayed:
        # No real transcript (dummy harness, or replay failed): a clean
        # per-message view — the incoming, then the reply it produced.
        for entry in role_entries:
            builder.add_step(
                source="user", message=entry["incoming"],
                extra={"round": entry["round"], "turn_index": entry["turn_index"]},
            )
            builder.add_step(
                source="agent", message=entry["reply"],
                extra={"round": entry["round"], "status": entry["status"]},
            )

    # Reconcile trajectory totals from the CLI ``result`` event the replay
    # captured (authoritative usage/cost — the default per-step sum is lossy;
    # see the deployer's apply_authoritative_metrics). The blocking model runs
    # ONE invocation per role, so exactly one result is expected.
    apply_metrics = getattr(deployer_cls, "apply_authoritative_metrics", None)
    if replayed and apply_metrics is not None:
        try:
            apply_metrics(builder, expected_results=1)
        except Exception as e:                                  # noqa: BLE001
            # Cost/token totals are reporting data, not run correctness — a
            # reconciliation bug must not void an otherwise-good episode. Flag
            # the numbers as a lower bound so no report treats them as exact.
            logger.exception(
                "authoritative-metrics reconciliation failed for role %s", role,
            )
            builder.trajectory.extra["metrics_error"] = f"{type(e).__name__}: {e}"
            builder.mark_metrics_partial()
    return builder


def _write_conversation_json(
    *,
    run_dir: Path,
    first_speaker: str,
    channel: str,
    termination_reason: str | None,
    end_reason_text: str | None,
    conversation: list[dict[str, Any]],
) -> None:
    payload = {
        "first_speaker": first_speaker,
        "channel": channel,
        "protocol_version": A2A_PROTOCOL_VERSION,
        "termination": {
            "reason": termination_reason,
            "end_reason": end_reason_text,
            # Beside the reason, not instead of it: the metrics read the
            # reason, and the protocol has no state for ``max_rounds``.
            "task_state": task_state.state_for_reason(termination_reason),
        },
        "turns": conversation,
    }
    try:
        (run_dir / "conversation.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("write conversation.json failed: %s", e)


def _build_pair_run_meta(
    *,
    run_id: str,
    unit: PairRunUnit,
    resolved: dict[str, tuple[Any, Any]],
    executor_types: dict[str, str],
    status: str,
    score: float | None,
    termination_reason: str | None,
    end_reason_text: str | None,
    error_obj: dict[str, Any] | None,
    error_str: str | None,
    total_s: float,
    n_turns: int,
) -> dict[str, Any]:
    roles_meta = {}
    for role, agent_spec in unit.roles.items():
        _deployer_cls, config = resolved[role]
        roles_meta[role] = {
            "id": agent_spec.id,
            "class": agent_spec.class_,
            "name": getattr(config, "name", agent_spec.class_),
            "model": getattr(config, "model", None),
            "executor": executor_types[role],
            "config_repr": redact_config(dict(agent_spec.config)),
        }
    return {
        "schema_version": 2,
        "run_id": run_id,
        "mode": "a2a",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "roles": roles_meta,
        "task": {
            "slug": slug_task(unit.task_path),
            "path": f"tasks/{unit.task_path}",
            "variant_index": unit.variant_index,
            "variant_name": unit.variant_name,
        },
        "status": status,
        "score": score,
        "conversation": {
            "turns": n_turns,
            "termination_reason": termination_reason,
            "end_reason": end_reason_text,
        },
        "termination": {
            "reason": status if status != "completed" else "completed",
            "error": error_obj if error_obj else (
                {"type": "Exception", "message": error_str, "traceback": error_str}
                if error_str and status != "completed" else None
            ),
        },
        "timings": {"duration_s": round(total_s, 2)},
    }
