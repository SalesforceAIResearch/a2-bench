"""BaseExecutor — substrate adapter that places + drives a deployer.

The framework recognises two Executor types (in :mod:`a2_run.executors`):

* :class:`LocalExecutor`   — run the deployer in the framework's own
                             Python process.
* :class:`SandboxExecutor` — ship the a2_run source into the sandbox
                             container and run the deployer inside it.

What the executor IS to a deployer
----------------------------------

A passive data carrier. The deployer's ``__init__(executor)`` reads
``executor.work_dir / sandbox / config / env`` and operates with **Python
stdlib only** (``subprocess`` / ``pathlib`` / ``json`` / ``asyncio``).
The deployer never calls the abstract methods below — those are
the **lifecycle's** handle on the substrate, not the deployer's.

What the executor IS to the lifecycle
-------------------------------------

* :meth:`run_deployer`   — place + run the deployer end-to-end;
                           return an :class:`AgentRunResult`.
* :meth:`gather_dir`     — bulk-pull ``src`` (substrate-native path)
                           into ``dst`` (host directory). No-op when
                           ``src`` is already a host path.

Cross-OS deployer contract
--------------------------

The Sandbox executor's substrate may be linux OR windows (per
:attr:`SandboxHandle.os`). Deployer code is responsible for its own OS
dispatch using ``self.executor.sandbox.os``; the framework does not
abstract this.
"""
from __future__ import annotations

import abc
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TYPE_CHECKING

from .sandbox import SandboxHandle

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .agent_deployer import AgentRunResult, BaseAgentDeployer


@dataclass
class GatherReport:
    """Outcome of :meth:`BaseExecutor.gather_dir`.

    Always returned (never raised) so the lifecycle can log and proceed
    when transport hiccups. Empty/no-op pulls report ``files == 0``.
    """

    transport: str                # "local" | "cua"
    files: int = 0
    bytes: int = 0
    error: str | None = None


@dataclass
class BaseExecutor(abc.ABC):
    """Per-unit substrate adapter. Constructed once per run by the
    lifecycle; lives for the duration of one run unit.
    """

    # ──────── data fields (deployer reads these) ────────

    # Annotated as ``Any``: each agent owns a standalone config dataclass
    # (claude_code → ClaudeCodeConfig …) with no shared base, so there is
    # no single type to import here, and importing the concrete classes
    # would create an interface-package cycle.
    config: Any
    """Per-agent resolved config (claude_code → ClaudeCodeConfig …).
    Concrete dataclass; deployer reads via ``self.config`` alias."""

    work_dir: str
    """Substrate-native scratch dir owned by this run. POSIX or Windows-
    style depending on :attr:`sandbox.os`. The deployer's
    transcript / stderr / done.marker land here."""

    sandbox: SandboxHandle
    """The sandbox container this unit evaluates in. Deployer reads
    ``sandbox.endpoint / .os / .work_dir_base / ...``; the Executor
    itself uses the handle's I/O when the substrate IS the sandbox
    (SandboxExecutor)."""

    env: dict[str, str] = field(default_factory=dict)
    """Env vars the framework wants injected into the deployer's
    process (api keys, base URLs). The deployer folds these into its
    launched-agent shell."""

    type: ClassVar[str] = ""
    """Subclass-supplied discriminator. Matches yaml ``executor: <type>``
    values. Validated on concrete-subclass creation."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Concrete subclasses must set ``type``; intermediate abstract
        # bases (still carrying abstract methods) are skipped.
        if getattr(cls, "__abstractmethods__", None):
            return
        if not getattr(cls, "type", ""):
            raise TypeError(
                f"{cls.__name__}: concrete Executor subclass must set "
                "class attribute `type` to a non-empty string"
            )

    # ──────── agent dependency bootstrap ────────

    @staticmethod
    def install_agent_deps(deployer_module: str) -> None:
        """Install Python deps declared in the agent's ``pyproject.toml``.

        Called by the entry point (``_sandbox_entry``)
        **before** ``importlib.import_module(deployer_module)`` so that
        top-level imports in deployers (e.g. ``import yaml``) don't crash
        when the package isn't pre-installed in the environment.

        Locates the agent package from *deployer_module*
        (e.g. ``a2_run.agents.claude_code.deployer`` → ``a2_run/agents/claude_code/``),
        reads ``[project].dependencies`` from its ``pyproject.toml``, and
        ``pip install``s any that are listed into the current interpreter
        (``sys.executable`` — the python the Image declares). Every image's
        declared python ships with pip, so a single ``pip`` path is used (no
        uv); ``ensurepip`` self-heals the rare case where pip is absent.
        """
        pkg_path = deployer_module.rsplit(".", 1)[0].replace(".", "/")
        toml_path: Path | None = None
        for base in sys.path:
            candidate = Path(base) / pkg_path / "pyproject.toml"
            if candidate.is_file():
                toml_path = candidate
                break
        if toml_path is None:
            return

        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        deps = data.get("project", {}).get("dependencies", [])
        if not deps:
            return

        # Ensure pip is importable for this interpreter (uv-created venvs
        # may omit it); ensurepip is stdlib and a no-op when pip exists.
        if subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
        ).returncode != 0:
            _logger.info("install_agent_deps: pip missing, bootstrapping via ensurepip")
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade"],
                check=True, timeout=120,
            )

        _logger.info("install_agent_deps: %s (from %s)", deps, toml_path)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", *deps],
            check=True, timeout=120,
        )
        _logger.info("install_agent_deps: done")

    # ──────── methods (lifecycle uses these) ────────

    @abc.abstractmethod
    async def run_deployer(
        self,
        *,
        deployer_cls: type["BaseAgentDeployer"],
        prompt: str,
        timeout_s: float,
        turn_index: int,
        standing_context: str | None = None,
    ) -> "AgentRunResult":
        """Place + drive the deployer end-to-end on this substrate.

        Runs ``launch_turn(prompt, turn_index=N)`` — one SESSION for the
        role, against a work_dir that persists across calls (``turn_index``
        counts the role's sessions; >0 only after a resume).
        ``standing_context`` is the agent's identity + environment config,
        forwarded on every call.

        Concrete implementations:

        * **LocalExecutor**: in-process construct + ``await install();
          await launch_turn()``.
        * **SandboxExecutor**: ship the ``a2_run/`` tree into the
          sandbox, launch an entry that imports the deployer +
          constructs a local-flavored Executor in-sandbox and runs it.

        On crash inside the substrate, the implementation MUST surface
        the traceback in :attr:`AgentRunResult.error` (and pull any
        crash logs into the host's gather target if possible)."""

    @abc.abstractmethod
    async def gather_dir(
        self,
        *,
        src: str,
        dst: Path,
    ) -> GatherReport:
        """Recursively copy ``src`` (substrate-native path) into ``dst``
        (host directory; created if missing).

        No-op (``files=0``) when ``src`` is already a host path
        (LocalExecutor). Best-effort — any per-file failure is logged
        but does not raise; the ``error`` field on the return value
        reports the first failure."""
