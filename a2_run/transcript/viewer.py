"""Render one run as a page of two tabs: its transcript and the judge's verdict.

The **transcript** tab is one merged timeline — the turns in order, each showing
what the agent received, what it *did* (reasoning, tool calls, observations, at
full length) and what it *said*. Where a judge quoted a message, that span is
highlighted in place with the judge's reasoning as an inline note, so the
evidence is read where it was said rather than in a list somewhere else.

**Fidelity.** The page shows everything the run recorded — full text with no
truncation, every step with its envelope (source, id, timestamp, per-step usage,
the deployer's ``extra``), images, the raw per-role trajectory metadata and the
orchestrator's ``events.jsonl``. Long content is collapsed behind a disclosure
or capped by a scroll box, never cut: the reader can always reach the exact
bytes. ``…`` appears only in one-line collapsed previews, whose expanded body
sits directly beneath.

The **judge** tab holds the offline metrics, one sub-tab per agent: dimension
scores, the judge's reasoning, and the same quotes as excerpts that jump back to
the message they came from. It is a peer of the transcript, not a preamble to it.

Above both: each agent's standing brief, cost/token usage at three grains (run,
role, turn), and — when the task reported a rubric — a score card showing which
criteria earned or lost the points (see :mod:`a2_run.transcript.breakdown`).

Pure functions of a :class:`RunView`; :mod:`a2_run.transcript.server` calls
:func:`render_run` to serve a run page live. The many-run views live in
:mod:`a2_run.transcript.dashboard`. No files are written and nothing touches the
network.
"""

from __future__ import annotations

import html
import json
from typing import Any

from .breakdown import extract_breakdown
from .loader import Act, ImageRef, Metrics, RunView, StepView, TurnBlock

# --- palette -----------------------------------------------------------------
# Flat, editorial, cream-paper look: a warm off-white page, solid hairline
# borders, no shadows / gradients / bevels. Brand primaries are blue, orange
# and yellow; a matching green is added for "ok" (not in the base set).
_C = {
    "ink": "#151515",         # near-black text
    "muted": "#55554f",       # warm dark gray — legible body text
    "faint": "#83837d",       # secondary labels
    "line": "#cfd0c9",        # hairline dividers on the cream page
    "hair": "#e2e3db",        # faint fill / row hover
    "bg": "#EEEFE9",          # page background (cream)
    "card": "#ffffff",        # surfaces
    "border": "#BFBFBC",      # solid card border
    "accent": "#1D4AFF",      # links / primary (brand blue)
    "ok": "#2f8a4d",          # created green, kept flat to match
    "bad": "#F54E00",         # brand orange-red
    "warn": "#9a7b16",        # darkened brand yellow (legible as text)
    # role colors: blue, orange-red, green, ochre — all legible as bold text
    "roles": ["#1D4AFF", "#F54E00", "#2f8a4d", "#9a7b16"],
    # flat per-act fills (light tints only — the 1px border does the separating,
    # the tint + colored label does the categorizing; no bevel edge)
    "bg_reason": "#fbf4dd",   # thinks — yellow tint
    "bg_tool": "#e9edff",     # calls  — blue tint
    "bg_obs": "#f3f4ee",      # result — neutral cream-gray
    "bg_err": "#fdeadf",      # error  — orange tint
    "lbl_reason": "#9a7b16",  # thinks label (darkened yellow)
    "lbl_tool": "#1D4AFF",    # calls label (blue)
    "lbl_err": "#F54E00",     # error label (orange)
}

_STATUS_TAG = {
    "completed": "ok", "success": "ok",
    "failed": "bad", "error": "bad", "cancelled": "bad", "not_executed": "bad",
    "timeout": "warn",
}

# Fixed-width, lowercase log labels instead of emoji — reads like a trace.
_ACT_LABEL = {
    "reason": "thinks", "tool": "calls", "obs": "result",
    "obs_err": "error", "note": "says", "env": "env",
    "sys": "system", "event": "step",
}

#: Body text longer than this gets a scroll box (CSS ``max-height``) rather than
#: pushing the rest of the turn off screen. The full text is still in the DOM and
#: still selectable/searchable — this caps height, not content.
_SCROLL_OVER = 1200


def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _fmt_num(x: Any, nd: int = 2) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


# --- metric formatting -------------------------------------------------------
def _fmt_tokens(n: int) -> str:
    """Compact token count: ``900`` / ``12.3k`` / ``1.4M``."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    s = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"{s}M"


def _fmt_cost(usd: float | None, *, partial: bool = False) -> str:
    """Dollar cost with precision that suits the magnitude; ``—`` when unknown."""
    if usd is None:
        return "—"
    if usd == 0:
        return "$0"
    if usd < 1:
        body = f"${usd:.4f}"
    elif usd < 100:
        body = f"${usd:.2f}"
    else:
        body = f"${usd:,.0f}"
    return ("≥ " + body) if partial else body


def _tokens_title(m: Metrics) -> str:
    """Full breakdown for a hover tooltip (raw, comma-grouped integers)."""
    return " · ".join([
        f"input {m.input_tokens:,}",
        f"output {m.output_tokens:,}",
        f"cache read {m.cache_read_tokens:,}",
        f"cache write {m.cache_creation_tokens:,}",
    ])


def _cost_title(m: Metrics) -> str:
    if m.cost_usd is None:
        return "no cost reported"
    base = f"${m.cost_usd:.6f}"
    return base + " — lower bound (a turn ended before reporting usage)" if m.partial else base


# =====================================================================================
# single run
# =====================================================================================
def _role_colors(rv: RunView) -> dict[str, str]:
    return {r: _C["roles"][i % len(_C["roles"])] for i, r in enumerate(rv.role_order())}


def _outcome_tag(rv: RunView) -> str:
    score = rv.score
    if rv.status in ("completed", "success"):
        if score is not None:
            cls = "ok" if score >= 1.0 else ("warn" if score > 0 else "bad")
            return f'<span class="tag {cls}">score {_fmt_num(score)}</span>'
        return '<span class="tag ok">completed</span>'
    return f'<span class="tag {_STATUS_TAG.get(rv.status, "warn")}">{_esc(rv.status)}</span>'


def _stat(label: str, value: str, *, title: str = "", strong: bool = False) -> str:
    t = f' title="{_esc(title)}"' if title else ""
    cls = "stat strong" if strong else "stat"
    return (f'<div class="{cls}"{t}><div class="k">{_esc(label)}</div>'
            f'<div class="v">{_esc(value)}</div></div>')


def _stats_strip(rv: RunView) -> str:
    """Run-level headline numbers: cost, tokens, turns, duration."""
    total = rv.total_metrics()
    cells = [
        _stat("cost", _fmt_cost(total.cost_usd, partial=total.partial),
              title=_cost_title(total), strong=True),
        _stat("tokens", _fmt_tokens(total.total_tokens),
              title=_tokens_title(total), strong=True),
        _stat("turns", str(len(rv.turns))),
    ]
    if rv.duration_s is not None:
        cells.append(_stat("duration", f"{rv.duration_s:.1f}s"))
    if rv.channel:
        cells.append(_stat("channel", rv.channel))
    return f'<div class="stats">{"".join(cells)}</div>'


def _role_chip(rv: RunView, role: str, col: str) -> str:
    meta = rv.roles.get(role, {})
    sub = " · ".join(s for s in [_esc(meta.get("model")), _esc(meta.get("executor"))] if s)
    lead = '<span class="lead">opens</span>' if role == rv.first_speaker else ""
    m = rv.role_metrics(role)
    usage = ""
    if m.has_data:
        usage = (
            f'<div class="chip-usage">'
            f'<span title="{_esc(_cost_title(m))}">{_esc(_fmt_cost(m.cost_usd, partial=m.partial))}</span>'
            f'<span class="dot">·</span>'
            f'<span title="{_esc(_tokens_title(m))}">{_esc(_fmt_tokens(m.total_tokens))} tok</span>'
            f'</div>'
        )
    return (
        f'<div class="role-chip" style="--rc:{col}">'
        f'<div class="chip-top"><b style="color:{col}">{_esc(role)}</b>{lead}</div>'
        f'{f"<div class=chip-meta>{sub}</div>" if sub else ""}'
        f'{usage}</div>'
    )


def _header(rv: RunView, colors: dict[str, str]) -> str:
    chips = "".join(_role_chip(rv, role, colors[role]) for role in rv.role_order())
    end = ""
    if rv.termination_reason:
        end = f'ended <b>{_esc(rv.termination_reason)}</b>'
        if rv.end_reason:
            end += f' <span class="muted">— {_esc(rv.end_reason)}</span>'
    return (
        f'<div class="run-head">'
        f'<div class="rh-top">{_outcome_tag(rv)}'
        f'<span class="runid">{_esc(rv.run_id)}</span></div>'
        f'{_stats_strip(rv)}'
        f'<div class="roles">{chips}</div>'
        f'{f"<div class=rh-end>{end}</div>" if end else ""}'
        f'</div>'
    )


def _score_card(rv: RunView, colors: dict[str, str]) -> str:
    """This run's score, broken into the criteria that produced it.

    Renders only when the task reported a breakdown (see
    :mod:`a2_run.transcript.breakdown`); otherwise the header's score tag is all
    there is to say and this is omitted rather than showing an empty shell.
    """
    bd = extract_breakdown(rv)
    if not bd:
        return ""

    parts = []
    for p in bd["parts"]:
        col = colors.get(p["role"], _C["muted"])
        rows = []
        for c in p["criteria"]:
            if c.get("passed") is None:
                mark, cls = "·", "skip"
            elif c["passed"]:
                mark, cls = "✓", "pass"
            else:
                mark, cls = "✕", "fail"
            frac = c.get("fraction")
            # A fractionally-credited criterion states the fraction it earned;
            # a plain gate just shows the mark.
            extra = (f' <span class="cfrac">{frac:.0%}</span>'
                     if frac is not None and 0 < frac < 1 else "")
            note = (f' <span class="muted">— {_esc(c["note"])}</span>'
                    if c.get("note") else "")
            rows.append(f'<li class="{cls}"><span class="mk">{mark}</span>'
                        f'<code>{_esc(c["name"])}</code>{extra}{note}</li>')
        facts = "".join(
            f'<div class="fact"><span class="fk">{_esc(k)}</span>'
            f'<span class="fv">{_esc(v)}</span></div>'
            for k, v in (p.get("facts") or {}).items() if v is not None
        )
        weight = (f'<span class="muted"> · weight {p["weight"]:.2f}</span>'
                  if p.get("weight") is not None else "")
        reason = (f'<div class="sc-reason">{_esc(p["reason"])}</div>'
                  if p.get("reason") else "")
        parts.append(
            f'<div class="sc-part">'
            f'<div class="sc-head"><b style="color:{col}">{_esc(p["role"])}</b>'
            f'<span class="sc-num">{_fmt_num(p["score"])}</span>{weight}</div>'
            f'{reason}'
            f'{f"<ul class=crit-list>{chr(10).join(rows)}</ul>" if rows else ""}'
            f'{f"<div class=facts>{facts}</div>" if facts else ""}'
            f'</div>'
        )
    note = (f'<p class="csub">{_esc(bd["note"])}</p>' if bd.get("note") else "")
    return (
        f'<details class="scorecard" open>'
        f'<summary><b>score {_fmt_num(bd["total"])}</b>'
        f'<span class="muted"> — how it was reached</span></summary>'
        f'<div class="sc-body">{note}{"".join(parts)}</div></details>'
    )


def _fmt_metric_val(v: Any) -> str:
    """One offline-metric score, rendered without inventing precision."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".")
    if v is None:
        return "—"
    return str(v)


#: A judge dimension at or above this (1-10, 1 = clean) is worth opening the
#: panel for. 4 is where the prompt's own bands stop being "hedged/partial".
_FLAG_AT = 4


def _turn_anchor(index: int) -> str:
    """DOM id of the timeline turn a judge citation at wire ``index`` refers to."""
    return f"t{index}"


def _cite_anchor(index: int, n: int) -> str:
    """DOM id of one highlighted span inside a turn's reply."""
    return f"c{index}-{n}"


def _citations_by_index(
    results: dict[str, dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """All verified citations grouped by the wire message they quote.

    The reply text is rendered once, so a message quoted by both agents' judges
    carries both sets of highlights; each citation keeps its ``role`` so the
    marker can be colored by whose judge raised it. Unresolved quotes are
    dropped here — they can't be located in the text to highlight.
    """
    out: dict[int, list[dict[str, Any]]] = {}
    for res in results.values():
        for role, payload in (res.get("roles") or {}).items():
            if not isinstance(payload, dict):
                continue
            for c in payload.get("citations") or []:
                if not isinstance(c, dict) or not c.get("resolved"):
                    continue
                index = c.get("index")
                if not isinstance(index, int):
                    continue
                out.setdefault(index, []).append({**c, "role": role})
    return out


def _highlight_reply(
    text: str, citations: list[dict[str, Any]], colors: dict[str, str],
) -> str:
    """Reply text with each cited span wrapped as a highlight + margin note.

    Spans are located by exact match, then whitespace-insensitively, and are
    laid down left to right; a citation that overlaps one already placed is
    skipped rather than producing nested markup. Everything outside a span is
    escaped normally, so this never emits unescaped model output.
    """
    spans: list[tuple[int, int, dict[str, Any]]] = []
    for c in citations:
        found = _locate_span(text, str(c.get("quote") or ""))
        if found is None:
            continue
        start, end = found
        if any(start < e and s < end for s, e, _ in spans):
            continue                      # overlaps an earlier highlight
        spans.append((start, end, c))
    if not spans:
        return _esc(text)

    spans.sort()
    parts, cursor = [], 0
    for n, (start, end, c) in enumerate(spans, start=1):
        role = str(c.get("role") or "")
        col = colors.get(role, _C["bad"])
        parts.append(_esc(text[cursor:start]))
        parts.append(
            f'<mark class="cmark" id="{_cite_anchor(c["index"], n)}"'
            f' style="--rc:{col}">{_esc(text[start:end])}'
            f'<span class="cnote" style="--rc:{col}">'
            f'<span class="cwho">{_esc(role)} judge</span>'
            f'{_esc(c.get("description") or "")}</span></mark>'
        )
        cursor = end
    parts.append(_esc(text[cursor:]))
    return "".join(parts)


def _locate_span(text: str, quote: str) -> tuple[int, int] | None:
    """Character span of ``quote`` in ``text``: exact, else whitespace-tolerant."""
    quote = quote.strip()
    if not quote:
        return None
    pos = text.find(quote)
    if pos != -1:
        return pos, pos + len(quote)

    # Whitespace-insensitive: match on a collapsed copy, map back to the
    # original offsets so the returned span still indexes `text`.
    norm, offsets = [], []
    pending = False
    for i, ch in enumerate(text):
        if ch.isspace():
            pending = bool(norm)
            continue
        if pending:
            norm.append(" ")
            offsets.append(i)
            pending = False
        norm.append(ch.casefold())
        offsets.append(i)
    hay = "".join(norm)
    needle = " ".join(quote.split()).casefold()
    pos = hay.find(needle)
    if pos == -1:
        return None
    return offsets[pos], offsets[pos + len(needle) - 1] + 1


def _wire_index(rv: RunView) -> dict[int, int]:
    """Judge citation index → position in :meth:`RunView.timeline`.

    The judge is shown only turns that delivered a non-empty message, numbered
    from 1 (see ``metrics.judge.format_transcript``), while the timeline renders
    every turn. Computed rather than assumed so an empty reply can't shift every
    citation link by one.
    """
    out: dict[int, int] = {}
    n = 0
    for position, turn in enumerate(rv.turns):
        if (turn.reply or "").strip():
            n += 1
            out[n] = position
    return out


def _score_pills(scores: dict[str, Any], *, flag_at: int = _FLAG_AT) -> str:
    """Dimension scores as pills, the notable ones highlighted."""
    cells = []
    for key, value in scores.items():
        cls = "fact"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if key in ("score", "min_score", "mean_score"):
                if value < 1.0:
                    cls = "fact hot"
            elif value >= flag_at:
                cls = "fact hot"
        cells.append(
            f'<div class="{cls}"><span class="fk">{_esc(key)}</span>'
            f'<span class="fv">{_esc(_fmt_metric_val(value))}</span></div>'
        )
    return f'<div class="facts">{"".join(cells)}</div>' if cells else ""


def _citation_excerpts(
    citations: list[dict[str, Any]], wire: dict[int, int], col: str,
) -> str:
    """The judge's evidence: each quote highlighted, with its reasoning.

    The same quotes are highlighted in place in the transcript tab; showing the
    excerpt here too means a verdict can be read without leaving the pane, while
    ``msg N`` still jumps to the full message for surrounding context. A quote the
    judge could not have taken verbatim is marked, since there is nothing to
    highlight.
    """
    items = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        index = c.get("index")
        desc = _esc(c.get("description") or "")
        quote = _esc(c.get("quote") or "")
        if c.get("resolved") and isinstance(index, int) and index in wire:
            where = (f'<a class="cref" href="#{_turn_anchor(index)}">'
                     f'msg {index}</a>')
        else:
            where = ('<span class="cref bad" title="not found verbatim in the '
                     'transcript — a pointer, not evidence">unverified</span>')
        items.append(
            f'<li class="cite">'
            f'<div class="chead2">{where}<span class="cdesc">{desc}</span></div>'
            f'<blockquote style="--rc:{col}">{quote}</blockquote></li>'
        )
    if not items:
        return ""
    return (
        f'<div class="mcites"><h4>evidence — also highlighted in the transcript</h4>'
        f'<ol class="cites">{"".join(items)}</ol></div>'
    )


def _metrics_panel(rv: RunView, colors: dict[str, str]) -> str:
    """The offline conduct metrics for this run: one sub-tab per agent.

    Each agent is a separate judge call with its own scores and reasoning, so the
    agents are tabs rather than a stack — you read one verdict against the other.
    The quotes behind a verdict are highlighted inline in the transcript tab, so
    this pane carries only the scores, the reasoning, and jump-links to them.

    Read generically from ``metrics.json``, so a metric added to the
    :mod:`metrics` package renders here with no change to this file. Omitted
    entirely when the run has not been scored.
    """
    results = rv.metric_results()
    if not results:
        return ""

    wire = _wire_index(rv)
    blocks = []
    for i, (name, res) in enumerate(sorted(results.items())):
        status = str(res.get("status") or "ok")
        bad = status != "ok"
        level = res.get("level")
        roles = {r: p for r, p in (res.get("roles") or {}).items()
                 if isinstance(p, dict)}

        # One radio group per metric drives its tabs — CSS-only, so the panel
        # needs no JS and keeps working in a saved page.
        tabs, panes = [], []
        for j, (role, payload) in enumerate(roles.items()):
            rid = f"m{i}r{j}"
            col = colors.get(role, _C["muted"])
            scores = payload.get("scores") or {}
            hot = any(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                and v >= _FLAG_AT for k, v in scores.items()
                if k not in ("score", "min_score", "mean_score")
            )
            tabs.append(
                f'<input type="radio" name="{name}-tabs" id="{rid}"'
                f'{" checked" if j == 0 else ""}>'
                f'<label class="mtab" for="{rid}" style="--rc:{col}">'
                f'{_esc(role)}{"<span class=dotflag></span>" if hot else ""}</label>'
            )
            summary = payload.get("summary") or ""
            justification = payload.get("justification") or ""
            panes.append(
                f'<div class="mpane">'
                f'{_score_pills(scores)}'
                f'{f"<div class=msum>{_esc(summary)}</div>" if summary else ""}'
                f'{f"<div class=mwhy><h4>why these scores</h4>{_esc(justification)}</div>"
                   if justification else ""}'
                f'{_citation_excerpts(payload.get("citations") or [], wire, col)}'
                f'</div>'
            )

        err = (f'<div class="sc-reason">{_esc(res.get("error"))}</div>'
               if res.get("error") else "")
        summary = res.get("summary") or ""
        blocks.append(
            f'<div class="mblock">'
            f'<div class="mhead"><b>{_esc(name)}</b>'
            f'{f"<span class=mtag>level {level}</span>" if level is not None else ""}'
            f'{f"<span class=mbad>{_esc(status)}</span>" if bad else ""}</div>'
            f'{err}'
            f'{f"<div class=msum>{_esc(summary)}</div>" if summary else ""}'
            f'{_score_pills(res.get("scores") or {})}'
            f'{f"<div class=mtabs>{''.join(tabs)}{''.join(panes)}</div>"
               if roles else ""}'
            f'</div>'
        )

    return "".join(blocks)


def _metrics_flagged(results: dict[str, dict[str, Any]]) -> bool:
    """True when any offline metric errored or scored something notable.

    Generic on purpose — it reads the score shapes a metric may report rather
    than naming one, so a new metric participates without a change here. Two
    conventions are understood: 1-10 dimensions where high is bad (the judge),
    and 0-1 scores where low is bad.
    """
    for res in results.values():
        if res.get("status") not in ("ok", None) or res.get("error"):
            return True
        buckets = [res.get("scores") or {}]
        buckets += [
            p.get("scores") or {} for p in (res.get("roles") or {}).values()
            if isinstance(p, dict)
        ]
        for scores in buckets:
            for key, v in scores.items():
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                # 0-1 "higher is better" scores: anything short of perfect.
                if key in ("score", "min_score", "mean_score"):
                    if v < 1.0:
                        return True
                # Everything else here is a 1-10 judge dimension (high is bad).
                elif v >= _FLAG_AT:
                    return True
    return False


def _brief_blocks(rv: RunView, colors: dict[str, str]) -> str:
    """Each role's standing brief, as a collapsed system-prompt block leading the timeline."""
    out = []
    for role, brief in rv.briefs().items():
        col = colors.get(role, _C["muted"])
        out.append(
            f'<details class="sys" style="--ac:{col}">'
            f'<summary><b style="color:{col}">{_esc(role)}</b>'
            f'<span class="muted"> — system brief</span></summary>'
            f'<div class="sys-body">{_esc(brief)}</div></details>'
        )
    if not out:
        return ""
    return f'<div class="briefs">{"".join(out)}</div>'


def _img_html(img: ImageRef) -> str:
    """One image, shown inline. ``path`` refs resolve through the file route."""
    if img.kind == "path":
        src = "file/" + img.src.lstrip("/")
    elif img.kind == "url":
        src = img.src
    else:                                   # inline base64 that survived
        src = f"data:{img.media_type};base64,{img.src}"
    alt = img.alt or img.src
    return (f'<figure class="shot"><img src="{_esc(src)}" alt="{_esc(alt)}" '
            f'loading="lazy">'
            f'<figcaption>{_esc(img.alt or img.src)}</figcaption></figure>')


def _scroll_cls(text: str) -> str:
    """``tall`` for a body long enough to need its own scroll box."""
    return " tall" if len(text) > _SCROLL_OVER else ""


def _args_html(a: Act) -> str:
    """A tool call's arguments in full.

    Rendered per-key rather than as one JSON blob so a long free-text value (an
    ``a2a_send_message`` body, a file's contents in a ``Write``) reads as the
    text it is instead of an escaped one-liner. Every key is shown, in call
    order, with no clipping.
    """
    if not a.arguments:
        # Non-dict arguments (or none) — show whatever was recorded.
        return (f'<div class="argsraw{_scroll_cls(a.tool_args)}">{_esc(a.tool_args)}</div>'
                if a.tool_args else "")
    rows = []
    for key, value in a.arguments.items():
        if isinstance(value, str):
            shown, multi = value, ("\n" in value or len(value) > 120)
        else:
            try:
                shown = json.dumps(value, indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                shown = str(value)
            multi = "\n" in shown or len(shown) > 120
        cls = "argv block" + _scroll_cls(shown) if multi else "argv"
        rows.append(f'<div class="argrow"><span class="argk">{_esc(key)}</span>'
                    f'<span class="{cls}">{_esc(shown)}</span></div>')
    return f'<div class="argsgrid">{"".join(rows)}</div>'


def _act_html(a: Act) -> str:
    label = _ACT_LABEL.get(a.kind, a.kind)
    cls = {"obs": "obs", "obs_err": "err", "reason": "reason", "tool": "tool",
           "note": "note", "env": "obs", "sys": "sys",
           "event": "obs"}.get(a.kind, "note")
    if a.kind == "tool":
        cid = (f'<span class="tcid" title="tool_call_id">{_esc(a.tool_call_id)}</span>'
               if a.tool_call_id else "")
        body = (f'<div class="tname"><code>{_esc(a.tool_name)}</code>{cid}</div>'
                f'{_args_html(a)}')
    else:
        pieces = []
        if a.text:
            pieces.append(f'<div class="txt{_scroll_cls(a.text)}">{_esc(a.text)}</div>')
        if a.images:
            pieces.append("".join(_img_html(i) for i in a.images))
        if not pieces:
            # Present but content-free — say so rather than render a blank row,
            # so "the tool returned nothing" stays readable as a fact.
            what = {"reason": "(empty thinking block)",
                    "event": "(step recorded no content — see its metadata)"}
            pieces.append(f'<div class="txt none">'
                          f'{_esc(what.get(a.kind, "(no content returned)"))}</div>')
        body = "".join(pieces)
    return (f'<div class="act {cls}"><span class="kind">{_esc(label)}</span>'
            f'<div class="body">{body}</div></div>')


def _kv_html(d: dict[str, Any], cls: str = "kvs") -> str:
    """A metadata dict as label/value rows; values JSON-encoded when not scalar."""
    rows = []
    for key, value in d.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            shown = "null" if value is None else str(value)
        else:
            try:
                shown = json.dumps(value, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                shown = str(value)
        block = " block" if "\n" in shown or len(shown) > 110 else ""
        rows.append(f'<div class="kv"><span class="kvk">{_esc(key)}</span>'
                    f'<span class="kvv{block}{_scroll_cls(shown)}">{_esc(shown)}</span></div>')
    return f'<div class="{cls}">{"".join(rows)}</div>' if rows else ""


#: Deployer ``extra`` keys that are pure plumbing: present on nearly every step
#: and carrying no signal a reader acts on. They stay in the step's hover title
#: (and verbatim in the record tab + the raw trajectory), but they don't get a
#: row of their own — printed on every step they buried the acts.
_PLUMBING_KEYS = frozenset({"stop_reason", "item_id", "turn_index", "round"})


def _step_html(s: StepView) -> str:
    """One step's acts.

    The envelope (step id, source, timestamp, per-step usage, plumbing keys) is
    reference detail that drowned the acts when printed on every row, so it moves
    to the step's hover title. Deployer metadata that actually says something —
    an exit code, a failed status, a file change — still gets its own expandable
    row. The acts themselves carry the step.
    """
    title = f"step {s.step_id} · {s.source}"
    if s.timestamp:
        title += f" · {s.timestamp}"
    if s.metrics is not None:
        title += f" · {_tokens_title(s.metrics)}"
    if s.extra:
        plumbing = " · ".join(f"{k}={s.extra[k]}" for k in sorted(s.extra)
                              if k in _PLUMBING_KEYS)
        if plumbing:
            title += f" · {plumbing}"
    notable = {k: v for k, v in s.extra.items() if k not in _PLUMBING_KEYS}
    meta = ""
    if notable:
        meta = (f'<details class="smeta"><summary>step {s.step_id} metadata'
                f'</summary>{_kv_html(notable, "kvs tight")}</details>')
    acts = "".join(_act_html(a) for a in s.acts)
    return f'<div class="step" title="{_esc(title)}">{acts}{meta}</div>'


def _turn_meta(b: TurnBlock) -> str:
    """Right-aligned per-turn stats: cost · tokens · duration."""
    bits = []
    if b.metrics is not None:
        m = b.metrics
        bits.append(f'<span class="tm-cost" title="{_esc(_cost_title(m))}">'
                    f'{_esc(_fmt_cost(m.cost_usd, partial=m.partial))}</span>')
        bits.append(f'<span title="{_esc(_tokens_title(m))}">'
                    f'{_esc(_fmt_tokens(m.total_tokens))} tok</span>')
    if b.duration_s is not None:
        bits.append(f'<span>{b.duration_s:.1f}s</span>')
    if not bits:
        return ""
    return '<span class="turn-meta">' + '<span class="dot">·</span>'.join(bits) + '</span>'



def _trace_summary(b: TurnBlock) -> str:
    """One line describing the work behind a message: what the agent did.

    The trace is how the agent got to its message, not the message itself, so it
    folds into a single honest line ("thought · 3 tools · 1 error") that expands
    to the full step list. This keeps the conversation readable at a glance while
    leaving every recorded act one click away.
    """
    acts = b.acts
    tools = [a for a in acts if a.kind == "tool"
             and not a.tool_name.endswith("a2a_send_message")]
    errors = sum(1 for a in acts if a.kind == "obs_err")
    thoughts = sum(1 for a in acts if a.kind == "reason" and a.text.strip())
    bits = []
    if thoughts:
        bits.append("thought" if thoughts == 1 else f"{thoughts} thoughts")
    if tools:
        names = []
        for a in tools:                     # first few tool names, deduped
            short = a.tool_name.rsplit("__", 1)[-1]
            if short not in names:
                names.append(short)
        shown = ", ".join(names[:3]) + (" …" if len(names) > 3 else "")
        bits.append(f"{len(tools)} tool call{'s' if len(tools) != 1 else ''} ({shown})")
    if errors:
        bits.append(f'<b class="tr-err">{errors} error{"s" if errors != 1 else ""}</b>')
    if not bits:
        return ""
    return " · ".join(bits)


def _trace_html(b: TurnBlock) -> str:
    """The agent's internal work for this turn, collapsed behind its summary."""
    summary = _trace_summary(b)
    if not summary and not b.steps:
        return ""
    steps = "".join(_step_html(s) for s in b.steps)
    label = summary or "trace"
    return (f'<details class="trace"><summary><span class="tr-lbl">trace</span>'
            f'<span class="tr-sum">{label}</span></summary>'
            f'<div class="trace-body">{steps}</div></details>')


def _turn_html(
    b: TurnBlock,
    col: str,
    wire_index: int | None = None,
    citations: list[dict[str, Any]] | None = None,
    colors: dict[str, str] | None = None,
    show_inbound: bool = False,
    envelope: str = "",
) -> str:
    """One turn as a message in the thread: who spoke, what they sent, how.

    Reads top-to-bottom the way a session does — the inbound that woke this agent
    (only when it isn't already the previous message in the thread), the trace of
    what it did, then the message it sent and to whom. The message is the
    prominent element; the trace is folded because it is *how* rather than *what*.
    """
    # The inbound is normally the previous speaker's message, already displayed
    # directly above — repeating it in full is what made the thread unreadable.
    # It is printed only when it ISN'T already on the page (the opening
    # assignment, or a service notice the relay injected); when only the
    # channel's envelope is new, the envelope alone is shown.
    inbound = ""
    if show_inbound and b.incoming:
        inbound = (f'<div class="inbound"><span class="in-lbl">received</span>'
                   f'<div class="in-body{_scroll_cls(b.incoming)}">'
                   f'{_esc(b.incoming)}</div></div>')
    elif envelope:
        inbound = (f'<div class="envelope" title="the peer\'s message above, '
                   f'as this agent received it">received '
                   f'<code>{_esc(envelope)}</code> + the message above</div>')

    # The judge's quotes are highlighted in place, so the evidence is read where
    # it was said rather than in a separate list.
    if b.reply:
        body = (_highlight_reply(b.reply, citations, colors or {}) if citations
                else _esc(b.reply))
        msg = f'<div class="msg-body{_scroll_cls(b.reply)}">{body}</div>'
        # A channel rewrite between emit and delivery is exactly what this view
        # exists to expose, so show BOTH texts when they differ rather than
        # picking one.
        if not b.reply_is_emitted and b.emitted.strip():
            msg += (f'<details class="emitted"><summary>the agent emitted '
                    f'different text — the channel rewrote it</summary>'
                    f'<div class="msg-body{_scroll_cls(b.emitted)}">'
                    f'{_esc(b.emitted)}</div></details>')
    elif b.trailing:
        msg = ""                         # no relayed message in a trailing block
    else:
        msg = '<div class="msg-body none">(no message delivered)</div>'

    to = (f'<span class="to">→ {_esc(b.sent_to)}</span>' if b.sent_to else "")
    tags = []
    if b.ended:
        tags.append('<span class="pill ok">ended the run</span>')
    if b.status != "completed":
        tags.append(f'<span class="pill bad">{_esc(b.status)}</span>')
    if b.trailing:
        tags.append('<span class="pill warn" title="work this agent did after its '
                    'last message — filing results, ending the run">'
                    'after last message</span>')
    if citations:
        tags.append(f'<span class="cflag" title="{len(citations)} judge '
                    f'citation(s) in this message">{len(citations)}</span>')

    err = (f'<div class="turn-err">{_esc(b.error)}</div>' if b.error else "")
    anchor = f' id="{_turn_anchor(wire_index)}"' if wire_index is not None else ""
    return (
        f'<div class="turn"{anchor} style="--ac:{col}">'
        f'<div class="turn-head">'
        f'<b class="who" style="color:{col}">{_esc(b.role)}</b>{to}'
        f'{"".join(tags)}{_turn_meta(b)}</div>'
        f'{inbound}{_trace_html(b)}{err}{msg}'
        f'</div>'
    )


def _subagent_html(traj: dict[str, Any], depth: int = 0) -> str:
    """A sub-agent trajectory, rendered as its own nested step list.

    Sub-agents (claude-code's ``Task`` tool, native sub-loops) are whole episodes
    the schema records and the timeline has no turn to hang them off, so they get
    their own disclosure per role rather than going unmentioned.
    """
    from .loader import _step_view       # local: internal helper, same package

    agent = traj.get("agent") or {}
    label = " · ".join(str(x) for x in
                       [agent.get("name"), agent.get("model")] if x) or "subagent"
    steps = [s for s in (traj.get("steps") or []) if isinstance(s, dict)]
    inner = "".join(_step_html(_step_view(s)) for s in steps)
    nested = "".join(_subagent_html(s, depth + 1)
                     for s in (traj.get("subagent_trajectories") or [])
                     if isinstance(s, dict))
    instruction = str(traj.get("instruction") or "")
    instr = (f'<div class="sub-instr{_scroll_cls(instruction)}">{_esc(instruction)}</div>'
             if instruction else "")
    return (f'<details class="subagent"><summary>subagent '
            f'<b>{_esc(label)}</b>'
            f'<span class="muted"> — {len(steps)} step(s)</span></summary>'
            f'<div class="sub-body">{instr}{inner}{nested}</div></details>')


def _role_record(rv: RunView, role: str, col: str) -> str:
    """One role's raw trajectory record: agent info, totals, timings, ``extra``.

    ``extra`` is where the deployers put the CLI's ``system`` init event (model,
    tool list, MCP servers, permission mode), the authoritative ``result``
    events, resume/session boundaries and any ``replay_degraded`` flag. It is all
    part of what the run recorded, so it is shown verbatim rather than filtered.
    """
    started, ended = rv.role_timings(role)
    info = dict(rv.role_agent_info(role))
    final = dict(rv.role_final(role))
    timing = {k: v for k, v in (("started_at", started), ("ended_at", ended)) if v}
    subs = "".join(_subagent_html(s) for s in rv.subagents(role))
    sections = []
    if info:
        sections.append(f'<h5>agent</h5>{_kv_html(info)}')
    if final:
        sections.append(f'<h5>final_metrics</h5>{_kv_html(final)}')
    if timing:
        sections.append(f'<h5>timings</h5>{_kv_html(timing)}')
    extra = rv.role_extra(role)
    if extra:
        sections.append(f'<h5>trajectory extra</h5>{_kv_html(extra)}')
    if subs:
        sections.append(f'<h5>subagents</h5>{subs}')
    if not sections:
        return ""
    return (f'<details class="rrec" style="--rc:{col}">'
            f'<summary><b style="color:{col}">{_esc(role)}</b>'
            f'<span class="muted"> — trajectory record</span></summary>'
            f'<div class="rrec-body">{"".join(sections)}</div></details>')


def _events_panel(rv: RunView) -> str:
    """The orchestrator's ``events.jsonl`` as a table.

    The runner's own log: provisioning, per-role session start/finish, and the
    terminal record. It is the only place some facts appear — a role that never
    started has no trajectory to show.
    """
    events = rv.events()
    if not events:
        return ""
    rows = []
    for e in events:
        if e.get("type") == "unparsed":
            rows.append(f'<tr><td class="nowrap">—</td><td>unparsed</td>'
                        f'<td><code>{_esc(e.get("raw"))}</code></td></tr>')
            continue
        data = e.get("data")
        if isinstance(data, dict):
            try:
                shown = json.dumps(data, ensure_ascii=False)
            except (TypeError, ValueError):
                shown = str(data)
        else:
            shown = "" if data is None else str(data)
        rows.append(
            f'<tr><td class="nowrap">{_esc(e.get("ts", ""))}</td>'
            f'<td><b>{_esc(e.get("type", ""))}</b></td>'
            f'<td class="evd"><code>{_esc(shown)}</code></td></tr>'
        )
    return (f'<details class="rrec"><summary><b>events.jsonl</b>'
            f'<span class="muted"> — the orchestrator\'s run log '
            f'({len(events)} records)</span></summary>'
            f'<div class="rrec-body"><table class="evt"><thead><tr><th>ts</th>'
            f'<th>type</th><th>data</th></tr></thead><tbody>'
            f'{"".join(rows)}</tbody></table></div></details>')


def _artifacts_panel(rv: RunView) -> str:
    """Every file in the run dir, linked — the unprocessed source behind the page.

    Includes each agent's own ``origin_log/<role>/transcript_000.jsonl``, its
    prompt, stderr and the task's output files, so any rendering here can be
    checked against the record it came from.
    """
    files = rv.artifacts()
    if not files:
        return ""
    rows = "".join(
        f'<tr><td><a href="file/{_esc(rel)}">{_esc(rel)}</a></td>'
        f'<td class="num nowrap">{size:,}</td></tr>'
        for rel, size in files
    )
    return (f'<details class="rrec"><summary><b>files</b>'
            f'<span class="muted"> — the raw record ({len(files)})</span></summary>'
            f'<div class="rrec-body"><table class="evt"><thead><tr><th>path</th>'
            f'<th class="num">bytes</th></tr></thead><tbody>{rows}</tbody>'
            f'</table></div></details>')


def _records_panel(rv: RunView, colors: dict[str, str]) -> str:
    """The record tab: per-role trajectory metadata, the run log, the raw files."""
    parts = [_role_record(rv, role, colors.get(role, _C["muted"]))
             for role in rv.role_order()]
    parts.append(_events_panel(rv))
    parts.append(_artifacts_panel(rv))
    body = "".join(p for p in parts if p)
    return f'<div class="records">{body}</div>' if body else ""


def _timeline(rv: RunView, colors: dict[str, str]) -> str:
    """The conversation as one thread, each agent on its own side.

    Turns alternate left/right by role so the handoff between agents is visible
    as shape rather than having to be read off a label. An inbound message is
    rendered only when it is NOT the message directly above it — otherwise every
    message would appear twice (once as the sender's, once as the receiver's
    quoted input), which is what made the thread hard to follow.
    """
    blocks = rv.timeline()
    if not blocks:
        return '<p class="muted">No conversation recorded.</p>'
    # Reverse of _wire_index: which judge-visible message number each turn is.
    msg_no = {pos: n for n, pos in _wire_index(rv).items()}
    cites = _citations_by_index(rv.metric_results())
    # Left/right by role, in the order they first speak.
    order = rv.role_order()
    side = {role: ("l" if i % 2 == 0 else "r") for i, role in enumerate(order)}

    out = []
    prev_sent = None            # the text the previous turn put on the wire
    for i, b in enumerate(blocks):
        # Show the inbound only when its BODY isn't already the message above.
        # The channel delivers the peer's text wrapped in a ``from: <name>``
        # envelope, so an exact comparison would call every inbound "new" and
        # reprint the whole conversation twice.
        show, env = _inbound_novelty(b.incoming, prev_sent)
        out.append(
            f'<div class="row {side.get(b.role, "l")}">'
            + _turn_html(b, colors.get(b.role, _C["muted"]), msg_no.get(i),
                         cites.get(msg_no.get(i, -1)), colors,
                         show_inbound=show, envelope=env)
            + '</div>'
        )
        if b.reply:
            prev_sent = b.reply
    return (f'{_brief_blocks(rv, colors)}'
            f'<div class="timeline">{"".join(out)}</div>')


def _inbound_novelty(incoming: str, prev_sent: str | None) -> tuple[bool, str]:
    """Decide whether an inbound needs printing, and pull out its envelope.

    Returns ``(show_body, envelope)``. When the inbound is just the previous
    message re-delivered under the channel's envelope, the body is redundant
    (it is directly above) but the envelope is not — it is what the receiving
    agent was told about the sender — so only the envelope is shown. It is
    recovered as the prefix preceding the known body, so any shape works
    (``direct``'s one line, ``a2a``'s multi-line header). An inbound that is
    genuinely new (the opening assignment, a service notice) prints in full.
    """
    text = (incoming or "").strip()
    if not text:
        return False, ""
    if prev_sent is None:
        return True, ""
    body = prev_sent.strip()
    if text == body:
        return False, ""
    if body and text.endswith(body):
        return False, text[: len(text) - len(body)].strip()
    return True, ""


#: CSS pairing each per-agent tab radio with its pane. Generated rather than
#: hand-listed so a run with more roles than anticipated cannot end up with a
#: pane no selector can reveal (12 is far past any real a2a role count).
_MTAB_RULES = ",\n".join(
    f".mtabs>input:nth-of-type({n}):checked~.mpane:nth-of-type({n})"
    for n in range(1, 13)
) + "{display:block}"

#: Same pairing for the page-level tabs (transcript / judge / record), generated
#: so adding a pane can't leave it unreachable.
_PTAB_RULES = ",\n".join(
    f".ptabs>input:nth-of-type({n}):checked~.ppane:nth-of-type({n})"
    for n in range(1, 7)
) + "{display:block}"


_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font:16px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;color:%(ink)s;background:%(bg)s;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:none;width:100%%;margin:0;padding:36px 48px 120px}
.wrap.wide{max-width:none}
/* readable-measure column for prose-heavy timeline content on very wide screens */
.timeline,.briefs{max-width:1400px}
@media(max-width:900px){.wrap{padding:28px 20px 96px}}
a{color:%(accent)s;text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:25px;font-weight:700;letter-spacing:-.015em;margin:0 0 4px;line-height:1.25}
.crumb{font-size:13px;letter-spacing:.03em;text-transform:uppercase;color:%(faint)s;margin:0 0 18px}
.muted{color:%(muted)s}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;color:#2b2b2b;word-break:break-word}
/* -- run header: a rule-bounded band, no card -- */
.run-head{margin:16px 0 30px}
.rh-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.runid{font-size:12.5px;color:%(faint)s;font-family:ui-monospace,monospace;word-break:break-all}
.tag{display:inline-block;padding:1px 0;font-size:15px;font-weight:700;letter-spacing:-.01em}
.tag.ok{color:%(ok)s} .tag.bad{color:%(bad)s} .tag.warn{color:%(warn)s}
/* -- stats strip: bare numbers between hairlines, no box -- */
.stats{display:flex;flex-wrap:wrap;gap:40px;margin:18px 0 0;padding:18px 0;border-top:1px solid %(line)s;border-bottom:1px solid %(line)s}
.stat .k{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:%(faint)s}
.stat .v{font-size:17px;font-variant-numeric:tabular-nums;margin-top:4px}
.stat.strong .v{font-size:24px;font-weight:700;letter-spacing:-.02em}
/* -- roles: flat cards with a solid hairline border, one accent tick -- */
.roles{display:flex;flex-direction:column;gap:8px;margin-top:20px}
.role-chip{display:flex;align-items:baseline;gap:12px;padding:10px 14px;background:%(card)s;border:1px solid %(border)s;border-radius:8px;flex-wrap:wrap}
.chip-top{display:flex;align-items:baseline;gap:9px;font-size:16px}
.chip-top .lead{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:%(faint)s}
.chip-meta{font-size:14px;color:%(muted)s}
.chip-usage{font-size:14px;color:%(muted)s;margin-left:auto;font-variant-numeric:tabular-nums}
.chip-usage .dot{color:%(faint)s;margin:0 6px}
.rh-end{font-size:15px;margin-top:18px;color:%(muted)s}
/* -- system brief: flat disclosure card -- */
.sys{margin:0;background:%(card)s;border:1px solid %(border)s;border-radius:8px}
.sys+.sys{margin-top:8px}
.sys>summary{padding:10px 14px;cursor:pointer;list-style:none;font-size:15px;color:%(muted)s}
.sys>summary::-webkit-details-marker{display:none}
.sys>summary::before{content:'▸';color:%(faint)s;margin-right:9px;font-size:11px}
.sys[open]>summary::before{content:'▾'}
.sys[open]>summary{border-bottom:1px solid %(line)s}
.sys-body{padding:12px 14px;font-size:14px;line-height:1.65;white-space:pre-wrap;color:%(muted)s}
.briefs{margin:8px 0 4px;display:flex;flex-direction:column;gap:8px}
/* -- score card: the rubric behind the number -- */
.scorecard{margin:0 0 14px;background:%(card)s;border:1px solid %(border)s;border-radius:8px;max-width:1400px}
.scorecard>summary{padding:11px 15px;cursor:pointer;list-style:none;font-size:15px}
.scorecard>summary::-webkit-details-marker{display:none}
.scorecard>summary::before{content:'▾';color:%(faint)s;margin-right:9px;font-size:11px}
.scorecard:not([open])>summary::before{content:'▸'}
.scorecard[open]>summary{border-bottom:1px solid %(line)s}
.sc-body{padding:6px 15px 14px;display:flex;flex-direction:column;gap:4px}
.sc-body .csub{font-size:14px;color:%(muted)s;margin:8px 0 2px}
.sc-part{padding:11px 0;border-bottom:1px solid %(hair)s}
.sc-part:last-child{border-bottom:none}
.sc-head{display:flex;align-items:baseline;gap:10px;font-size:15px}
.sc-num{font-variant-numeric:tabular-nums;font-weight:700}
.sc-reason{font-size:14px;color:%(bad)s;margin-top:6px}
.crit-list{list-style:none;margin:9px 0 0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2px 20px}
.crit-list li{font-size:14px;display:flex;align-items:baseline;gap:8px;padding:3px 0}
.crit-list .mk{font-size:12px;width:11px;flex:0 0 11px;text-align:center}
.crit-list li.pass .mk{color:%(ok)s}
.crit-list li.fail .mk{color:%(bad)s}
.crit-list li.fail code{font-weight:700}
.crit-list li.skip .mk{color:%(faint)s}
.cfrac{font-size:12.5px;color:%(warn)s;font-variant-numeric:tabular-nums}
.facts{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px;padding-top:9px;border-top:1px dashed %(hair)s}
.fact{font-size:13px;display:flex;gap:7px}
.fact .fk{color:%(faint)s}
.fact .fv{font-family:ui-monospace,monospace;color:%(muted)s;overflow-wrap:anywhere}
/* -- offline metrics panel -- */
.mblock{padding:11px 0;border-bottom:1px solid %(hair)s}
.mblock:last-child{border-bottom:none}
.mhead{display:flex;align-items:baseline;gap:10px;font-size:15px}
.mtag{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:%(faint)s}
.mbad{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:%(bad)s;font-weight:700;margin-left:2px}
.msum{font-size:14px;color:%(muted)s;margin-top:5px}
/* a score worth looking at, per _FLAG_AT */
.fact.hot .fv{color:%(bad)s;font-weight:700}
.fact.hot .fk{color:%(muted)s}
/* -- page tabs: transcript | judge -- */
.ptabs{margin-top:26px}
.ptabs>input{position:absolute;opacity:0;pointer-events:none}
.ptab{display:inline-block;font:inherit;font-size:14.5px;font-weight:600;
 padding:9px 18px;color:%(muted)s;cursor:pointer;border-bottom:2px solid transparent}
.ptab:hover{color:%(ink)s}
.ptabs>input:checked+.ptab{color:%(ink)s;border-bottom-color:%(accent)s}
.ppane{display:none;padding-top:4px;border-top:1px solid %(line)s;margin-top:-1px}
%(ptab_rules)s
/* -- inline judge citations: highlight the quoted span, note in the margin -- */
.cmark{background:color-mix(in srgb,var(--rc) 17%%,transparent);color:inherit;
 border-bottom:2px solid var(--rc);border-radius:2px;padding:1px 0;
 position:relative;cursor:help}
.cmark:target{background:color-mix(in srgb,var(--rc) 34%%,transparent)}
.cnote{display:block;font-size:12px;line-height:1.5;color:%(muted)s;
 border-left:2px solid var(--rc);padding:3px 0 3px 8px;margin:5px 0 3px}
.cwho{display:block;font-size:10.5px;text-transform:uppercase;
 letter-spacing:.05em;color:var(--rc);font-weight:700}
/* count of judge citations in a turn, shown on its header */
.cflag{font-size:10.5px;font-weight:700;color:%(card)s;background:%(bad)s;
 border-radius:9px;padding:1px 7px;line-height:1.5}
.mcites h4{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:%(faint)s;margin:14px 0 0;font-weight:600}
/* per-agent tabs: CSS-only via a radio group, so a saved page still works */
.mtabs{margin-top:12px}
.mtabs>input{position:absolute;opacity:0;pointer-events:none}
.mtab{display:inline-block;font-size:13px;font-weight:600;padding:6px 13px;
 margin:0 2px 0 0;color:%(muted)s;cursor:pointer;border:1px solid %(border)s;
 border-radius:6px 6px 0 0;border-bottom-color:transparent;background:%(hair)s}
.mtab:hover{color:%(ink)s}
.mtabs>input:checked+.mtab{background:%(card)s;color:var(--rc);
 box-shadow:inset 0 2px 0 var(--rc)}
.dotflag{display:inline-block;width:6px;height:6px;border-radius:50%%;
 background:%(bad)s;margin-left:6px;vertical-align:middle}
.mpane{display:none;border:1px solid %(border)s;border-radius:0 6px 6px 6px;
 padding:14px 16px;margin-top:-1px;background:%(card)s}
/* nth-of-type pairs each radio with its pane: 1st radio -> 1st pane, etc.
   The rules are generated (see _MTAB_RULES) so a run with more roles than a
   hand-written list anticipated can't end up with a pane nothing can show. */
%(mtab_rules)s
.mpane .facts{margin-top:0;padding-top:0;border-top:none}
.mwhy{margin-top:12px;font-size:13.5px;color:%(muted)s;white-space:pre-wrap;
 line-height:1.65}
.mwhy h4{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:%(faint)s;margin:0 0 5px;font-weight:600}
/* the judge's quotes, each linking to the turn it came from */
.cites{list-style:none;margin:14px 0 0;padding:0;display:flex;
 flex-direction:column;gap:10px}
.cite{font-size:13px;border-left:2px solid %(hair)s;padding-left:11px}
.cref{font-family:ui-monospace,monospace;font-size:11.5px;font-weight:700;
 margin-right:8px;white-space:nowrap}
.cref.bad{color:%(bad)s;cursor:help}
.cdesc{color:%(muted)s}
.cite blockquote{margin:5px 0 0;font-size:13px;color:%(ink)s;
 background:color-mix(in srgb,var(--rc,%(hair)s) 13%%,%(bg)s);
 border-left:2px solid var(--rc,%(border)s);border-radius:0 5px 5px 0;
 padding:7px 10px;white-space:pre-wrap}
.chead2{display:flex;gap:2px;align-items:baseline}
/* -- the thread: one message per turn, each agent on its own side -- */
.timeline{display:flex;flex-direction:column;gap:18px;margin-top:24px}
.row{display:flex}
.row.r{justify-content:flex-end}
/* Messages take most of the column but not all of it, so the unused margin on
   one side is what signals "this is the other agent" at a glance. */
.row>.turn{width:min(88%%,1080px)}
.turn{background:%(card)s;border:1px solid %(border)s;border-radius:10px;
 scroll-margin-top:18px;padding:0 0 2px;
 border-top:3px solid var(--ac,%(border)s)}
/* a citation just jumped here — flash the turn so the target is obvious */
.turn:target{border-color:%(accent)s;box-shadow:0 0 0 3px rgba(29,74,255,.13)}
.turn-head{display:flex;align-items:baseline;gap:10px;font-size:15px;
 padding:11px 16px 0}
.turn-head .who{font-size:17px;font-weight:700;letter-spacing:-.01em}
.turn-head .to{font-size:14px;color:%(faint)s}
.turn-meta{margin-left:auto;font-size:13px;color:%(faint)s;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.turn-meta .dot{margin:0 5px}
.turn-meta .tm-cost{color:%(muted)s}
.turn-err{margin:9px 16px 0;padding:9px 12px;background:%(bg_err)s;
 border:1px solid %(bad)s;border-radius:6px;color:%(bad)s;font-size:14px;
 white-space:pre-wrap}
.pill{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;font-weight:750;white-space:nowrap}
.pill.ok{color:%(ok)s} .pill.bad{color:%(bad)s}
/* -- the message itself: the most prominent thing on the page -- */
.msg-body{margin:10px 16px 14px;padding:14px 16px;background:%(bg_obs)s;
 border:1px solid %(border)s;border-radius:8px;font-size:16px;line-height:1.6;
 white-space:pre-wrap;word-wrap:break-word}
.msg-body.none{color:%(faint)s;font-style:italic;font-size:14px}
.emitted{margin:0 16px 12px}
.emitted>summary{cursor:pointer;list-style:none;font-size:11px;font-weight:700;
 text-transform:uppercase;letter-spacing:.04em;color:%(bad)s;padding:2px 0}
.emitted>summary::-webkit-details-marker{display:none}
.emitted>summary::before{content:'▸ '}
.emitted[open]>summary::before{content:'▾ '}
.emitted .msg-body{margin:6px 0 0;background:%(bg_err)s}
/* -- the trace: how the agent got there, folded by default -- */
.trace{margin:9px 16px 0;border-top:1px dashed %(hair)s;
 border-bottom:1px dashed %(hair)s}
.trace>summary{cursor:pointer;list-style:none;display:flex;gap:9px;
 align-items:baseline;padding:7px 0;font-size:13px;color:%(faint)s}
.trace>summary::-webkit-details-marker{display:none}
.trace>summary::before{content:'▸';font-size:10px}
.trace[open]>summary::before{content:'▾'}
.trace .tr-lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
 font-weight:700}
.trace .tr-sum{color:%(muted)s}
.trace .tr-err{color:%(bad)s}
.trace-body{padding:4px 0 12px}
/* -- acts: flat tinted rows, uniform 1px border, no colored left bevel -- */
.act{display:flex;gap:14px;padding:9px 13px;margin:7px 0;font-size:15px;border-radius:6px;border:1px solid %(border)s;background:%(card)s}
.act .kind{flex:0 0 50px;text-align:right;font-family:ui-monospace,monospace;font-size:12.5px;font-weight:600;padding-top:2px;user-select:none}
.act .body{flex:1;min-width:0;white-space:pre-wrap;word-wrap:break-word}
.act.reason{background:%(bg_reason)s}
.act.reason .kind{color:%(lbl_reason)s} .act.reason .body{color:%(muted)s;font-style:italic}
.act.tool{background:%(bg_tool)s}
.act.tool .kind{color:%(lbl_tool)s}
.act.tool .body{font-family:ui-monospace,monospace;font-size:14px}
.act.tool .args{color:%(muted)s;word-break:break-word}
.act.obs{background:%(bg_obs)s}
.act.obs .kind{color:%(faint)s} .act.obs .body{color:#3a3a36;font-size:14px}
.act.err{background:%(bg_err)s}
.act.err .kind{color:%(bad)s} .act.err .body{color:%(bad)s;font-size:14px}
.act.note{background:%(card)s}
.act.note .kind{color:%(faint)s} .act.note .body{color:%(ink)s}
.act.sys{background:%(hair)s}
.act.sys .kind{color:%(warn)s} .act.sys .body{color:%(muted)s;font-size:14px}
/* full text, never clipped: long bodies get their own scroll box so one huge
   tool result can't push the rest of the turn off screen */
.act .txt{white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.act .txt.none{color:%(faint)s;font-style:italic}
.act .txt.tall,.argv.tall,.argsraw.tall,.kvv.tall,.in-body.tall,.reply.tall,
.sub-instr.tall{max-height:26em;overflow:auto;background:rgba(0,0,0,.02);
 border:1px solid %(hair)s;border-radius:4px;padding:7px 9px}
/* -- one step's acts; its envelope is a hover title, not a printed row -- */
.step{margin:0}
.smeta{margin:2px 0 6px}
.smeta>summary{cursor:pointer;list-style:none;font-size:11px;color:%(faint)s;
 font-family:ui-monospace,monospace;padding:1px 0}
.smeta>summary::-webkit-details-marker{display:none}
.smeta>summary::before{content:'▸ '}
.smeta[open]>summary::before{content:'▾ '}
/* -- tool call: name + every argument, in full -- */
.tname{display:flex;align-items:baseline;gap:9px}
.tcid{font-size:11px;color:%(faint)s;font-family:ui-monospace,monospace}
.argsgrid{margin-top:6px;display:flex;flex-direction:column;gap:4px}
.argrow{display:flex;gap:10px;align-items:baseline;font-size:14px}
.argk{flex:0 0 auto;min-width:74px;color:%(lbl_tool)s;font-weight:600;
 font-family:ui-monospace,monospace;font-size:12.5px}
.argv{flex:1;min-width:0;font-family:ui-monospace,monospace;color:%(ink)s;
 white-space:pre-wrap;overflow-wrap:anywhere}
.argv.block{display:block;width:100%%;margin-top:2px}
.argrow:has(.argv.block){flex-direction:column;gap:1px}
.argsraw{margin-top:6px;font-family:ui-monospace,monospace;font-size:13.5px;
 white-space:pre-wrap;overflow-wrap:anywhere}
/* -- images the run captured (screenshots, tool image results) -- */
.shot{margin:8px 0 2px;max-width:760px}
.shot img{display:block;max-width:100%%;height:auto;border:1px solid %(border)s;
 border-radius:5px;background:%(card)s}
.shot figcaption{font-size:11.5px;color:%(faint)s;margin-top:4px;
 font-family:ui-monospace,monospace;overflow-wrap:anywhere}
/* -- inbound the agent received that isn't already the message above it
      (the opening assignment, or a notice the relay injected) -- */
.inbound{margin:11px 16px 0;padding:10px 13px;background:%(bg)s;
 border-left:3px solid %(line)s;border-radius:0 6px 6px 0}
.in-lbl{display:block;font-size:10.5px;text-transform:uppercase;
 letter-spacing:.06em;color:%(faint)s;font-weight:700;margin-bottom:5px}
.in-body{font-size:14.5px;white-space:pre-wrap;word-wrap:break-word;
 color:%(muted)s;line-height:1.6}
/* the peer's message is directly above; only the channel envelope is new */
.envelope{margin:9px 16px 0;font-size:11.5px;color:%(faint)s;
 text-transform:uppercase;letter-spacing:.05em;cursor:help}
.envelope code{font-size:12px;text-transform:none;letter-spacing:normal;
 color:%(muted)s;white-space:pre-wrap}
/* -- record tab: raw per-role metadata, the run log, the files -- */
.records{max-width:1400px;margin-top:20px;display:flex;flex-direction:column;
 gap:8px}
.rrec{background:%(card)s;border:1px solid %(border)s;border-radius:8px}
.rrec>summary{padding:11px 15px;cursor:pointer;list-style:none;font-size:15px}
.rrec>summary::-webkit-details-marker{display:none}
.rrec>summary::before{content:'▸';color:%(faint)s;margin-right:9px;font-size:11px}
.rrec[open]>summary::before{content:'▾'}
.rrec[open]>summary{border-bottom:1px solid %(line)s}
.rrec-body{padding:12px 15px 15px}
.rrec-body h5{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
 color:%(faint)s;margin:14px 0 6px;font-weight:600}
.rrec-body h5:first-child{margin-top:0}
.kvs{display:flex;flex-direction:column;gap:3px}
.kvs.tight{margin:5px 0 2px;padding:7px 9px;background:%(bg)s;
 border:1px solid %(hair)s;border-radius:5px}
.kv{display:flex;gap:10px;align-items:baseline;font-size:13.5px}
.kvk{flex:0 0 auto;min-width:132px;color:%(faint)s;
 font-family:ui-monospace,monospace;font-size:12.5px}
.kvv{flex:1;min-width:0;font-family:ui-monospace,monospace;color:%(muted)s;
 white-space:pre-wrap;overflow-wrap:anywhere}
.kvv.block{display:block;width:100%%;margin-top:2px}
.kv:has(.kvv.block){flex-direction:column;gap:1px}
table.evt{font-size:13px;margin-top:0}
table.evt th,table.evt td{padding:7px 10px}
table.evt td.evd code{font-size:12px;overflow-wrap:anywhere}
/* -- subagent trajectories, nested -- */
.subagent{border:1px solid %(border)s;border-radius:6px;margin:6px 0;
 background:%(bg)s}
.subagent>summary{padding:8px 12px;cursor:pointer;list-style:none;font-size:14px}
.subagent>summary::-webkit-details-marker{display:none}
.subagent>summary::before{content:'▸';color:%(faint)s;margin-right:8px;font-size:10px}
.subagent[open]>summary::before{content:'▾'}
.sub-body{padding:4px 12px 10px}
.sub-instr{font-size:13.5px;color:%(muted)s;white-space:pre-wrap;margin-bottom:8px}
.pill.warn{color:%(warn)s}
.foot{margin-top:48px;color:%(faint)s;font-size:13px}
/* -- run list: a flat table on the cream page, hairline rows only -- */
table{width:100%%;border-collapse:collapse;font-size:15px;margin-top:16px;background:%(card)s;border:1px solid %(border)s;border-radius:8px;overflow:hidden}
th,td{padding:11px 16px}
th{text-align:left;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:%(faint)s;font-weight:700;border-bottom:1px solid %(border)s}
td{border-bottom:1px solid %(line)s;vertical-align:top}
tbody tr:last-child td{border-bottom:none}
th.num,td.num{text-align:right}
td.num{font-variant-numeric:tabular-nums}
td.nowrap{white-space:nowrap;font-variant-numeric:tabular-nums}
/* the run slug is one long unbroken path — let it wrap anywhere so it can't force overflow */
td.run{word-break:break-all}
tbody tr:hover td{background:%(hair)s}
""" % {**_C, "mtab_rules": _MTAB_RULES, "ptab_rules": _PTAB_RULES}


def _page(title: str, body: str, *, wide: bool = False) -> str:
    wrap = "wrap wide" if wide else "wrap"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_esc(title)}</title><style>{_STYLE}</style></head>'
            f'<body><div class="{wrap}">{body}</div></body></html>')


def _page_tabs(rv: RunView, colors: dict[str, str]) -> str:
    """Transcript / judge / record as peer tabs.

    The judge's verdict is a different way of reading the same run, not a
    preamble to it, so it sits beside the transcript rather than stacked above.
    The record tab holds what the run logged around the conversation — per-role
    trajectory metadata, the orchestrator's event log, and links to the raw
    files. Radio-driven and CSS-only — no JS, and a saved page still switches.

    Tabs are built from whichever panes exist, so a run with no judge scores
    still gets its record tab (rather than the pane silently disappearing, as a
    fixed two-tab layout would do).
    """
    panes: list[tuple[str, str, str]] = [("transcript", "", _timeline(rv, colors))]

    judge = _metrics_panel(rv, colors)
    if judge:
        flag = ("<span class=dotflag></span>"
                if _metrics_flagged(rv.metric_results()) else "")
        panes.append(("judge", flag, judge))

    records = _records_panel(rv, colors)
    if records:
        panes.append(("record", "", records))

    if len(panes) == 1:
        return panes[0][2]

    tabs, bodies = [], []
    for i, (label, flag, body) in enumerate(panes):
        tid = f"pv-{i}"
        tabs.append(
            f'<input type="radio" name="run-view" id="{tid}"'
            f'{" checked" if i == 0 else ""}>'
            f'<label class="ptab" for="{tid}">{_esc(label)}{flag}</label>'
        )
        bodies.append(f'<div class="ppane">{body}</div>')
    return f'<div class="ptabs">{"".join(tabs)}{"".join(bodies)}</div>'


def render_run(rv: RunView, *, back_href: str | None = None) -> str:
    """One run → a full standalone HTML page: transcript and judge as tabs."""
    colors = _role_colors(rv)
    back = f'<a href="{_esc(back_href)}">← all runs</a> · ' if back_href else ""
    role_str = " ↔ ".join(rv.role_order()) or "run"
    body = (
        f'<p class="crumb">{back}a2-bench</p>'
        f'<h1>{_esc(rv.task_slug or rv.task_path)} '
        f'<span class="muted" style="font-weight:400">· {_esc(role_str)}</span></h1>'
        f'{_header(rv, colors)}'
        f'{_score_card(rv, colors)}'
        f'{_page_tabs(rv, colors)}'
        f'<div class="foot">Rendered from <code>{_esc(rv.run_dir.name)}</code>.</div>'
    )
    return _page(rv.run_id, body)
