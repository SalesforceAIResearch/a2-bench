"""Generated legal practice_suite task with lazily loaded scenario variants."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from tasks.task_spec import Task, TaskYamlConfig, part, render, scored


_ROOT = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _index():
    value = json.loads((_ROOT / "scenario_index.json").read_text(encoding="utf-8"))
    if value.get("schema_version") != "a2-scenario-index-v1":
        raise ValueError("unsupported practice_suite scenario index")
    return value["scenarios"]


def variant_names():
    return [str(item["id"]) for item in _index()]


@lru_cache(maxsize=1)
def _position_by_id():
    return {str(item["id"]): position for position, item in enumerate(_index())}


def _variant_index(task_cfg):
    name = str(task_cfg.metadata["variant_name"])
    try:
        return _position_by_id()[name]
    except KeyError:
        raise KeyError(f"unknown practice_suite scenario {name!r}") from None


def _rubric(variant_index):
    """The sealed rubric for one scenario: host-side judge input, never staged."""
    entry = _index()[variant_index]
    if "rubric_offset" not in entry:
        raise LookupError(f"scenario {entry['id']!r} has no rubric")
    with (_ROOT / "rubrics.jsonl").open("rb") as handle:
        handle.seek(int(entry["rubric_offset"]))
        raw = handle.read(int(entry["rubric_length"]))
    value = json.loads(raw)
    if value.get("id") != entry.get("id"):
        raise ValueError(f"rubric index mismatch at variant {variant_index}")
    return value


def _record(variant_index):
    entries = _index()
    if variant_index < 0 or variant_index >= len(entries):
        raise IndexError(f"practice_suite variant index out of range: {variant_index}")
    entry = entries[variant_index]
    with (_ROOT / "scenarios.jsonl").open("rb") as handle:
        handle.seek(int(entry["offset"]))
        raw = handle.read(int(entry["length"]))
    value = json.loads(raw)
    if value.get("id") != entry.get("id"):
        raise ValueError(f"scenario index mismatch at variant {variant_index}")
    return value


def load_variant(variant_index=0):
    record = _record(variant_index)
    variant = str(record["id"])
    spec = {"metadata_extra": {"a2_conversion": record["manifest"]}}
    config = TaskYamlConfig(
        DOMAIN_NAME="legal",
        TASK_NAME="practice_suite",
        VARIANT_NAME=variant,
        REQUIRES_TASK_DATA=True,
        OS_TYPE="linux",
        spec=spec,
    )
    namespace = config.namespace
    visibility = record.get("visibility")
    if visibility == "mesh" or visibility is None:
        visibility = {}
    if not isinstance(visibility, dict):
        raise ValueError(f"invalid visibility for {variant}: {visibility!r}")
    # The registry is written with sorted keys; present roles with the assigned
    # lead first and specialists in workstream order so slugs and rosters read
    # naturally. Membership is unchanged.
    manifest = record["manifest"]
    ordered = [str(manifest["assigned_role"])]
    ordered += [str(w["role"]) for w in manifest.get("workstreams") or []]
    ordered += [name for name in record["roles"] if name not in ordered]
    return Task(
        roles={name: render(str(record["roles"][name]), namespace)
               for name in ordered if name in record["roles"]},
        first_speaker=str(record["first_speaker"]),
        assigned_role=str(record["assigned_role"]),
        assignment=render(str(record["assignment"]), namespace),
        agent_cards=record.get("agent_cards") or {},
        visibility=visibility,
        egress=record.get("egress") or {},
        metadata=config.to_metadata(),
        computer={"provider": record.get("provider", "docker"),
                  "setup_config": {"os_type": "linux"}},
        vm=record.get("vm") or {},
    )


async def start(task_cfg, sandboxes):
    output = task_cfg.metadata["remote_output_dir"]
    for sandbox in sandboxes.values():
        await sandbox.mkdir(output)


async def evaluate(task_cfg, sandboxes):
    scenario = task_cfg.metadata.get("a2_conversion") or {}
    evaluation = scenario.get("evaluation") or {}
    mode = evaluation.get("mode", "artifact_presence")
    if mode == "rubric":
        from tasks.rubric_judge import evaluate_rubric
        rubric = _rubric(_variant_index(task_cfg))
        return await evaluate_rubric(
            task_cfg, sandboxes, evaluation, rubric["criteria"],
            task_description=str(rubric.get("task_description") or ""),
        )
    results = []
    for item in evaluation.get("deliverables") or []:
        role = str(item["role"])
        relative = str(item["path"])
        output_dir = task_cfg.metadata["remote_output_dir"]
        path = f"{output_dir.rstrip('/')}/{relative}"
        exists = await sandboxes[role].exists(path)
        passed = exists
        reason = None if exists else f"missing {relative}"
        if exists and mode == "exact_text":
            reference_dir = task_cfg.metadata["reference_dir"]
            reference = f"{reference_dir.rstrip('/')}/{item['reference']}"
            try:
                actual = (await sandboxes[role].read_text(path)).strip()
                expected = (await sandboxes[role].read_text(reference)).strip()
                passed = actual == expected
                if not passed:
                    reason = "text did not match the sealed reference"
            except Exception as exc:
                passed = False
                reason = f"comparison failed: {type(exc).__name__}: {exc}"
        results.append((role, relative, passed, reason))

    score = sum(int(passed) for _, _, passed, _ in results) / len(results) if results else 0.0
    return scored(score, [
        part(role, 1.0 if passed else 0.0,
             criteria={path: passed}, reason=reason)
        for role, path, passed, reason in results
    ], note=(
        "artifact_presence measures filing completeness only; apply the source benchmark's "
        "semantic evaluator to gathered outputs" if mode == "artifact_presence" else None
    ))
