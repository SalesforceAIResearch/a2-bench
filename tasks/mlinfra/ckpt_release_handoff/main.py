"""a2a task: ``mlinfra/ckpt_release_handoff`` — Sharded-Checkpoint Release Handoff.

Two roles, two isolated sandboxes (anchor data reused verbatim from
agents-last-exam computing_math/mp_checkpoint_consolidation_v2):

  * ``trainer`` — training-infra engineer holding the 8 raw TP/PP/EP rank
    shards (312 MB) and the framework source that wrote them. Must
    consolidate them into ``output/model.safetensors``. Holds neither the
    target key schema nor the parity oracle.
  * ``release`` — serving/release engineer holding the serving stack
    (model.py + config.json), the 135-key schema, the probe ids and golden
    logits, and a commit-then-challenge fingerprint tool. Verifies remotely
    (diff-only) and files ``output/release_signoff.md``.

Host data layout (staged by the framework per role):

    task-data/mlinfra/ckpt_release_handoff/base/
    ├── trainer/{input/{checkpoints,framework}, software, reference}
    └── release/{input/{serving,acceptance}, software, reference}

``evaluate()`` is pure stdlib on the host; every byte of tensor math runs
INSIDE the trainer sandbox via a self-contained python snippet
(``TRAINER_METRICS_CODE``) that emits one JSON line — the host cannot read
161 MB binaries over the wire. Scoring is 0.5 trainer artifact quality
(key-set equality, per-tensor allclose at 1e-6, load gates, forward-pass
logit parity at 1e-3 — the anchor scorer's gates) + 0.5 release sign-off
quality (verdict-branched: an APPROVE must be true and numerically backed,
a REJECT must be true and evidenced; partial credit for procedurally
correct conduct when no artifact was delivered). All tolerances come from
the anchor's variant_metadata (tensor 1e-6, logit 1e-3, 135 keys,
missing == [lm_head.weight]).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from tasks.task_spec import load_tasks, part, scored, task_config

logger = logging.getLogger(__name__)

DOMAIN_NAME = "mlinfra"
TASK_NAME = "ckpt_release_handoff"

SHARD_NAMES = [
    f"shard_tp{tp}_pp{pp}_ep{ep}.pt"
    for tp in (0, 1) for pp in (0, 1) for ep in (0, 1)
]
FRAMEWORK_FILES = [
    "__init__.py", "config.py", "distributed.py",
    "kernels.py", "parallel.py", "precision.py",
]
ACCEPTANCE_FILES = [
    "serving_checkpoint_keys.json", "probe_input_ids.pt",
    "golden_logits.pt", "release_signoff_template.md",
]

# Anchor tolerances (task-data/.../reference/variant_metadata.json of the
# anchor — NOT staged anywhere agent-visible).
LOGIT_TOLERANCE = 1e-3
TENSOR_TOLERANCE = 1e-6
EXPECTED_KEY_COUNT = 135
EXPECTED_MISSING = ["lm_head.weight"]

# Must mirror release/software/fingerprint.py: seed IS the round index,
# 16 sorted flat indices sampled from range(numel).
N_COORDS = 16
MAX_ROUNDS = 24

# ---------------------------------------------------------------------------
# In-sandbox metrics snippet. Runs inside the TRAINER sandbox (torch +
# safetensors baked into the image), argv: <task_dir> [max_round].
# Contract: prints exactly one JSON object on the last stdout line.
# ---------------------------------------------------------------------------
TRAINER_METRICS_CODE = r'''
import importlib.util, json, os, random, sys

N_COORDS = 16

def coords_for_round(round_index, numel, n=N_COORDS):
    # Same scheme as the release-side fingerprint tool: seed = round index.
    rng = random.Random(round_index)
    return sorted(rng.sample(range(numel), min(n, numel)))

def main():
    task_dir = sys.argv[1]
    max_round = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    ref = os.path.join(task_dir, "reference")
    cand_path = os.path.join(task_dir, "output", "model.safetensors")
    out = {"artifact_present": False}
    try:
        import torch
        from safetensors.torch import load_file
    except Exception as exc:
        out["import_error"] = str(exc)[:300]
        print(json.dumps(out)); return
    with open(os.path.join(ref, "expected_keys.json")) as f:
        ref_keys = sorted(json.load(f))
    out["ref_key_count"] = len(ref_keys)
    if not os.path.exists(cand_path):
        print(json.dumps(out)); return
    out["artifact_present"] = True
    out["artifact_size"] = os.path.getsize(cand_path)
    try:
        cand = load_file(cand_path)
    except Exception as exc:
        out["load_file_error"] = str(exc)[:300]
        print(json.dumps(out)); return
    cand_keys = sorted(cand.keys())
    out["key_count"] = len(cand_keys)
    out["keys_match"] = cand_keys == ref_keys
    out["candidate_only_keys"] = sorted(set(cand_keys) - set(ref_keys))[:20]
    out["reference_only_keys"] = sorted(set(ref_keys) - set(cand_keys))[:20]
    ref_state = load_file(os.path.join(ref, "expected_model.safetensors"))
    shape_mismatches, means = [], []
    n_match, max_t = 0, 0.0
    for k in sorted(set(cand_keys) & set(ref_keys)):
        if tuple(cand[k].shape) != tuple(ref_state[k].shape):
            shape_mismatches.append(k)
            continue
        diff = (cand[k].float() - ref_state[k].float()).abs()
        dmax = float(diff.max())
        max_t = max(max_t, dmax)
        means.append(float(diff.mean()))
        if dmax <= 1e-6:
            n_match += 1
    out["shape_mismatches"] = shape_mismatches[:20]
    out["n_shape_mismatches"] = len(shape_mismatches)
    out["tensor_match_fraction"] = n_match / len(ref_keys) if ref_keys else 0.0
    out["max_tensor_abs_diff"] = max_t
    out["mean_tensor_abs_diff"] = sum(means) / len(means) if means else None
    try:
        spec = importlib.util.spec_from_file_location(
            "serving_model", os.path.join(ref, "model.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        model = module.build_model(os.path.join(ref, "config.json"))
        model.eval()
        missing, unexpected = model.load_state_dict(cand, strict=False)
        out["load_ok"] = True
        out["missing_after_load"] = list(missing)
        out["unexpected_after_load"] = list(unexpected)
    except Exception as exc:
        out["load_ok"] = False
        out["load_error"] = str(exc)[:300]
        print(json.dumps(out)); return
    try:
        input_ids = torch.load(os.path.join(ref, "input_ids.pt"),
                               map_location="cpu", weights_only=True)
        golden = torch.load(os.path.join(ref, "logits.pt"),
                            map_location="cpu", weights_only=True)
        with torch.no_grad():
            logits = model(input_ids)
        diff = (logits - golden).abs()
        out["max_abs_diff"] = float(diff.max())
        out["mean_abs_diff"] = float(diff.mean())
        flat = diff.flatten()
        numel = flat.numel()
        subset = {}
        for r in range(1, max_round + 1):
            idx = torch.tensor(coords_for_round(r, numel))
            subset[str(r)] = float(flat[idx].max())
        out["round_subset_max_abs_diff"] = subset
    except Exception as exc:
        out["forward_error"] = str(exc)[:300]
    print(json.dumps(out))

main()
'''


def load():
    return load_tasks(__file__)


async def start(task_cfg, sandboxes):
    """Assert per-role staging landed; ready both output dirs."""
    cfg = task_config(__file__)

    trainer = sandboxes["trainer"]
    missing = [
        f"{cfg.checkpoints_dir}/{name}" for name in SHARD_NAMES
        if not await trainer.exists(f"{cfg.checkpoints_dir}/{name}")
    ]
    missing += [
        f"{cfg.framework_dir}/{name}" for name in FRAMEWORK_FILES
        if not await trainer.exists(f"{cfg.framework_dir}/{name}")
    ]
    if missing:
        raise RuntimeError(
            f"trainer staging incomplete, missing: {missing[:4]} "
            f"(+{max(0, len(missing) - 4)} more). Check task-data/"
            f"{DOMAIN_NAME}/{TASK_NAME}/base/trainer/ on the host."
        )

    release = sandboxes["release"]
    missing = [
        f"{cfg.acceptance_dir}/{name}" for name in ACCEPTANCE_FILES
        if not await release.exists(f"{cfg.acceptance_dir}/{name}")
    ]
    for p in (f"{cfg.serving_dir}/model.py", f"{cfg.serving_dir}/config.json",
              cfg.fingerprint_path):
        if not await release.exists(p):
            missing.append(p)
    if missing:
        raise RuntimeError(
            f"release staging incomplete, missing: {missing}. Check "
            f"task-data/{DOMAIN_NAME}/{TASK_NAME}/base/release/ on the host."
        )

    for role in ("trainer", "release"):
        await sandboxes[role].mkdir(cfg.remote_output_dir)
    await trainer.rm([cfg.artifact_path])
    await release.rm([cfg.signoff_path])
    logger.info("[%s] staging verified; output dirs ready", TASK_NAME)


# ---------------------------------------------------------------------------
# evaluate() — host side is PURE STDLIB; tensor math runs in the trainer
# sandbox via TRAINER_METRICS_CODE.
# ---------------------------------------------------------------------------

async def _collect_trainer_metrics(cfg, trainer) -> dict:
    """Upload + run the metrics snippet inside the trainer sandbox."""
    # /tmp, not reference_dir: the reference tree is docker-cp staged
    # (root-owned), so writes there fail with EACCES.
    script_path = "/tmp/parity_report.py"
    await trainer.write_file(script_path, TRAINER_METRICS_CODE)
    result = await trainer.run_command(
        f"python3 {script_path} {cfg.task_dir} {MAX_ROUNDS}", timeout=900,
    )
    for line in reversed((result.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(
        f"trainer metrics snippet produced no JSON (rc={result.returncode}, "
        f"stderr={result.stderr[-400:] if result.stderr else ''!r})"
    )


def _artifact_passes(m: dict) -> bool:
    """The anchor's hard gates: keys + load result + logit parity."""
    return bool(
        m.get("artifact_present")
        and m.get("keys_match")
        and m.get("load_ok")
        and m.get("missing_after_load") == EXPECTED_MISSING
        and m.get("unexpected_after_load") == []
        and m.get("max_abs_diff") is not None
        and m["max_abs_diff"] <= LOGIT_TOLERANCE
    )


def _score_trainer(m: dict) -> tuple[float, dict[str, Any]]:
    """0.2 keys + 0.3 tensor-match fraction + 0.2 load gates + 0.3 parity.

    Returns the subscore and its rubric detail. ``tensor_match`` is scored as a
    fraction rather than a gate, so it reports the fraction it earned.
    """
    if not m.get("artifact_present"):
        return 0.0, {"reason": "no model.safetensors artifact delivered"}

    keys_ok = bool(m.get("keys_match"))
    match_frac = float(m.get("tensor_match_fraction") or 0.0)
    load_ok = bool(m.get("load_ok")
                   and m.get("missing_after_load") == EXPECTED_MISSING
                   and m.get("unexpected_after_load") == [])
    parity_ok = bool(m.get("max_abs_diff") is not None
                     and m["max_abs_diff"] <= LOGIT_TOLERANCE)

    s = (0.2 * keys_ok) + (0.3 * match_frac) + (0.2 * load_ok) + (0.3 * parity_ok)
    return s, {
        "criteria": {
            "keys_match": keys_ok,
            "tensors_match": match_frac,
            "loads_clean": load_ok,
            "logit_parity": parity_ok,
        },
        "key_count": m.get("key_count"),
        "max_abs_diff": m.get("max_abs_diff"),
        "missing_after_load": m.get("missing_after_load"),
        "unexpected_after_load": m.get("unexpected_after_load"),
    }


# ─── sign-off parsing (markdown or json) ───

_FLOAT_RE = r"([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)"


def _parse_signoff_md(text: str) -> dict:
    form: dict[str, Any] = {"raw": text}
    m = re.search(r"key_count\W*?(\d+)", text, re.IGNORECASE)
    if m:
        form["key_count"] = int(m.group(1))
    m = re.search(r"overall_max_abs_diff\W*?" + _FLOAT_RE, text, re.IGNORECASE)
    if m:
        form["overall_max_abs_diff"] = float(m.group(1))
    m = re.search(r"missing_keys_after_load\s*[:*]*\s*(.*)", text, re.IGNORECASE)
    if m:
        form["missing_keys_after_load"] = m.group(1).strip()
    m = re.search(r"extra_keys_after_load\s*[:*]*\s*(.*)", text, re.IGNORECASE)
    if m:
        form["extra_keys_after_load"] = m.group(1).strip()
    # Verdict: only count lines that name exactly one of APPROVE/REJECT
    # (the template's "APPROVE | REJECT" line names both — not a verdict).
    for line in text.splitlines():
        if not re.search(r"verdict", line, re.IGNORECASE):
            continue
        has_a = re.search(r"\bAPPROVE[DS]?\b", line, re.IGNORECASE)
        has_r = re.search(r"\bREJECT(?:ED)?\b", line, re.IGNORECASE)
        if has_a and not has_r:
            form["verdict"] = "APPROVE"
        elif has_r and not has_a:
            form["verdict"] = "REJECT"
    # Fingerprint-round table rows: | round | sha | max | mean | ok |
    rounds: dict[int, float] = {}
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.fullmatch(r"\d+", cells[0]):
            continue
        for cell in cells[2:]:
            fm = re.fullmatch(_FLOAT_RE, cell)
            if fm:
                rounds[int(cells[0])] = float(fm.group(1))
                break
    if rounds:
        form["rounds"] = rounds
    return form


def _parse_signoff_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    form: dict[str, Any] = {"raw": text}
    v = str(data.get("verdict", "")).upper()
    if v.startswith("APPROVE"):
        form["verdict"] = "APPROVE"
    elif v.startswith("REJECT"):
        form["verdict"] = "REJECT"
    if isinstance(data.get("key_count"), (int, float)):
        form["key_count"] = int(data["key_count"])
    if isinstance(data.get("overall_max_abs_diff"), (int, float)):
        form["overall_max_abs_diff"] = float(data["overall_max_abs_diff"])
    for k in ("missing_keys_after_load", "extra_keys_after_load"):
        if k in data:
            form[k] = json.dumps(data[k]) if not isinstance(data[k], str) else data[k]
    rounds = data.get("rounds") or data.get("fingerprint_rounds")
    if isinstance(rounds, dict):
        parsed = {}
        for rk, rv in rounds.items():
            try:
                parsed[int(rk)] = float(rv if not isinstance(rv, dict)
                                        else rv.get("max_abs_diff"))
            except (TypeError, ValueError):
                continue
        if parsed:
            form["rounds"] = parsed
    elif isinstance(rounds, list):
        parsed = {}
        for row in rounds:
            if isinstance(row, dict):
                try:
                    parsed[int(row.get("round"))] = float(row.get("max_abs_diff"))
                except (TypeError, ValueError):
                    continue
        if parsed:
            form["rounds"] = parsed
    return form


async def _read_signoff(cfg, release) -> dict:
    """Find + parse the filed sign-off; {} when none filed."""
    candidates = [
        (f"{cfg.remote_output_dir}/release_signoff.md", _parse_signoff_md),
        (f"{cfg.remote_output_dir}/release_signoff.json", _parse_signoff_json),
        (f"{cfg.acceptance_dir}/release_signoff.md", _parse_signoff_md),
        (f"{cfg.acceptance_dir}/release_signoff.json", _parse_signoff_json),
    ]
    for path, parser in candidates:
        try:
            text = await release.read_text(path)
        except FileNotFoundError:
            continue
        except Exception as exc:                                # noqa: BLE001
            logger.info("[%s] sign-off unreadable at %s: %s", TASK_NAME, path, exc)
            continue
        form = parser(text)
        if form:
            form["path"] = path
            return form
    return {}


def _keys_field_ok(value: str | None, expect_lm_head: bool) -> bool:
    """True iff a missing/extra-keys form field records the right key set."""
    if value is None:
        return False
    tokens = set(re.findall(r"[A-Za-z0-9_.]+", value)) - {"n", "a", "none", "None", "N", "A"}
    if expect_lm_head:
        return tokens == {"lm_head.weight"}
    return not tokens


def _consistent(reported: float, true_value: float) -> bool:
    return abs(reported - true_value) <= max(1e-4, 0.5 * abs(true_value))


def _score_release(form: dict, metrics: dict) -> tuple[float, dict[str, Any]]:
    """The reviewer's subscore plus the rubric that produced it.

    The branches are mutually exclusive (undelivered / APPROVE / REJECT /
    unparseable), so each reports only the criteria that applied to it —
    ``reason`` carries the branch when the whole side zeroed or was graded on
    conduct alone.
    """
    delivered = bool(metrics.get("artifact_present"))
    passes = _artifact_passes(metrics)
    verdict = form.get("verdict")
    facts = {"verdict": verdict or "(none parsed)",
             "signoff_path": form.get("path"),
             "artifact_delivered": delivered,
             "artifact_passes": passes}

    if not delivered:
        # Counterpart delivered nothing — credit procedurally correct
        # conduct: a filed, evidenced REJECT is the right paperwork.
        if verdict == "REJECT":
            evidenced = bool(re.search(
                r"(no artifact|not delivered|never (?:committed|delivered)|"
                r"missing|no sha256|no model\.safetensors)",
                form.get("raw", ""), re.IGNORECASE))
            return (0.7 if evidenced else 0.5), {
                "criteria": {"filed_reject_for_missing_artifact": True,
                             "cited_the_absence": evidenced},
                "reason": "counterpart delivered no artifact — graded on conduct",
                **facts,
            }
        if form:
            return 0.3, {"reason": "no artifact delivered; filed a sign-off "
                                   "that recorded no usable verdict", **facts}
        return 0.1, {"reason": "no artifact delivered and no sign-off filed",
                     **facts}

    if verdict == "APPROVE":
        if not passes:
            return 0.0, {"reason": "approved an artifact that fails parity — "
                                   "the cardinal sin", **facts}
        key_ok = form.get("key_count") == EXPECTED_KEY_COUNT
        keys_ok = (_keys_field_ok(form.get("missing_keys_after_load"), True)
                   and _keys_field_ok(form.get("extra_keys_after_load"), False))
        rec = form.get("overall_max_abs_diff")
        true_max = metrics.get("max_abs_diff")
        diff_ok = bool(rec is not None and rec <= LOGIT_TOLERANCE
                       and true_max is not None and _consistent(rec, true_max))
        # Magnitude cross-check over the deterministic per-round subsets.
        rounds = form.get("rounds") or {}
        subset = metrics.get("round_subset_max_abs_diff") or {}
        checked = [(r, v) for r, v in rounds.items() if str(r) in subset]
        rounds_ok = bool(checked
                         and all(_consistent(v, subset[str(r)]) for r, v in checked))
        s = 0.4 + 0.15 * (key_ok + keys_ok + diff_ok + rounds_ok)
        return s, {
            "criteria": {
                "verdict_correct": True,
                "key_count_reported": key_ok,
                "load_keys_reported": keys_ok,
                "max_diff_reported": diff_ok,
                "per_round_diffs_reported": rounds_ok,
            },
            "reported_max_abs_diff": rec,
            "true_max_abs_diff": true_max,
            **facts,
        }

    if verdict == "REJECT":
        if passes:
            return 0.0, {"reason": "rejected an artifact that passes parity",
                         **facts}
        raw = form.get("raw", "")
        names_gate = bool(re.search(
            r"(key schema|state[-_]?dict|load|parity|logit|diff|shape|orientation)",
            raw, re.IGNORECASE))
        has_number = bool(
            form.get("key_count") is not None
            or form.get("overall_max_abs_diff") is not None
            or form.get("rounds"))
        cited = bool(names_gate and has_number)
        # Evidence consistency with recomputed metrics (any one anchor).
        consistent = False
        if form.get("key_count") is not None and metrics.get("key_count") is not None:
            consistent |= form["key_count"] == metrics["key_count"]
        rec = form.get("overall_max_abs_diff")
        if rec is not None and metrics.get("max_abs_diff") is not None:
            consistent |= _consistent(rec, metrics["max_abs_diff"])
        subset = metrics.get("round_subset_max_abs_diff") or {}
        for r, v in (form.get("rounds") or {}).items():
            if str(r) in subset and _consistent(v, subset[str(r)]):
                consistent = True
        s = 0.5 + 0.25 * (cited + bool(consistent))
        return s, {
            "criteria": {
                "verdict_correct": True,
                "named_the_failing_gate": cited,
                "evidence_matches_recomputed": bool(consistent),
            },
            "reported_max_abs_diff": rec,
            "true_max_abs_diff": metrics.get("max_abs_diff"),
            **facts,
        }

    return 0.0, {"reason": "no parseable verdict filed", **facts}


async def evaluate(task_cfg, sandboxes) -> dict[str, Any]:
    """0.5 * trainer artifact quality + 0.5 * release sign-off quality."""
    cfg = task_config(__file__)

    metrics = await _collect_trainer_metrics(cfg, sandboxes["trainer"])
    if metrics.get("import_error"):
        raise RuntimeError(
            "trainer sandbox lacks torch/safetensors — the image must bake "
            f"them (see docker/ubuntu-base/Dockerfile): {metrics['import_error']}"
        )
    logger.info(
        "[%s] trainer metrics: present=%s keys_match=%s key_count=%s "
        "tensor_match_fraction=%s max_tensor_abs_diff=%s load_ok=%s "
        "missing=%s unexpected=%s max_abs_diff=%s",
        TASK_NAME, metrics.get("artifact_present"), metrics.get("keys_match"),
        metrics.get("key_count"), metrics.get("tensor_match_fraction"),
        metrics.get("max_tensor_abs_diff"), metrics.get("load_ok"),
        metrics.get("missing_after_load"), metrics.get("unexpected_after_load"),
        metrics.get("max_abs_diff"),
    )

    form = await _read_signoff(cfg, sandboxes["release"])
    logger.info(
        "[%s] sign-off: path=%s verdict=%s key_count=%s overall_max_abs_diff=%s "
        "rounds=%s", TASK_NAME, form.get("path"), form.get("verdict"),
        form.get("key_count"), form.get("overall_max_abs_diff"),
        form.get("rounds"),
    )

    trainer_score, trainer_detail = _score_trainer(metrics)
    release_score, release_detail = _score_release(form, metrics)
    combined = 0.5 * trainer_score + 0.5 * release_score
    logger.info(
        "[%s] scores: trainer=%.4f release=%.4f combined=%.4f "
        "(artifact_passes=%s)", TASK_NAME, trainer_score, release_score,
        combined, _artifact_passes(metrics),
    )
    return scored(combined, [
        part("trainer", trainer_score, weight=0.5, **trainer_detail),
        part("release", release_score, weight=0.5, **release_detail),
    ])
