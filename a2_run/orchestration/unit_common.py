"""Shared per-unit helpers for the pair (a2a) lifecycle.

Executor construction, env passthrough (API keys / ADC / CA bundle),
task-data staging, score extraction, and process-wide signal handling.
"""
from __future__ import annotations

import hashlib
import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from ..base_interface import BaseExecutor, Provider
from ..environments.env import A2Env
from ..executors import LocalExecutor, SandboxExecutor
from .experiment_spec import ArtifactsSpec

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 7200
# Wall-clock ceiling for the evaluation phase. Without it, a wedged sandbox
# RPC inside a task's evaluate() hangs the whole unit until the episode
# budget. Hitting this bound is treated as a unit ``timeout`` (not
# ``failed``), so resume won't re-run it.
EVAL_TIMEOUT_S = 7200


# ======================================================================
# Shutdown signal plumbing
# ======================================================================

# Process-wide shutdown event: set by SIGINT/SIGTERM handlers, read by
# per-unit cleanup so signal arrival flips every in-flight unit to
# "cancelled" without losing the events.jsonl tail.
_shutdown_event: asyncio.Event | None = None


def get_shutdown_event() -> asyncio.Event:
    """Lazy singleton — needs a running loop to construct."""
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def install_signal_handlers() -> None:
    """Wire SIGINT / SIGTERM → shutdown event.

    Idempotent. Skipped silently when not running on the main thread (the
    asyncio loop policy refuses ``add_signal_handler`` from worker threads).
    """
    import threading

    if threading.current_thread() is not threading.main_thread():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    event = get_shutdown_event()

    def _on_signal(sig: signal.Signals) -> None:
        logger.warning("Signal %s received — flagging shutdown", sig.name)
        event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):
            # Windows / nested-loop edge cases: best-effort.
            pass


# ======================================================================
# Executor construction + env passthrough
# ======================================================================


def build_executor(
    *,
    executor_type: str,
    env: A2Env,
    config: Any,
    agent_name: str,
    run_id: str,
    host_artifacts_dir: Path,
) -> BaseExecutor:
    """Dispatch yaml ``executor: <type>`` to the concrete substrate adapter.

    ``host_artifacts_dir`` is owned by the lifecycle (not the executor); for
    Local it's also the deployer's work_dir (no gather step). For Sandbox the
    deployer's work_dir is a remote-side path and gather copies into
    ``host_artifacts_dir`` after launch.
    """
    env_passthrough = _collect_env_passthrough()
    if getattr(config, "provider", None) == "vertex":
        _materialise_gcp_adc(env_passthrough)
    # Corporate/proxy setups terminate TLS with a privately-signed cert; ship
    # the host CA bundle CONTENT into the substrate so the in-container CLI
    # trusts it (the host path itself would dangle inside the container).
    _materialise_ca_bundle(env_passthrough)
    if executor_type == "sandbox":
        sb = env.sandbox
        sep = "/" if sb.is_linux else "\\"
        # Remote path must stay OPAQUE: the run_id encodes roles, harness
        # classes, model ids and timestamps, all agent-visible via cwd. Use a
        # short digest instead (still deterministic per run).
        session_tag = "s-" + hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:8]
        remote_work_dir = f"{sb.work_dir_base.rstrip(sep)}{sep}{agent_name}{sep}{session_tag}"
        return SandboxExecutor(
            config=config,
            work_dir=remote_work_dir,
            sandbox=env.sandbox,
            env=env_passthrough,
        )
    if executor_type == "local":
        return LocalExecutor(
            config=config,
            work_dir=str(host_artifacts_dir),
            sandbox=env.sandbox,
            env=env_passthrough,
        )
    raise NotImplementedError(f"executor type {executor_type!r} not supported")


def _collect_env_passthrough() -> dict[str, str]:
    """Env vars to propagate into the substrate so deployers see API keys.

    Keep this list small — only well-known agent credentials deployers need.
    Host-only orchestration values must not be added here because this
    mapping is sent into the sandbox.
    """
    import os

    keys = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        # OpenRouter (claude_code provider=openrouter): the deployer maps this
        # to ANTHROPIC_AUTH_TOKEN in-container. Passed through the env (not
        # config) so it stays out of the gathered, keyless _spec.json.
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        # AWS Bedrock credentials (claude_code provider=bedrock).
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        # Bedrock via an LLM gateway (token auth, no sigv4).
        "ANTHROPIC_BEDROCK_BASE_URL",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        # GCP Vertex credentials (claude_code provider=vertex).
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        # Cursor credentials (cursor harness): a crsr_... key, consumed by
        # cursor-agent directly from the env.
        "CURSOR_API_KEY",
    )
    return {k: os.environ[k] for k in keys if k in os.environ}


def _materialise_ca_bundle(env: dict[str, str]) -> None:
    """Ship the host CA bundle CONTENT into the substrate as content.

    Corporate/proxy TLS termination (e.g. claude_code provider=bedrock routed
    through an internal gateway) presents a privately-signed cert. Node trusts
    it on the host only because ``NODE_EXTRA_CA_CERTS`` points at a host CA
    file — and that host PATH dangles inside the container. So we read the
    file's content and ship it as ``A2_CA_BUNDLE_PEM`` through the read-once
    secrets side-channel; the claude_code deployer writes it back to an
    in-container path and RE-POINTS ``NODE_EXTRA_CA_CERTS`` there.

    Only the ADDITIVE node var is materialised. The replacement-style
    SSL_CERT_FILE / REQUESTS_CA_BUNDLE are intentionally NOT set: pointing
    them at just this bundle would drop the public roots and break npm/pip
    in the container.
    """
    import os

    ca_path = os.environ.get("NODE_EXTRA_CA_CERTS", "").strip()
    if not ca_path:
        return
    p = Path(ca_path).expanduser()
    if not p.is_file():
        logger.warning(
            "NODE_EXTRA_CA_CERTS=%s is not a readable file — skipping CA "
            "passthrough; in-container TLS to a private gateway may fail",
            ca_path,
        )
        return
    try:
        content = p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("failed to read CA bundle %s: %s", p, e)
        return
    if content.strip():
        env["A2_CA_BUNDLE_PEM"] = content
        logger.info(
            "env_passthrough: materialised CA bundle from %s (%d B)",
            p, len(content),
        )


def _materialise_gcp_adc(env: dict[str, str]) -> None:
    """Ship gcloud application-default credentials into the sandbox as content.

    ADC is a FILE on the host (written by ``gcloud auth application-default
    login``), and host paths do not exist inside the sandbox container. So the
    file's content travels as ``GCP_ADC_JSON`` through the read-once secrets
    side-channel; the claude_code deployer (provider=vertex) writes it back to
    the well-known ADC path in-sandbox. GOOGLE_APPLICATION_CREDENTIALS, if set
    host-side, selects which file to read — the variable itself is never
    forwarded (its path would dangle in the container).
    """
    import os

    adc_path = Path(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        or os.path.join(
            os.path.expanduser("~"),
            ".config", "gcloud", "application_default_credentials.json",
        )
    ).expanduser()
    if not adc_path.is_file():
        logger.warning(
            "provider=vertex but no ADC file at %s — run "
            "`gcloud auth application-default login` on the host",
            adc_path,
        )
        return
    try:
        content = adc_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("failed to read ADC file %s: %s", adc_path, e)
        return
    if content.strip():
        env["GCP_ADC_JSON"] = content
        logger.info("env_passthrough: materialised GCP_ADC_JSON from %s (%d B)",
                    adc_path, len(content))


# ======================================================================
# Task data + score helpers
# ======================================================================


def _task_data_source(artifacts: ArtifactsSpec | None) -> str:
    return (
        artifacts.task_data_source if artifacts is not None
        else ArtifactsSpec().task_data_source
    )


async def stage_task_data(
    *,
    env: A2Env,
    provider: Provider,
    artifacts: ArtifactsSpec | None,
    task_meta: dict[str, Any],
    role: str | None = None,
) -> None:
    """Stage input/software onto the sandbox (Phase 1).

    Dispatches on ``task_data_source``: ``"baked_in_sandbox"`` (image already
    has data; sanity-check only) or ``"local:<dir>"`` (upload from host).
    Returns silently when the task declares no data-staging requirements.

    ``role`` (a2a) selects a per-role host data dir when the backend supports
    it — the two sandboxes in a pair can stage different data. Once the backend
    returns, an existing ``input/`` tree is normalized into a writable working
    copy owned by the sandbox's normal user.
    """
    from ..environments import task_data as task_data_pkg

    task_data = task_meta.get("task_data")
    if task_data is None or not task_data.requires_task_data:
        return

    source = _task_data_source(artifacts)
    backend = task_data_pkg.select(source)
    await backend.stage_input(env.sandbox, task_data, source=source, role=role)
    input_dir = task_data_pkg.join(
        env.sandbox,
        task_data_pkg.task_subdir(env.sandbox, task_data),
        "input",
    )
    if await env.sandbox.exists(input_dir):
        await env.sandbox.make_user_writable(input_dir)


async def stage_task_reference(
    *,
    env: A2Env,
    artifacts: ArtifactsSpec | None,
    task_meta: dict[str, Any],
    role: str | None = None,
) -> None:
    """Stage reference/ onto the sandbox (Phase 3, just before evaluate).

    Mirrors :func:`stage_task_data` but for the answer key: the reference is
    copied in only after the agent has finished, so it is never visible during
    the run. Returns silently when the task declares no data-staging
    requirements (a task with no reference simply has none to copy). ``role``
    selects the per-role host dir (a2a) — e.g. only the asker's sandbox gets
    the expected answer.
    """
    from ..environments import task_data as task_data_pkg

    task_data = task_meta.get("task_data")
    if task_data is None or not task_data.requires_task_data:
        return

    source = _task_data_source(artifacts)
    backend = task_data_pkg.select(source)
    await backend.stage_reference(env.sandbox, task_data, source=source, role=role)


def extract_score(eval_output: Any) -> float | None:
    """Return a numeric score while preserving valid falsy values like 0.0."""
    if eval_output is None:
        return None
    if isinstance(eval_output, dict):
        for key in ("score", "final_score"):
            if key in eval_output and eval_output[key] is not None:
                try:
                    return float(eval_output[key])
                except (ValueError, TypeError):
                    pass
        return None
    if isinstance(eval_output, list) and eval_output:
        try:
            return float(eval_output[0])
        except (ValueError, TypeError):
            return None
    if isinstance(eval_output, (int, float)):
        return float(eval_output)
    return None


def extract_breakdown(eval_output: Any) -> dict[str, Any] | None:
    """The task's own account of how its score was reached, if it reported one.

    A task opts in by returning a dict with a ``breakdown`` key holding
    ``{"total": float, "parts": [{"role", "score", "weight", "criteria", …}]}``
    — the shape :mod:`a2_run.transcript.breakdown` reads. Tasks that return a
    bare ``[score]`` (the older contract) yield ``None`` here and lose nothing
    but the drill-down.
    """
    if not isinstance(eval_output, dict):
        return None
    bd = eval_output.get("breakdown")
    if not isinstance(bd, dict) or not isinstance(bd.get("parts"), list):
        return None
    return bd
