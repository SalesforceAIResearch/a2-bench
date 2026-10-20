"""YAML loader for ExperimentSpec.

The experiment yaml is deliberately thin: it wires together externalized
config files and carries only run-level knobs. Agent and environment
configuration live in their own files under ``configs/``; the experiment
references them by path.

Shape:

* ``agents: {<role>: <path>, ...}`` — role name → path to an agent preset
  yaml under ``configs/agents/``. One or more roles: two-plus is the usual
  a2a pair/market; a SINGLE role is a solo baseline (one container, no peer
  to message — the intrinsic-behavior control for a multi-agent scenario).
  Role names must match the task's declared roles. The optional ``a2a:``
  block (``max_rounds``, ``channel``) tunes the conversation.
* ``environment: <path>`` — a single environment yaml under
  ``configs/environments/`` (``provider:`` + provider-specific knobs).
* ``tasks: <path>`` — ``.txt`` → one task path per line (all variants);
  ``.yaml`` → list of ``{path, variants}``. An inline list also works.
  ``variants`` entries are variant NAMES (str) or indices (int); omit the
  key to run every declared variant.
* Other top-level keys: ``output``, ``concurrency``, ``cleanup_mode``,
  ``wall_time_s``, ``secret_file``.

Other behavior:

* ``${env:VAR}`` substitution at parse time (KeyError if VAR unset) — applied
  to the experiment yaml AND every referenced config file.
* All relative paths resolve against the main yaml's directory.
* Unknown / missing keys raise loudly.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .experiment_spec import (
    A2ASpec,
    AgentSpec,
    ArtifactsSpec,
    EnvironmentSpec,
    ExperimentSpec,
    OutputSpec,
    ProviderSpec,
    TaskSpec,
)

logger = logging.getLogger(__name__)


# ${env:VARNAME} → os.environ[VARNAME]
_ENV_RE = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")

# Top-level keys consumed by the loader (anything else → TypeError).
_TOP_LEVEL_KEYS = frozenset({
    "name", "secret_file", "agents", "environment", "tasks",
    # run-level fields:
    "output",
    "concurrency",
    "cleanup_mode",
    "wall_time_s",
    # a2a (pair) relay-loop knobs.
    "a2a",
})


# =============================================================================
# Public API
# =============================================================================

def load_experiment(
    path: str | Path, overrides: list[str] | None = None,
) -> ExperimentSpec:
    """Read a yaml file, substitute env vars, build :class:`ExperimentSpec`.

    Two-pass parse: first pass extracts ``secret_file:`` (if present) without
    ``${env:VAR}`` substitution; that file is loaded into ``os.environ``
    (shell env wins on conflict); second pass parses for real, with
    substitution. If ``secret_file:`` is unset, falls back to auto-loading
    ``<main yaml's directory>/secret/.env`` (then ``.../.env``) when they
    exist. Profile yamls referenced from the main file inherit the same
    ``os.environ``, so their ``${env:...}`` refs resolve the same way.

    ``overrides`` are CLI ``--set KEY=VALUE`` items applied to the parsed
    yaml before validation (see :func:`_apply_overrides`), so one base yaml
    plus overrides replaces a file per role/task/knob combination.
    """
    main_path = Path(path).resolve()
    base_dir = main_path.parent

    text = main_path.read_text()
    # First pass: parse raw text (no substitution) just to find secret_file.
    try:
        pre = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"experiment yaml {main_path} is not valid yaml: {exc}") from exc
    if not isinstance(pre, dict):
        raise TypeError(f"experiment yaml root must be a mapping, got {type(pre).__name__}")
    secret_file = pre.get("secret_file")
    if secret_file is not None:
        _load_dotenv(_resolve_path(secret_file, base_dir), override=True)
    else:
        # Convenience auto-detect. `secret/.env` is the canonical location
        # (alongside the checked-in `secret/.env.example` template); legacy
        # `.env` at the yaml's directory is still honored as a fallback.
        _load_dotenv(base_dir / "secret" / ".env")
        _load_dotenv(base_dir / ".env")

    # Second pass: substitute + parse for real.
    raw = yaml.safe_load(_substitute_env(text)) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"experiment yaml root must be a mapping, got {type(raw).__name__}")
    if overrides:
        _apply_overrides(raw, overrides)
    return _build_experiment(raw, base_dir=base_dir)


def _apply_overrides(raw: dict[str, Any], overrides: list[str]) -> None:
    """Apply ``--set KEY=VALUE`` items onto the parsed experiment mapping.

    ``KEY`` is a dotted path (``a2a.max_rounds``, ``agents.shopper``,
    ``output.root``); intermediate mappings are created as needed. ``VALUE``
    is parsed as a yaml scalar/inline value, so ``10``, ``true``, and
    ``{shopper: a.yaml, merchant: b.yaml}`` all work; anything unparseable
    stays a plain string. Relative paths resolve against the yaml's
    directory, exactly as if written in the file. Unknown keys still fail
    validation in ``_build_experiment`` — overrides bypass nothing.
    """
    for item in overrides:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"--set expects KEY=VALUE, got {item!r}")
        try:
            parsed = yaml.safe_load(_substitute_env(value))
        except yaml.YAMLError:
            parsed = value
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = parsed


# =============================================================================
# Env + yaml helpers
# =============================================================================

def _load_dotenv(path: Path, *, override: bool = False) -> None:
    """Hand-rolled dotenv parser. Format: ``KEY=value`` per line; ``#``
    starts a line comment; surrounding single/double quotes are stripped;
    blank lines and unparseable lines are skipped (logged at DEBUG).

    When ``override=False`` (default / auto-detected files), shell env
    wins — never overwrites an already-set os.environ key.  When
    ``override=True`` (explicit ``secret_file:`` in the experiment yaml),
    the file is the authoritative source and overwrites shell values.

    Missing file is silently ignored — most setups have one, some don't.
    """
    if not path.is_file():
        return
    for line_no, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            logger.debug("dotenv %s:%d skipped (no '='): %r", path, line_no, raw)
            continue
        key = key.strip()
        value = value.strip()
        # Strip an inline comment after an unquoted value: `FOO=bar # note`.
        if value and value[0] not in ("'", '"'):
            value = value.split(" #", 1)[0].rstrip()
        # Strip matching quotes around the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key.isidentifier():
            logger.debug("dotenv %s:%d skipped (bad key): %r", path, line_no, raw)
            continue
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def _read_yaml(path: Path) -> Any:
    text = path.read_text()
    text = _substitute_env(text)
    return yaml.safe_load(text) or {}


def _substitute_env(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            raise KeyError(
                f"yaml references ${{env:{var}}} but {var} is not set"
            )
        return val
    return _ENV_RE.sub(repl, text)


def _resolve_path(p: str | Path, base_dir: Path) -> Path:
    """Relative paths resolve under ``base_dir``; absolute / ``~`` honored."""
    pp = Path(p).expanduser()
    if pp.is_absolute():
        return pp
    return (base_dir / pp).resolve()


# =============================================================================
# Top-level builder
# =============================================================================

def _build_experiment(raw: dict[str, Any], *, base_dir: Path) -> ExperimentSpec:
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise TypeError(f"unknown top-level keys: {sorted(unknown)}")
    _require(raw, "name")

    # Run-level knobs live directly at the experiment top level. `output.root`
    # is the host-side run-log dir (a workflow choice); artifact SOURCING and
    # output MIRRORING (task_data_source / output_path) are substrate-coupled
    # and live in the environment yaml instead (see _build_environment_from_path).
    output = _build_output(raw.get("output") or {})
    concurrency = _build_concurrency(raw)
    cleanup_mode = _build_cleanup_mode(raw)
    wall_time_s = raw.get("wall_time_s")
    if wall_time_s is not None:
        wall_time_s = int(wall_time_s)
        if wall_time_s <= 0:
            raise ValueError(f"wall_time_s must be a positive integer, got {wall_time_s}")

    if "agents" not in raw:
        raise KeyError(
            "missing required field: `agents` (role → agent preset path mapping)"
        )
    if not isinstance(raw["agents"], dict):
        raise TypeError(
            "only a2a experiments are supported: `agents:` must be a "
            "role → agent-preset-path MAPPING (e.g. {asker: configs/agents/"
            f"claude_code.yaml, ...}}), got {type(raw['agents']).__name__}"
        )
    roles = _build_roles(raw["agents"], base_dir=base_dir)
    a2a = _build_a2a(raw.get("a2a") or {})

    if "environment" not in raw:
        raise KeyError("missing required field: `environment` (path to an environment yaml)")
    environment, artifacts = _build_environment_from_path(raw["environment"], base_dir=base_dir)

    if "tasks" not in raw:
        raise KeyError("missing required field: `tasks`")
    tasks = _build_tasks(raw["tasks"], base_dir=base_dir)

    if not tasks:
        raise ValueError("experiment must declare at least one task")

    return ExperimentSpec(
        name=str(raw["name"]),
        output=output,
        environment=environment,
        tasks=tasks,
        artifacts=artifacts,
        concurrency=concurrency,
        cleanup_mode=cleanup_mode,
        wall_time_s=wall_time_s,
        roles=roles,
        a2a=a2a,
    )


# =============================================================================
# Section builders
# =============================================================================

def _build_output(raw: dict[str, Any]) -> OutputSpec:
    return OutputSpec(root=str(raw.get("root", ".logs/a2")))


def _build_concurrency(eff: dict[str, Any]) -> int:
    n = int(eff.get("concurrency", 1))
    if n < 1:
        raise ValueError(f"concurrency must be >= 1, got {n}")
    return n


_VALID_CLEANUP_MODES = frozenset({"delete", "stop", "keep"})


def _build_cleanup_mode(eff: dict[str, Any]) -> str:
    raw = str(eff.get("cleanup_mode") or "delete")
    if raw not in _VALID_CLEANUP_MODES:
        raise ValueError(
            f"cleanup_mode must be one of {sorted(_VALID_CLEANUP_MODES)}, got {raw!r}"
        )
    return raw


_VALID_OUTPUT_PATH_LITERALS = frozenset({"local"})
_VALID_TASK_DATA_LITERALS = frozenset({"baked_in_sandbox"})


def _build_artifacts(raw: dict[str, Any]) -> ArtifactsSpec:
    defaults = ArtifactsSpec()

    output_path = raw.get("output_path")
    if output_path is not None:
        op = str(output_path).strip()
        if op and op not in _VALID_OUTPUT_PATH_LITERALS:
            raise ValueError(
                f"artifacts_path.output_path must be null or 'local'; "
                f"got {output_path!r}"
            )
        output_path = op or None

    task_data_source = raw.get("task_data_source") or defaults.task_data_source
    tdp = str(task_data_source).strip()
    if (tdp not in _VALID_TASK_DATA_LITERALS
            and not tdp.startswith("local:")):
        raise ValueError(
            f"artifacts_path.task_data_source must be 'baked_in_sandbox' "
            f"or 'local:<dir>'; got {task_data_source!r}"
        )

    return ArtifactsSpec(
        task_data_source=tdp,
        output_path=output_path,
    )


# ---- agent ---------------------------------------------------------------

# Shape of an agent config yaml (configs/agents/<preset>.yaml):
#   harness | class : str          — registry shortcut OR fqdn
#   model            : str          — sugar → config.model
#   id               : str | None   — agent-level dir name; defaults to harness
#   executor         : str | None   — vm | local | docker (deployer default if None)
#   config           : dict         — passed verbatim to the deployer Config
_AGENT_TOP_KEYS = frozenset({"harness", "class", "model", "id", "executor", "config"})


def _build_agent_from_path(entry: Any, *, base_dir: Path) -> AgentSpec:
    """Load one agent preset — a path string, or a mapping that varies one.

    Mapping form: ``{preset: <path>, <overrides...>}``. The preset yaml loads
    as usual, then the remaining keys overlay it: top-level agent keys
    (``model``, ``id``, ``executor``, ``harness``/``class``) replace the
    preset's, ``config:`` deep-merges, and any other key is treated as a
    ``config`` field — so a model or provider variation needs no extra
    preset file::

        agents:
          asker: {preset: configs/agents/claude_code_vertex.yaml,
                  model: claude-opus-5}
    """
    overlay: dict[str, Any] = {}
    if isinstance(entry, dict):
        overlay = dict(entry)
        path = overlay.pop("preset", None)
        if not isinstance(path, str):
            raise TypeError(
                "agent mapping form requires `preset: <path>` plus override "
                f"keys, got {entry!r}"
            )
    elif isinstance(entry, str):
        path = entry
    else:
        raise TypeError(
            f"each agent entry must be a preset path or a "
            f"{{preset: <path>, ...}} mapping, got {type(entry).__name__}: {entry!r}"
        )
    resolved = _resolve_path(path, base_dir)
    raw = _read_yaml(resolved)
    if not isinstance(raw, dict):
        raise TypeError(f"agent config {path!r} must be a mapping")

    for key, val in overlay.items():
        if key == "config":
            if not isinstance(val, dict):
                raise TypeError(
                    f"agent override for {path!r}: config must be a mapping"
                )
            merged = dict(raw.get("config") or {})
            merged.update(val)
            raw["config"] = merged
        elif key in _AGENT_TOP_KEYS:
            raw[key] = val
        else:
            # Bare config field (effort_level, provider, …); validated later
            # by build_config against the deployer's Config dataclass.
            cfg = dict(raw.get("config") or {})
            cfg[key] = val
            raw["config"] = cfg

    unknown = set(raw) - _AGENT_TOP_KEYS
    if unknown:
        raise TypeError(f"agent config {path!r} has unknown keys: {sorted(unknown)}")

    if "harness" in raw and "class" in raw:
        raise ValueError(
            f"agent config {path!r}: set either `harness:` (shortcut) or "
            f"`class:` (fqdn), not both"
        )
    cls = raw.get("harness") or raw.get("class")
    if not cls:
        raise KeyError(f"agent config {path!r} missing required field: `harness` (or `class`)")
    cls = str(cls)

    config: dict[str, Any] = dict(raw.get("config") or {})
    # `model:` at the agent level is sugar for `config.model:`.
    if (m := raw.get("model")) is not None:
        config["model"] = m

    # Default the agent id to the harness name so the agent-level output dir is
    # named after the harness (`harness:`/`class:`), not the preset filename.
    # An explicit `id:` still wins — use it to disambiguate two presets of the
    # same harness in one `agents:` matrix.
    agent_id = raw.get("id") or cls

    executor = raw.get("executor")
    if executor is not None and not isinstance(executor, str):
        raise TypeError(f"agent config {path!r}: executor must be a string, got {type(executor).__name__}")

    return AgentSpec(id=str(agent_id), class_=cls, config=config, executor=executor)


# ---- a2a (pair) mode -------------------------------------------------------

_A2A_KEYS = frozenset({"max_messages", "channel"})


def _build_roles(raw: dict[str, Any], *, base_dir: Path) -> dict[str, AgentSpec]:
    """Mapping-form ``agents:`` — role name → preset path or
    ``{preset: <path>, ...overrides}`` (see :func:`_build_agent_from_path`).

    One or more roles: two-plus for a paired/market conversation, or a single
    role for a solo baseline (one container, no peer). Role names must match
    the task's declared roles; that cross-check happens at run time when the
    task is loaded.
    """
    if not raw:
        raise ValueError(
            "a2a mode requires at least 1 role under `agents:`, got none"
        )
    roles: dict[str, AgentSpec] = {}
    for role, path in raw.items():
        role_name = str(role)
        if not role_name.isidentifier():
            raise ValueError(
                f"role name {role_name!r} must be a simple identifier "
                f"(letters/digits/underscore)"
            )
        roles[role_name] = _build_agent_from_path(path, base_dir=base_dir)
    return roles


def _build_a2a(raw: dict[str, Any]) -> A2ASpec:
    if not isinstance(raw, dict):
        raise TypeError(f"a2a must be a mapping, got {type(raw).__name__}")
    if "max_rounds" in raw:
        raise TypeError(
            "a2a.max_rounds was renamed to a2a.max_messages (one message = "
            "one hop; a full exchange under the old knob was 2 messages, so "
            "carry the old value over as max_messages = 2 * max_rounds)"
        )
    unknown = set(raw) - _A2A_KEYS
    if unknown:
        raise TypeError(f"unknown a2a keys: {sorted(unknown)}")
    defaults = A2ASpec()
    max_messages = int(raw.get("max_messages", defaults.max_messages))
    if max_messages < 1:
        raise ValueError(f"a2a.max_messages must be >= 1, got {max_messages}")
    channel = str(raw.get("channel", defaults.channel))
    from ..protocols import CHANNEL_REGISTRY
    if channel not in CHANNEL_REGISTRY:
        raise ValueError(
            f"a2a.channel must be one of {sorted(CHANNEL_REGISTRY)}, got {channel!r}"
        )
    return A2ASpec(max_messages=max_messages, channel=channel)


# ---- environment ---------------------------------------------------------

# Keys at the env-yaml top level that are NOT provider config.
_ENVIRONMENT_TOP_RESERVED = frozenset({
    "provider", "providers", "snapshots", "task_data_source", "output_path",
})


def _build_environment_from_path(
    path: Any, *, base_dir: Path,
) -> tuple[EnvironmentSpec, ArtifactsSpec]:
    """Load the environment from a ``configs/environments/<env>.yaml``.

    Two shapes are accepted:

    * **Per-snapshot** (``snapshots:`` present) — each task-card snapshot maps
      to ``{provider, image, <provider>: {knobs}}``. Each block is self-contained
      (there is no shared top-level ``providers:`` section — gcloud connection
      fields are repeated per snapshot and reconciled). The loader reshapes this
      into one :class:`ProviderSpec` per provider kind plus a snapshot→kind
      routing table. ``gcs_sa_key`` (top level, alongside ``task_data_source``)
      is injected into providers that need in-sandbox GCS authentication.
    * **Single provider** (``provider:`` at top level, no ``snapshots:``) — the
      dev/``static`` shape: one provider serves every snapshot (it is a fixed
      attached box, so there is nothing to map).

    Returns the resolved :class:`EnvironmentSpec` plus the artifact-path config.
    """
    if not isinstance(path, str):
        raise TypeError(
            f"environment must be a single path string to an environment yaml, "
            f"got {type(path).__name__}: {path!r}"
        )
    raw = _read_yaml(_resolve_path(path, base_dir))
    if not isinstance(raw, dict):
        raise TypeError(f"environment config {path!r} must be a mapping")

    artifacts = _build_artifacts({
        "task_data_source": raw.get("task_data_source"),
        "output_path": raw.get("output_path"),
    })

    if "snapshots" in raw:
        env = _build_per_snapshot_env(raw, path)
    elif "provider" in raw:
        env = _build_single_provider_env(raw, path)
    else:
        raise KeyError(
            f"environment config {path!r} must declare either `snapshots:` "
            f"(per-snapshot provider mapping) or `provider:` (single provider)"
        )
    return env, artifacts


def _build_single_provider_env(raw: dict[str, Any], path: str) -> EnvironmentSpec:
    """Single-provider env: one provider, all snapshots."""
    provider = str(raw["provider"])
    cfg = {k: v for k, v in raw.items() if k not in _ENVIRONMENT_TOP_RESERVED}
    return EnvironmentSpec(
        provider_specs={provider: ProviderSpec(kind=provider, config=cfg)},
        snapshot_kind={},
        default_kind=provider,
    )


def _build_per_snapshot_env(raw: dict[str, Any], path: str) -> EnvironmentSpec:
    """Per-snapshot env: reshape snapshot→{provider,image,knobs} into one
    ProviderSpec per kind + a snapshot→kind routing table.

    Docker carries only the image NAME + container sizing; the container ref
    and paths are read from the Image entry by the provider.
    """
    snapshots = raw["snapshots"]
    if not isinstance(snapshots, dict) or not snapshots:
        raise TypeError(f"environment {path!r}: `snapshots:` must be a non-empty mapping")

    snapshot_kind: dict[str, str] = {}
    docker_cfg: dict[str, Any] | None = None

    for tag, entry in snapshots.items():
        if not isinstance(entry, dict):
            raise TypeError(f"environment {path!r}: snapshot {tag!r} must be a mapping")
        kind = entry.get("provider")
        image = entry.get("image")
        if not kind:
            raise KeyError(f"environment {path!r}: snapshot {tag!r} missing `provider`")
        if not image:
            raise KeyError(f"environment {path!r}: snapshot {tag!r} missing `image`")
        knobs = entry.get(kind) or {}       # provider-specific block (e.g. docker:)
        snapshot_kind[str(tag)] = str(kind)

        if kind == "docker":
            # docker carries just the image NAME + sizing knobs; the provider
            # resolves the container ref from the Image entry. Multiple
            # docker snapshots must agree.
            this = {"image": str(image), **knobs}
            if docker_cfg is not None and docker_cfg != this:
                raise ValueError(
                    f"environment {path!r}: multiple docker snapshots with "
                    f"differing config is not supported ({docker_cfg} vs {this})"
                )
            docker_cfg = this
        else:
            raise NotImplementedError(
                f"environment {path!r}: snapshot {tag!r} provider {kind!r} not supported"
            )

    provider_specs: dict[str, ProviderSpec] = {}
    if docker_cfg is not None:
        provider_specs["docker"] = ProviderSpec(kind="docker", config=dict(docker_cfg))

    return EnvironmentSpec(
        provider_specs=provider_specs,
        snapshot_kind=snapshot_kind,
        default_kind=None,
    )


# ---- tasks ---------------------------------------------------------------

def _build_tasks(raw: Any, *, base_dir: Path) -> list[TaskSpec]:
    """Three accepted shapes:

    * ``str`` ending in ``.txt``  → one `<path>` per line, all variants.
    * ``str`` ending in ``.yaml`` → list of ``{path, variants}`` entries.
    * ``list``                    → legacy inline form, each entry a
                                    ``{path, variants}`` dict.
    """
    if isinstance(raw, str):
        return _load_tasks_file(_resolve_path(raw, base_dir))
    if isinstance(raw, list):
        return [_build_task_entry(t) for t in raw]
    raise TypeError(f"tasks must be a string path or a list, got {type(raw).__name__}")


def _load_tasks_file(path: Path) -> list[TaskSpec]:
    if not path.exists():
        raise FileNotFoundError(f"tasks file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        out: list[TaskSpec] = []
        for raw_line in path.read_text().splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            # A .txt line names only a path, so it can't select variants —
            # run all declared variants (variants=None), same as an inline
            # entry that omits the key.
            out.append(TaskSpec(path=line, variants=None))
        return out
    if suffix in (".yaml", ".yml"):
        data = _read_yaml(path)
        if not isinstance(data, list):
            raise TypeError(f"{path}: top-level must be a list of task entries")
        return [_build_task_entry(t) for t in data]
    raise ValueError(f"unsupported tasks-file suffix: {path.suffix!r} (use .txt or .yaml)")


def _build_task_entry(raw: dict[str, Any]) -> TaskSpec:
    if not isinstance(raw, dict):
        raise TypeError(f"task entry must be a mapping, got {type(raw).__name__}")
    _require(raw, "path")
    # Omitting ``variants:`` (or writing it as null) means "run every declared
    # variant" (variants=None); the runner resolves that against the task's
    # actual variant list.
    variants = raw.get("variants")
    if variants is None:
        return TaskSpec(path=str(raw["path"]), variants=None)
    # Each entry is a variant NAME (str) or a positional index (int). Reject
    # bools explicitly — they are an int subclass and never a valid variant.
    if not isinstance(variants, list) or not all(
        isinstance(v, (int, str)) and not isinstance(v, bool) for v in variants
    ):
        raise TypeError(
            f"task.variants must be a list of variant names (str) or indices "
            f"(int), got {variants!r}"
        )
    return TaskSpec(path=str(raw["path"]), variants=list(variants))


# ---- shared --------------------------------------------------------------

def _require(raw: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        raise KeyError(f"missing required field(s): {missing}")
