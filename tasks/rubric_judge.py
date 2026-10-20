"""Content-level rubric judge for converted tasks.

A converted task's deliverable is judged against the source benchmark's rubric:
every criterion is scored on its own, PASS or FAIL, with only the deliverables
it names in context. This ports Harvey LAB's evaluator (``evaluation/scoring.py``
and ``evaluation/prompts/rubric_criterion.txt``) so ``evaluate()`` measures
what the memo *says*, not merely that a file exists.

Two design points worth knowing:

* **Text is extracted inside the owning role's sandbox.** The Ubuntu image
  ships pandoc, pandas, and poppler; the host does not. Extraction mirrors
  Harvey's harness (pandoc for .docx with tracked changes accepted, pandas for
  .xlsx, pdftotext for .pdf) so judged text matches the source benchmark.
* **The judge runs on the host** with the credentials the interaction metrics
  already use (``secret/.env``). Routing follows ``A2_JUDGE_PROVIDER`` when set
  and is otherwise inferred from which credentials exist (a Bedrock gateway
  URL, a Vertex project, AWS region, else direct). The model defaults to
  Harvey LAB's judge and is overridable with ``A2_RUBRIC_JUDGE_MODEL``.

The headline ``score`` is the pass fraction. Harvey's own headline is all-pass
(1.0 only when every criterion passes); that flag is reported alongside so both
readings are available without re-judging.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable

from tasks.task_spec import part, scored

DEFAULT_MODEL = "claude-sonnet-4-6"
MODEL_ENV = "A2_RUBRIC_JUDGE_MODEL"
PROVIDER_ENV = "A2_JUDGE_PROVIDER"

# Cap on the text handed to the judge per criterion. Harvey's judge fails loud
# on truncation; we truncate with a marker instead so one oversized deliverable
# does not void the whole rubric.
MAX_OUTPUT_CHARS = 400_000

# Marker :func:`extract_text` returns when a filed deliverable cannot be read.
# Criteria scoped to such a file fail without a judge call: the judge would
# only be paid to say "this is an error message".
ERROR_PREFIX = "(error reading "

# Harvey LAB's criterion prompt, verbatim, so verdicts stay comparable with the
# source benchmark. Placeholders are substituted with str.replace, never
# str.format, so braces in a memo or a criterion survive.
RUBRIC_PROMPT = """You are evaluating a legal AI agent's work product against a specific quality criterion.

## Task
{task_description}

## Agent's Output
{agent_output}

## Criterion
**{criterion_title}**

{match_criteria}

## Instructions
Evaluate the agent's output against the criterion above.
- **PASS**: The agent's output satisfies the criterion as described
- **FAIL**: The agent's output does not satisfy the criterion as described

Respond with JSON only:

```json
{
  "verdict": "pass" | "fail",
  "reasoning": "Brief explanation"
}
```
"""

Ask = Callable[[str], Awaitable[str]]


# =============================================================================
# Judge client
# =============================================================================

class RubricJudgeUnavailable(RuntimeError):
    """No usable judge: missing SDK or credentials. ``evaluate()`` fails loud."""


def build_judge(model: str | None = None, provider: str | None = None) -> Ask:
    """Return ``async (prompt) -> reply text`` bound to the configured model.

    Temperature 0 and no extended thinking, matching Harvey LAB's judge
    settings. The reply is plain text carrying a JSON object; no structured
    output feature is required from the provider.
    """
    from metrics.judge import JudgeUnavailable, build_anthropic_client, infer_provider

    model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    provider = provider or infer_provider()
    try:
        client = build_anthropic_client(provider)
    except JudgeUnavailable as exc:
        raise RubricJudgeUnavailable(str(exc)) from exc

    async def ask(prompt: str) -> str:
        response = await client.messages.create(
            model=model,
            max_tokens=2048,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            raise ValueError("judge declined to score this deliverable")
        return "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        )

    ask.model = model          # type: ignore[attr-defined]
    ask.provider = provider    # type: ignore[attr-defined]
    return ask


# =============================================================================
# Verdict parsing
# =============================================================================

def parse_verdict(text: str) -> tuple[str, str]:
    """``(verdict, reasoning)`` from a judge reply; ``verdict`` is pass|fail.

    Accepts a bare JSON object, a fenced one, or JSON embedded in prose. Falls
    back to a regex on the ``verdict`` key so a reply with a stray trailing
    sentence still counts. Raises ``ValueError`` when no verdict is found.
    """
    raw = (text or "").strip()
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(raw)
    braced = re.search(r"\{.*\}", raw, re.S)
    if braced:
        candidates.append(braced.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            verdict = str(value.get("verdict", "")).strip().lower()
            if verdict in ("pass", "fail"):
                return verdict, str(value.get("reasoning", "")).strip()
    loose = re.search(r'"verdict"\s*:\s*"(pass|fail)"', raw, re.I)
    if loose:
        reasoning = re.search(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.S)
        return loose.group(1).lower(), (reasoning.group(1) if reasoning else "").strip()
    raise ValueError(f"no verdict in judge reply: {raw[:200]!r}")


def render_prompt(*, task_description: str, agent_output: str,
                  criterion_title: str, match_criteria: str) -> str:
    out = RUBRIC_PROMPT
    for key, value in (
        ("task_description", task_description),
        ("agent_output", agent_output),
        ("criterion_title", criterion_title),
        ("match_criteria", match_criteria),
    ):
        out = out.replace("{" + key + "}", value)
    return out


# =============================================================================
# Deliverable discovery and text extraction (in-sandbox)
# =============================================================================

_XLSX_PY = r'''
import sys
import pandas as pd
sheets = pd.read_excel(sys.argv[1], sheet_name=None)
for name, frame in sheets.items():
    print(f"=== Sheet: {name} ===")
    print(frame.to_string(index=False))
'''

# Last-resort reader for OOXML when pandoc is missing or rejects the file:
# concatenates the text runs of word/document.xml or each slide.
_OOXML_PY = r'''
import html, re, sys, zipfile
archive = zipfile.ZipFile(sys.argv[1])
names = sorted(n for n in archive.namelist()
               if re.fullmatch(r"word/document\.xml|ppt/slides/slide\d+\.xml", n))
for name in names:
    xml = archive.read(name).decode("utf-8", "replace")
    xml = re.sub(r"</(w|a):p>", "\n", xml)
    print(html.unescape(re.sub(r"<[^>]+>", "", xml)))
'''


async def list_output_files(sandbox, output_dir: str) -> list[str]:
    """Relative paths of every file under ``output_dir`` (empty if absent)."""
    quoted = shlex.quote(output_dir.rstrip("/"))
    result = await sandbox.run_command(
        f"test -d {quoted} && cd {quoted} && find . -type f | sed 's|^\\./||' | sort",
        timeout=60,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _keywords(name: str) -> set[str]:
    stem = PurePosixPath(name).stem.lower().replace("-", " ").replace("_", " ")
    return set(stem.split())


def resolve_deliverable(expected: str, files: list[str],
                        exclude: set[str] | None = None) -> str | None:
    """Best-effort match of an expected deliverable to a produced file.

    Same ladder as Harvey LAB: exact path; else the only file with that
    extension; else the same-extension file with the most stem keywords in
    common. ``None`` when nothing plausible was filed. ``exclude`` holds files
    already claimed by another deliverable (see :func:`resolve_deliverables`).
    """
    if expected in files:
        return expected
    taken = exclude or set()
    files = [f for f in files if f not in taken]
    basename = PurePosixPath(expected).name
    same_name = [f for f in files if PurePosixPath(f).name == basename]
    if len(same_name) == 1:
        return same_name[0]
    suffix = PurePosixPath(expected).suffix.lower()
    candidates = [f for f in files if PurePosixPath(f).suffix.lower() == suffix]
    if len(candidates) == 1:
        return candidates[0]
    wanted = _keywords(expected)
    best, best_score = None, 0
    for candidate in candidates:
        overlap = len(wanted & _keywords(candidate))
        if overlap > best_score:
            best, best_score = candidate, overlap
    return best


def resolve_deliverables(expected: list[str], files: list[str]) -> dict[str, str | None]:
    """Match every expected deliverable, exact names first, no file used twice.

    Two passes so a fuzzy fallback never steals a file that another
    deliverable names exactly (a lone ``issue-memorandum.docx`` must not also
    stand in for a missing ``pricing-check.docx``).
    """
    resolved: dict[str, str | None] = {}
    used: set[str] = set()
    for name in expected:
        if name in files:
            resolved[name] = name
            used.add(name)
    for name in expected:
        if name in resolved:
            continue
        match = resolve_deliverable(name, files, exclude=used)
        resolved[name] = match
        if match is not None:
            used.add(match)
    return resolved


async def extract_text(sandbox, path: str) -> str:
    """Plain text of ``path`` read from inside the sandbox.

    .docx -> pandoc markdown (tracked changes accepted), .xlsx -> pandas,
    .pdf -> pdftotext, .pptx -> slide text, anything else -> raw bytes as
    UTF-8. Never raises: an unreadable file becomes an ``(error reading ...)``
    marker, which the judge will fail on its merits.
    """
    suffix = PurePosixPath(path).suffix.lower()
    quoted = shlex.quote(path)
    name = PurePosixPath(path).name
    try:
        if suffix == ".docx":
            text = await _command_text(
                sandbox,
                f"pandoc {quoted} -t markdown --wrap=none --track-changes=accept",
            )
            if text is None:
                text = await _python_text(sandbox, _OOXML_PY, path)
        elif suffix == ".pptx":
            text = await _python_text(sandbox, _OOXML_PY, path)
        elif suffix == ".xlsx":
            text = await _python_text(sandbox, _XLSX_PY, path)
        elif suffix == ".pdf":
            text = await _command_text(sandbox, f"pdftotext -layout {quoted} -")
        else:
            text = await sandbox.read_text(path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not void the rubric
        return f"(error reading {name}: {type(exc).__name__}: {exc})"
    if text is None:
        return f"(error reading {name}: no extractor succeeded)"
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n\n[... truncated at {MAX_OUTPUT_CHARS} characters ...]"
    return text


async def _command_text(sandbox, command: str) -> str | None:
    result = await sandbox.run_command(command, timeout=300)
    if result.returncode != 0:
        return None
    return result.stdout or ""


async def _python_text(sandbox, code: str, path: str) -> str | None:
    command = f"python3 - {shlex.quote(path)} <<'A2_RUBRIC_PY'\n{code}\nA2_RUBRIC_PY"
    return await _command_text(sandbox, command)


# =============================================================================
# Criterion judging
# =============================================================================

def _agent_output(criterion: dict, outputs: dict[str, str], missing: dict[str, str]) -> str | None:
    """Text the judge sees for one criterion, or ``None`` when nothing to judge.

    A criterion that names deliverables sees only those (Harvey's scoping); one
    that names none sees every gathered deliverable. A named deliverable that
    was not filed is shown as missing so the reasoning says why it failed.
    """
    names = [str(n) for n in (criterion.get("deliverables") or [])]
    if not names:
        if not outputs:
            return None
        return "\n\n".join(f"## Agent Output: {name}\n{text}" for name, text in outputs.items())
    if not any(name in outputs for name in names):
        return None
    sections = []
    for name in names:
        if name in outputs:
            sections.append(f"## Agent Output: {name}\n{outputs[name]}")
        else:
            sections.append(f"## Agent Output: {name}\n({missing.get(name) or 'file not found'})")
    return "\n\n".join(sections)


async def judge_criteria(
    criteria: list[dict],
    *,
    task_description: str,
    outputs: dict[str, str],
    ask: Ask,
    missing: dict[str, str] | None = None,
    concurrency: int = 8,
    retries: int = 2,
) -> list[dict[str, Any]]:
    """One verdict per criterion: ``{id, title, verdict, reasoning, deliverables}``.

    ``verdict`` is ``pass``, ``fail``, or ``error`` (judge unusable after
    retries; counted as a fail in the score, kept distinct for auditing).
    ``missing`` maps a deliverable that could not be judged to the reason (not
    filed, unreadable); criteria whose deliverables are all in it fail without
    a judge call.
    """
    missing = missing or {}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(criterion: dict) -> dict[str, Any]:
        base = {
            "id": str(criterion.get("id", "")),
            "title": str(criterion.get("title", "")),
            "deliverables": [str(n) for n in (criterion.get("deliverables") or [])],
        }
        agent_output = _agent_output(criterion, outputs, missing)
        if agent_output is None:
            reasons = [f"{n}: {missing.get(n) or 'not filed'}" for n in base["deliverables"]]
            return {**base, "verdict": "fail",
                    "reasoning": "; ".join(reasons) or "no deliverable filed; nothing to judge"}
        prompt = render_prompt(
            task_description=task_description,
            agent_output=agent_output,
            criterion_title=base["title"],
            match_criteria=str(criterion.get("match_criteria", "")),
        )
        last_error: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                async with semaphore:
                    reply = await ask(prompt)
                verdict, reasoning = parse_verdict(reply)
                return {**base, "verdict": verdict, "reasoning": reasoning}
            except Exception as exc:  # noqa: BLE001 - retry, then record
                last_error = exc
        return {**base, "verdict": "error",
                "reasoning": f"judge failed: {type(last_error).__name__}: {last_error}"}

    return list(await asyncio.gather(*(one(c) for c in criteria)))


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r.get("verdict") == "pass")
    errors = sum(1 for r in results if r.get("verdict") == "error")
    return {
        "n_criteria": total,
        "n_passed": passed,
        "n_error": errors,
        "pass_fraction": (passed / total) if total else 0.0,
        "all_pass": total > 0 and passed == total,
    }


# =============================================================================
# evaluate() for a converted task in rubric mode
# =============================================================================

async def evaluate_rubric(
    task_cfg,
    sandboxes: dict[str, Any],
    evaluation: dict[str, Any],
    criteria: list[dict],
    *,
    task_description: str,
    judge: Ask | None = None,
    concurrency: int = 8,
) -> dict[str, Any]:
    """Score the declared deliverables against ``criteria``.

    ``evaluation["deliverables"]`` is the converter's ``[{role, path}]`` list.
    Each deliverable's text is extracted from its owning role's sandbox; the
    criteria are judged; the result is one :func:`part` per deliverable-owning
    role with per-criterion booleans and the full verdict list as facts.
    """
    output_dir = str(task_cfg.metadata["remote_output_dir"]).rstrip("/")
    deliverables = [
        (str(item["role"]), str(item["path"]))
        for item in (evaluation.get("deliverables") or [])
    ]
    if not deliverables:
        raise ValueError("rubric evaluation declares no deliverables")
    if not criteria:
        raise ValueError("rubric evaluation declares no criteria")

    owner = {relative: role for role, relative in deliverables}
    resolved: dict[str, str | None] = {}
    for role in dict.fromkeys(r for r, _ in deliverables):
        files = await list_output_files(sandboxes[role], output_dir)
        expected = [rel for r, rel in deliverables if r == role]
        resolved.update(resolve_deliverables(expected, files))
    outputs: dict[str, str] = {}
    unjudgeable: dict[str, str] = {
        rel: "not filed" for rel, match in resolved.items() if match is None
    }
    for relative, match in resolved.items():
        if match is None:
            continue
        text = await extract_text(sandboxes[owner[relative]], f"{output_dir}/{match}")
        if text.startswith(ERROR_PREFIX):
            unjudgeable[relative] = "unreadable: " + text[len(ERROR_PREFIX):].rstrip(")")
        else:
            outputs[relative] = text

    if judge is None:
        judge = build_judge()
    results = await judge_criteria(
        criteria,
        task_description=task_description,
        outputs=outputs,
        ask=judge,
        missing=unjudgeable,
        concurrency=concurrency,
    )
    return assemble_result(
        deliverables, resolved=resolved, unjudgeable=unjudgeable, results=results,
        judge_model=getattr(judge, "model", None),
    )


_ISSUE_GROUP = re.compile(r"^\s*(ISSUE[_ -]?\w+)\s*:", re.I)


def research_metrics(results: list[dict[str, Any]],
                     resolved: dict[str, str | None]) -> dict[str, Any]:
    """Run-level numbers for cross-run aggregation, CRM-suite style.

    - ``end_to_end_completion``: 1.0 only when every criterion passed (the
      strict pass; Harvey LAB's and SWE-bench's headline convention).
    - ``rubric_pass_fraction``: the partial-credit score.
    - ``deliverable_completeness``: declared deliverables actually filed.
    - ``issue_completion``: fraction of Harvey issue groups (criteria titled
      ``ISSUE_nnn: ...``) whose every criterion passed; reported only when the
      rubric has such groups. Reads as "issues fully covered, not just touched".
    - ``judge_error_fraction``: criteria the judge could not score; a high value
      means the run's score is untrustworthy, not that the agent failed.
    """
    overall = summarize(results)
    filed = sum(1 for match in resolved.values() if match is not None)
    out: dict[str, Any] = {
        "end_to_end_completion": 1.0 if overall["all_pass"] else 0.0,
        "rubric_pass_fraction": overall["pass_fraction"],
        "deliverable_completeness": (filed / len(resolved)) if resolved else 0.0,
        "judge_error_fraction": (overall["n_error"] / overall["n_criteria"])
        if overall["n_criteria"] else 0.0,
        "criteria_total": overall["n_criteria"],
        "criteria_passed": overall["n_passed"],
    }
    groups: dict[str, list[bool]] = {}
    for result in results:
        match = _ISSUE_GROUP.match(str(result.get("title", "")))
        if match:
            groups.setdefault(match.group(1).upper(), []).append(result["verdict"] == "pass")
    if groups:
        complete = sum(1 for flags in groups.values() if all(flags))
        out["issue_completion"] = complete / len(groups)
        out["issues_total"] = len(groups)
        out["issues_fully_covered"] = complete
    return out


def assemble_result(
    deliverables: list[tuple[str, str]],
    *,
    resolved: dict[str, str | None],
    unjudgeable: dict[str, str],
    results: list[dict[str, Any]],
    judge_model: str | None,
    note_prefix: str = "rubric",
) -> dict[str, Any]:
    """Fold per-criterion verdicts into the ``evaluate()`` return shape.

    Shared by the in-run evaluator and the offline rescorer so both write the
    same breakdown: one :func:`part` per deliverable-owning role with
    per-criterion booleans, the verdict list as facts, and run-level
    :func:`research_metrics` inside ``breakdown``.
    """
    overall = summarize(results)
    owner = {relative: role for role, relative in deliverables}

    # Attribute each criterion to the role that owns its first named
    # deliverable; unscoped criteria belong to the first deliverable's owner.
    default_role = deliverables[0][0]
    by_role: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        role = next((owner[n] for n in result["deliverables"] if n in owner), default_role)
        by_role.setdefault(role, []).append(result)

    parts = []
    for role in dict.fromkeys(r for r, _ in deliverables):
        role_results = by_role.get(role, [])
        role_summary = summarize(role_results)
        role_artifacts = {rel: resolved[rel] for rel, r in owner.items() if r == role}
        missing_here = sorted(rel for rel, match in role_artifacts.items() if match is None)
        unreadable_here = sorted(
            f"{rel} ({unjudgeable[rel]})" for rel in role_artifacts
            if rel in unjudgeable and resolved[rel] is not None
        )
        reason_bits = ([f"missing {', '.join(missing_here)}"] if missing_here else []) + (
            [f"unreadable {', '.join(unreadable_here)}"] if unreadable_here else [])
        parts.append(part(
            role,
            role_summary["pass_fraction"],
            criteria={_label(r): r["verdict"] == "pass" for r in role_results},
            reason=("; ".join(reason_bits) or None),
            artifacts=role_artifacts,
            verdicts=role_results,
            judge_model=judge_model,
            **{k: v for k, v in role_summary.items() if k != "pass_fraction"},
        ))

    note = (
        f"{note_prefix}: {overall['n_passed']}/{overall['n_criteria']} criteria passed"
        + (f", {overall['n_error']} judge errors" if overall["n_error"] else "")
        + f"; all_pass={overall['all_pass']}"
    )
    return scored(overall["pass_fraction"], parts, note=note,
                  research_metrics=research_metrics(results, resolved))


def _label(result: dict[str, Any]) -> str:
    text = f"{result.get('id', '')} {result.get('title', '')}".strip()
    return text[:140] if text else "criterion"
