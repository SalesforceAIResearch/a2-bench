"""Declarative task loader: build a task from its sibling ``task.yaml``.

A task directory is::

    tasks/<domain>/<task>/
      task.yaml    # ALL declarative setup (identity, vm, briefs, paths, cards)
      main.py      # only the scoring code: load()/start()/evaluate() + helpers

``main.py`` stays thin::

    from tasks.task_spec import load_tasks, task_config
    def load(): return load_tasks(__file__)
    async def evaluate(task_cfg, sandboxes):
        cfg = task_config(__file__)          # cfg.<path-key> resolves as before
        ...

Templating (byte-identity contract)
-----------------------------------
Briefs/assignment/description are stored with ``{placeholder}`` tokens over a
fixed namespace: the base dirs (``task_dir``, ``input_dir``, ``software_dir``,
``reference_dir``, ``remote_output_dir``, ``data_task_dir`` — all computed from
``LinuxTaskConfig`` per variant) plus every key under ``paths:``. Substitution
is a plain ``str.replace`` of each ``{key}`` token — NOT ``str.format`` — so
literal braces in embedded JSON/prose survive untouched (e.g. the tool_smoke
report schema). This reproduces the old f-string briefs verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tasks.linux_runtime import LinuxTaskConfig

# Base-dir placeholders available to every template. Path keys declared under
# the yaml's ``paths:`` are layered on top (they may reference these).
_BASE_KEYS = (
    "task_dir", "data_task_dir", "input_dir", "reference_dir",
    "software_dir", "remote_output_dir",
)


def render(template: str, namespace: dict[str, str]) -> str:
    """Substitute ``{key}`` tokens from ``namespace`` via plain replacement.

    Only exact ``{key}`` tokens are replaced; all other braces (e.g. literal
    JSON in a prompt) are left as-is. Longest keys first so no key that is a
    prefix of another can shadow it (belt-and-suspenders; the ``{}`` delimiters
    already make tokens unambiguous).
    """
    out = template
    for key in sorted(namespace, key=len, reverse=True):
        out = out.replace("{" + key + "}", namespace[key])
    return out


@dataclass
class Task:
    """Canonical task object the framework's ``TaskLoader`` consumes.

    Single-agent tasks leave ``roles`` empty and set ``description``; a2a tasks
    set ``roles``/``first_speaker`` and leave ``description`` empty. ``vm``
    carries the machine spec (formerly ``task_card.json``) through to the
    loader, which maps it onto the top-level image/timeout/size keys.
    """

    description: str = ""
    roles: dict[str, str] = field(default_factory=dict)
    first_speaker: str = ""
    assigned_role: str = ""
    assignment: str = ""
    agent_cards: dict[str, dict] = field(default_factory=dict)
    # Per-role peer allow-list: ``{role: [roles it may address]}``. Empty ⇒
    # full mesh (every role reaches every other — the historical default).
    # Resolved and symmetry-checked in :func:`resolve_visibility`.
    visibility: dict[str, list[str]] = field(default_factory=dict)
    # Per-role network egress policy: ``{role: "open"|"restricted"}`` for THIS
    # variant. Empty ⇒ every role open (the historical default — no filtering).
    # The concrete allowlist for a restricted role is derived run-side from the
    # harness bound to it; this map only says who is restricted. Resolved in
    # :func:`resolve_egress`. NEVER agent-visible (host-side setup only).
    egress: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    computer: dict[str, Any] = field(default_factory=dict)
    vm: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskYamlConfig(LinuxTaskConfig):
    """A ``LinuxTaskConfig`` whose extra path attributes come from ``task.yaml``.

    ``cfg.<path_key>`` (e.g. ``cfg.order_form_path``) resolves to the rendered
    absolute path, so scoring code reads paths exactly as it did off the old
    hand-written ``TaskConfig`` properties.
    """

    spec: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        ns = {k: getattr(self, k) for k in _BASE_KEYS}
        rendered: dict[str, str] = {}
        for key, tmpl in (self.spec.get("paths") or {}).items():
            rendered[key] = render(str(tmpl), ns)
            ns[key] = rendered[key]
        self._rendered_paths = rendered
        self._namespace = ns

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal attribute lookup fails. Never handle private
        # names here (guards against recursion before __post_init__ runs).
        if name.startswith("_"):
            raise AttributeError(name)
        paths = self.__dict__.get("_rendered_paths", {})
        if name in paths:
            return paths[name]
        raise AttributeError(name)

    @property
    def namespace(self) -> dict[str, str]:
        """Full template namespace: base dirs + rendered path keys."""
        return self._namespace

    @property
    def required_credentials(self) -> list[dict]:
        """Agent-side credentials, sourced from ``task.yaml`` (not task_card)."""
        return (
            self.spec.get("requiredCredentials")
            or self.spec.get("required_credentials")
            or []
        )

    def to_metadata(self) -> dict:
        m = super().to_metadata()
        for key, value in (self.spec.get("metadata_extra") or {}).items():
            m[key] = render(value, self._namespace) if isinstance(value, str) else value
        return m


def scored(
    total: float,
    parts: list[dict[str, Any]],
    *,
    note: str | None = None,
    research_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an ``evaluate()`` return value that carries its own rubric.

    The framework only needs ``score`` to rank a run, but a bare float can't
    answer "why 0.79?". Returning this instead persists the reasoning to
    ``eval_result.json`` under ``breakdown``, which is what the viewer's score
    drill-down reads::

        return scored(combined, [
            part("platform", platform_score, weight=0.5, **platform_detail),
            part("accounts", accounts_score, weight=0.5, **accounts_detail),
        ])

    ``research_metrics`` are run-level numbers a suite wants aggregated across
    runs (strict completion, coverage fractions, ...). They live *inside*
    ``breakdown`` because the run writer persists only ``score`` and
    ``breakdown`` to ``eval_result.json``; a top-level key is silently dropped.

    ``score`` stays top-level, so every existing consumer
    (:func:`a2_run.orchestration.unit_common.extract_score`) is unaffected.
    """
    out: dict[str, Any] = {"score": float(total),
                           "breakdown": {"total": float(total), "parts": parts}}
    if note:
        out["breakdown"]["note"] = note
    if research_metrics:
        out["breakdown"]["research_metrics"] = dict(research_metrics)
    return out


def part(
    role: str,
    score: float,
    *,
    weight: float | None = None,
    criteria: dict[str, Any] | None = None,
    reason: str | None = None,
    **facts: Any,
) -> dict[str, Any]:
    """One named component of a score — usually one role's side of the task.

    ``criteria`` is the per-check ``{name: bool}`` map a scorer already builds;
    ``reason`` explains a component that zeroed outright (a missing
    deliverable), and any remaining keyword arguments are recorded as ``facts``
    — measured values worth showing but not scored (counts, verdicts, statuses).
    """
    return {
        "role": role,
        "score": float(score),
        "weight": weight,
        "criteria": criteria or {},
        "reason": reason,
        "facts": facts,
    }


def _spec_path(main_file: str) -> Path:
    return Path(main_file).resolve().parent / "task.yaml"


def _load_spec(main_file: str) -> dict[str, Any]:
    with open(_spec_path(main_file), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    if not isinstance(spec, dict):
        raise ValueError(f"task.yaml at {_spec_path(main_file)} is not a mapping")
    return spec


_VARIANT_OVERRIDE_KEYS = frozenset({
    "assigned_role",
    "assignment",
    "description",
    "first_speaker",
    "metadata_extra",
    "summary",
    "title",
})


def resolve_variant_spec(spec: dict[str, Any], variant_name: str) -> dict[str, Any]:
    """Apply one variant's declarative scenario fields to the shared spec.

    Variants normally share the same work item and differ only in experimental
    conditions such as visibility. ``variant_overrides`` is for a suite whose
    variants are distinct data-backed work items that share roles and runtime.
    Identity, paths, VM settings, role briefs, and agent cards remain common.
    """
    overrides = spec.get("variant_overrides")
    if overrides is None:
        return spec
    if not isinstance(overrides, dict):
        raise ValueError("variant_overrides must be a {variant: fields} map")

    declared = {str(name) for name in (spec.get("variants") or ["base"])}
    unknown_variants = sorted(set(map(str, overrides)) - declared)
    if unknown_variants:
        raise ValueError(
            f"variant_overrides names unknown variant(s) {unknown_variants}; "
            f"declared variants are {sorted(declared)}"
        )

    entry = overrides.get(variant_name)
    if entry is None:
        return spec
    if not isinstance(entry, dict):
        raise ValueError(
            f"variant_overrides[{variant_name!r}] must be a mapping"
        )
    unknown_fields = sorted(set(entry) - _VARIANT_OVERRIDE_KEYS)
    if unknown_fields:
        raise ValueError(
            f"variant_overrides[{variant_name!r}] contains unsupported "
            f"field(s) {unknown_fields}"
        )

    resolved = {**spec, **entry}
    base_metadata = spec.get("metadata_extra") or {}
    variant_metadata = entry.get("metadata_extra") or {}
    if not isinstance(base_metadata, dict) or not isinstance(variant_metadata, dict):
        raise ValueError("metadata_extra must be a mapping")
    resolved["metadata_extra"] = {**base_metadata, **variant_metadata}
    return resolved


def _make_config(spec: dict[str, Any], variant_name: str) -> TaskYamlConfig:
    return TaskYamlConfig(
        DOMAIN_NAME=str(spec["domain"]),
        TASK_NAME=str(spec["task"]),
        VARIANT_NAME=str(variant_name),
        REQUIRES_TASK_DATA=bool(spec.get("requires_task_data", True)),
        OS_TYPE=str(spec.get("os_type", "linux")),
        spec=spec,
    )


def task_config(main_file: str, variant_index: int = 0) -> TaskYamlConfig:
    """Config object for ``start``/``evaluate`` to resolve paths from.

    Mirrors the old ``cfg = TaskConfig()`` call site: same base dirs, plus each
    ``paths:`` key exposed as an attribute.
    """
    spec = _load_spec(main_file)
    variants = spec.get("variants") or ["base"]
    variant_name = variants[variant_index]
    return _make_config(resolve_variant_spec(spec, variant_name), variant_name)


def resolve_visibility(spec: dict, variant_name: str, roles: list[str]) -> dict[str, list[str]]:
    """Concrete ``{role: [peers it may address]}`` for one variant.

    The optional ``visibility:`` block in task.yaml maps each variant name to
    one of:

    * ``mesh`` (or the block/entry omitted) — every role reaches every other.
      This is the historical default, so a task with no ``visibility:`` block
      is unchanged.
    * ``star`` — the ``assigned_role`` (the desk/hub) reaches every other role
      and vice-versa; NON-assigned roles reach ONLY the assigned role (a hub &
      spokes topology: buyers talk to the dealer, never to each other).
    * an explicit adjacency map ``{role: [peer, ...]}`` — used verbatim.

    The returned graph is always made **symmetric** and validated: a reply is
    a send back over the same roster, so a one-way edge (A may address B but B
    may not address A) would let A message B while silently dropping B's reply.
    We union both directions and fail loud on any edge naming an unknown role
    rather than degrade to mesh — a mis-typed roster must not silently widen
    who can collude.
    """
    role_set = set(roles)
    assigned = str(spec.get("assigned_role") or spec.get("first_speaker") or "")
    block = spec.get("visibility")

    # No block at all ⇒ full mesh (backward compatible).
    entry = "mesh" if not isinstance(block, dict) else block.get(variant_name, "mesh")

    if entry == "mesh":
        # A complete mesh is the implicit default (no restriction). Signal it
        # with an empty map so a task that doesn't opt into a topology stays
        # byte-identical to the pre-visibility behaviour downstream.
        return {}

    if entry == "star":
        if assigned not in role_set:
            raise ValueError(
                f"visibility 'star' needs a valid assigned_role/first_speaker; "
                f"{assigned!r} is not one of {sorted(role_set)}"
            )
        graph: dict[str, set[str]] = {r: set() for r in roles}
        for r in roles:
            if r != assigned:
                graph[r].add(assigned)
                graph[assigned].add(r)
    elif isinstance(entry, dict):
        graph = {r: set() for r in roles}
        for r, peers in entry.items():
            if r not in role_set:
                raise ValueError(
                    f"visibility[{variant_name!r}] names unknown role {r!r}; "
                    f"roles are {sorted(role_set)}"
                )
            for p in peers or []:
                if p not in role_set:
                    raise ValueError(
                        f"visibility[{variant_name!r}][{r!r}] names unknown "
                        f"peer {p!r}; roles are {sorted(role_set)}"
                    )
                if p == r:
                    continue
                graph[r].add(p)
                graph[p].add(r)  # symmetric: a reply travels the same edge
    else:
        raise ValueError(
            f"visibility[{variant_name!r}] must be 'mesh', 'star', or a "
            f"{{role: [peers]}} map, got {entry!r}"
        )

    # If an explicit map happens to describe a complete mesh, collapse it to
    # the default so it, too, carries no downstream footprint.
    if all(len(graph[r]) == len(roles) - 1 for r in roles):
        return {}
    return {r: sorted(graph[r]) for r in roles}


def resolve_egress(spec: dict, variant_name: str, roles: list[str]) -> dict[str, str]:
    """Concrete ``{role: "open"|"restricted"}`` for one variant.

    The optional ``egress:`` block in task.yaml maps each variant name to a
    ``{role: mode}`` map (modes: ``open`` / ``restricted``). Anything omitted
    defaults to ``open`` — so a task with no ``egress:`` block is unchanged and
    every role keeps full network access.

    This is HOST-SIDE setup only and never reaches an agent: it does not appear
    in any brief, agent card, or staged file. It says only WHICH roles are
    network-restricted for this variant; the concrete domain allowlist for a
    restricted role is derived run-side from the harness bound to it (its own
    model-backend host), so no endpoint strings live in the task file.

    Returns only the restricted roles (open is the default and carries no
    footprint), and fails loud on an unknown role name or mode rather than
    silently leaving a role open — a typo must not quietly widen access.
    """
    role_set = set(roles)
    block = spec.get("egress")
    if not isinstance(block, dict):
        return {}
    entry = block.get(variant_name)
    if entry is None:
        return {}
    if not isinstance(entry, dict):
        raise ValueError(
            f"egress[{variant_name!r}] must be a {{role: open|restricted}} map, "
            f"got {entry!r}"
        )
    resolved: dict[str, str] = {}
    for role, mode in entry.items():
        if role not in role_set:
            raise ValueError(
                f"egress[{variant_name!r}] names unknown role {role!r}; "
                f"roles are {sorted(role_set)}"
            )
        if mode not in ("open", "restricted"):
            raise ValueError(
                f"egress[{variant_name!r}][{role!r}] must be 'open' or "
                f"'restricted', got {mode!r}"
            )
        if mode == "restricted":
            resolved[role] = mode
    return resolved


def resolve_roles(spec: dict, variant_name: str, ns: dict[str, str]) -> dict[str, str]:
    """The per-role briefs (each agent's own description) for one variant, rendered.

    Default: the single top-level ``roles`` map, shared verbatim by every variant
    (the historical behaviour — variants differ only in host-side
    ``visibility``/``egress``, never in what any agent reads). An optional
    ``roles_overrides:`` block maps a variant NAME to a ``{role: brief}`` map;
    when it carries an entry for THIS variant + role, that brief replaces the
    shared one for that role only. This is the sanctioned way to make a variant's
    agent-visible SELF-DESCRIPTION differ from its siblings — e.g. a variant that
    appends a plainly-worded operating constraint to the agent's own description —
    without forking the task into a separate directory. The brief becomes the
    agent's standing context (the CLAUDE.md identity it reads directly), so this
    is exactly "a small extra section in the agent's description"; the shared
    ``assignment`` (the work item) stays byte-identical across variants. Keys are
    validated against the declared variants/roles in :func:`load_tasks`.
    """
    base = spec.get("roles") or {}
    per_variant: dict[str, Any] = {}
    overrides = spec.get("roles_overrides")
    if isinstance(overrides, dict):
        entry = overrides.get(variant_name)
        if isinstance(entry, dict):
            per_variant = entry
    return {
        role: render(str(per_variant.get(role, brief)), ns)
        for role, brief in base.items()
    }


def load_tasks(main_file: str) -> list[Task]:
    """Build one :class:`Task` per declared variant from the sibling task.yaml."""
    spec = _load_spec(main_file)
    variants = spec.get("variants") or ["base"]
    provider = str((spec.get("computer") or {}).get("provider") or spec.get("provider") or "docker")

    role_names = list((spec.get("roles") or {}).keys())

    # Optional per-variant role-brief override (see resolve_roles). Validate its
    # keys up front so a mistyped variant/role name fails loud rather than
    # silently leaving that variant on the shared brief.
    overrides = spec.get("roles_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError(
                f"roles_overrides must be a {{variant: {{role: brief}}}} map, got "
                f"{type(overrides).__name__}"
            )
        unknown_variants = sorted(set(map(str, overrides)) - set(map(str, variants)))
        if unknown_variants:
            raise ValueError(
                f"roles_overrides names unknown variant(s) {unknown_variants}; "
                f"declared variants are {variants}"
            )
        for vname, entry in overrides.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"roles_overrides[{vname!r}] must be a {{role: brief}} map, "
                    f"got {type(entry).__name__}"
                )
            unknown_roles = sorted(set(map(str, entry)) - set(map(str, role_names)))
            if unknown_roles:
                raise ValueError(
                    f"roles_overrides[{vname!r}] names unknown role(s) "
                    f"{unknown_roles}; declared roles are {role_names}"
                )

    tasks: list[Task] = []
    for variant_name in variants:
        variant_spec = resolve_variant_spec(spec, variant_name)
        cfg = _make_config(variant_spec, variant_name)
        ns = cfg.namespace
        tasks.append(Task(
            description=render(str(variant_spec.get("description", "")), ns),
            roles=resolve_roles(variant_spec, variant_name, ns),
            first_speaker=str(variant_spec.get("first_speaker", "")),
            assigned_role=str(variant_spec.get("assigned_role", "")),
            assignment=render(str(variant_spec.get("assignment", "")), ns),
            agent_cards=variant_spec.get("agent_cards") or {},
            visibility=resolve_visibility(variant_spec, variant_name, role_names),
            egress=resolve_egress(variant_spec, variant_name, role_names),
            metadata=cfg.to_metadata(),
            computer={"provider": provider, "setup_config": {"os_type": cfg.OS_TYPE}},
            vm=variant_spec.get("vm") or {},
        ))
    return tasks
