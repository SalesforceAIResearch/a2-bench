"""The runs dashboard: one filterable, sortable table of runs.

Columns: date, task, models, outcome, plus the metric columns discovered from
the runs on disk. Per-run detail (transcript, judge metrics, score breakdown)
lives on the run page. Rendered server-side per request — the server re-reads
the run tree each load — with sorting client-side. All metric/facet knowledge
comes from :mod:`a2_run.transcript.metrics`, so adding a metric changes what
this renders without changing this file.
"""

from __future__ import annotations

import html
from typing import Any

from . import metrics as _reg
from .metrics import RunRow, facet_values

# The registries are read through the module (``_reg.METRICS``) rather than
# imported by value, so a metric or facet added to
# :mod:`a2_run.transcript.metrics` is picked up here no matter the import order —
# the extension point stays "edit the registry, touch nothing else".


def _metrics() -> tuple:
    return _reg.METRICS



def _facets() -> tuple:
    return _reg.FACETS


_D = {
    "ink": "#151515",
    "muted": "#55554f",
    "faint": "#83837d",
    "line": "#cfd0c9",
    "hair": "#e2e3db",
    "bg": "#EEEFE9",
    "card": "#ffffff",
    "border": "#BFBFBC",
    "accent": "#1D4AFF",
    # status (fixed roles, always shipped with an icon + label)
    "good": "#0ca30c",
    "warn": "#fab219",
    "critical": "#d03b3b",
    # de-emphasis: "no usable measurement" (crashed / timed out / never ran).
    # Not a status hue — these runs have no score to be good or bad about.
    "nodata": "#8b8a83",
}

# Outcome → (status color, icon). The icon is what carries meaning where the
# color can't (CVD, print, forced-colors) — never color alone.
#
# Only the three graded outcomes wear a status hue: a run that produced a real
# measurement is good / warning / critical. Everything else produced NO
# trustworthy measurement (it crashed, timed out, was cancelled, never ran), and
# those share the de-emphasis gray — distinguished by icon and label, and never
# painted critical-red, which would read as "scored zero" when the truth is
# "never scored". Two categories sharing one hue is the bug this avoids.
_OUTCOME_STYLE = {
    "pass": (_D["good"], "●"),
    "partial": (_D["warn"], "◐"),
    "zero": (_D["critical"], "○"),
    "unscored": (_D["faint"], "·"),
    "failed": (_D["nodata"], "✕"),
    "timeout": (_D["nodata"], "◷"),
    "cancelled": (_D["nodata"], "⊘"),
    "not_executed": (_D["nodata"], "–"),
}

def _esc(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def _outcome_style(name: str) -> tuple[str, str]:
    return _OUTCOME_STYLE.get(name, (_D["faint"], "·"))


def _fmt_ts(ts: str) -> str:
    """``YYYYMMDD_HHMMSS`` → ``YYYY-MM-DD HH:MM``; anything else passes through."""
    if len(ts) == 15 and ts[8] == "_" and ts[:8].isdigit() and ts[9:].isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
    return ts


# =============================================================================
# page building blocks
# =============================================================================


def _card(title: str, body: str, *, sub: str = "", extra: str = "") -> str:
    return (
        f'<section class="card">'
        f'<div class="chead"><h3>{_esc(title)}</h3>{extra}</div>'
        f'{f"<p class=csub>{_esc(sub)}</p>" if sub else ""}'
        f'{body}</section>'
    )


# =============================================================================
# the runs table
# =============================================================================


def _runs_tab(rows: list[RunRow]) -> str:
    cols = [m for m in _metrics() if m.column]
    head = (
        '<tr><th data-sort="s" class="sortable">time</th>'
        '<th data-sort="s" class="sortable">task</th>'
        '<th data-sort="s" class="sortable">models</th>'
        '<th data-sort="s" class="sortable">outcome</th>'
        + "".join(f'<th data-sort="n" class="sortable num" '
                  f'title="{_esc(m.describe)}">{_esc(m.label)}</th>'
                  for m in cols)
        + '<th data-sort="n" class="sortable num">issues</th></tr>'
    )
    body = []
    for r in rows:
        color, icon = _outcome_style(r.facets.get("outcome", "—"))
        outcome = r.facets.get("outcome", "—")
        roles_txt = " → ".join(r.roles)
        n_fail = len(r.failures)
        blocked = any(f["severity"] == "blocked" for f in r.failures)
        # A run with a blocked failure has no trustworthy measurement at all —
        # mark the count so it can't be skimmed past as a minor issue.
        if blocked:
            issues = f'<b style="color:{_D["critical"]}">{n_fail}</b>'
        else:
            issues = str(n_fail) if n_fail else "—"
        cells = "".join(
            f'<td class="num" data-v="'
            f'{r.metric(m.key) if r.metric(m.key) is not None else -1}">'
            f'{_esc(m.fmt(r.metric(m.key)))}</td>' for m in cols
        )
        body.append(
            f'<tr>'
            f'<td class="nowrap muted" data-v="{_esc(r.timestamp)}">'
            f'<a href="{_esc(r.href)}" title="{_esc(r.slug)}">'
            f'{_esc(_fmt_ts(r.timestamp))}</a></td>'
            f'<td data-v="{_esc(r.task)}">{_esc(r.task)}'
            f'<div class="sub">{_esc(roles_txt)}</div></td>'
            f'<td class="models" data-v="{_esc(r.facets.get("pairing", ""))}">'
            f'{_esc(r.facets.get("pairing", "—"))}</td>'
            f'<td data-v="{_esc(outcome)}" class="nowrap">'
            f'<span class="ico" style="color:{color}">{icon}</span> '
            f'{_esc(outcome)}</td>'
            f'{cells}'
            f'<td class="num" data-v="{n_fail}">{issues}</td></tr>'
        )
    return _card(
        "Runs", f'<div class="tscroll">'
                f'<table class="runs" id="runsTable"><thead>{head}</thead>'
                f'<tbody>{"".join(body)}</tbody></table></div>',
        sub=f"{len(rows)} run(s) in this slice. Click a column to sort, a "
            f"timestamp to read that run's timeline (hover it for the full path).",
    )


# =============================================================================
# filter bar
# =============================================================================


def _filter_bar(universe: dict[str, list[str]], active: dict[str, str],
                *, shown: int, total: int) -> str:
    """One row of dimension filters above the runs table.

    Option lists come from ``universe`` — the facet values of the *unfiltered*
    tree — not from the current slice, so narrowing one facet never empties the
    other dropdowns. A filter must always leave a way back out.
    """
    controls = []
    for f in _facets():
        vals = universe.get(f.key, [])
        cur = active.get(f.key, "")
        if len(vals) <= 1 and not cur:
            continue
        opts = "".join(
            f'<option value="{_esc(v)}"{" selected" if v == cur else ""}>'
            f'{_esc(v)}</option>' for v in vals
        )
        controls.append(
            f'<label class="fl" title="{_esc(f.describe)}">'
            f'<span>{_esc(f.label)}</span>'
            f'<select data-facet="{_esc(f.key)}" onchange="applyFilters()">'
            f'<option value=""{" selected" if not cur else ""}>all</option>'
            f'{opts}</select></label>'
        )
    if not controls:
        return ""
    hidden = total - shown
    note = f"{hidden} run(s) hidden by filters" if hidden else "no filters"
    return (
        f'<div class="filters">{"".join(controls)}'
        f'<button class="chip clear" onclick="resetFilters()">reset</button>'
        f'<span class="fcount">{_esc(note)}</span></div>'
    )


# =============================================================================
# page
# =============================================================================

_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 system-ui,-apple-system,'Segoe UI',sans-serif;color:%(ink)s;background:%(bg)s;-webkit-font-smoothing:antialiased}
.wrap{width:100%%;margin:0;padding:32px 40px 120px}
@media(max-width:900px){.wrap{padding:22px 16px 90px}}
a{color:%(accent)s;text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:26px;font-weight:700;letter-spacing:-.02em;margin:0 0 4px}
h3{font-size:15px;font-weight:700;margin:0;letter-spacing:-.01em}
.crumb{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:%(faint)s;margin:0 0 16px}
.muted{color:%(muted)s}.empty{color:%(faint)s;font-size:14px;margin:6px 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em}
.ico{font-style:normal;font-size:11px}
.dot{color:%(faint)s;margin:0 7px}
/* -- filter bar: one row, above everything it scopes -- */
.filters{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;padding:14px 0 16px;border-bottom:1px solid %(line)s;margin-bottom:2px}
.fl{display:flex;flex-direction:column;gap:4px;font-size:12px}
.fl>span{color:%(faint)s;letter-spacing:.04em;text-transform:uppercase;font-size:10.5px}
select{font:inherit;font-size:13px;padding:5px 8px;border:1px solid %(border)s;border-radius:6px;background:%(card)s;color:%(ink)s}
.fcount{margin-left:auto;font-size:13px;color:%(muted)s;font-variant-numeric:tabular-nums}
.chip{font:inherit;font-size:12.5px;padding:5px 10px;border:1px solid %(border)s;border-radius:999px;background:%(card)s;color:%(ink)s;cursor:pointer}
.chip:hover{background:%(hair)s}
.chip.on{background:%(ink)s;color:#fff;border-color:%(ink)s}
.chip.clear{color:%(muted)s}
/* -- cards -- */
.card{background:%(card)s;border:1px solid %(border)s;border-radius:10px;padding:18px 20px;margin:0 0 18px}
.chead{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
.csub{font-size:13px;color:%(muted)s;margin:5px 0 14px;max-width:76ch}
/* -- runs table -- */
table.runs{width:100%%;border-collapse:collapse;font-size:13.5px;margin-top:8px}
table.runs th{text-align:left;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:%(faint)s;font-weight:700;padding:9px 10px;border-bottom:1px solid %(border)s;white-space:nowrap}
table.runs th.sortable{cursor:pointer;user-select:none}
table.runs th.sortable:hover{color:%(ink)s}
table.runs th.num,table.runs td.num{text-align:right}
table.runs td{padding:8px 10px;border-bottom:1px solid %(hair)s;vertical-align:top}
table.runs td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
table.runs td.nowrap{white-space:nowrap}
table.runs td .sub{font-size:11.5px;color:%(faint)s;margin-top:2px}
/* a model pairing is two hyphenated slugs — break between them, never inside a
   name, so the column can't collapse to one character per line */
table.runs td.models{min-width:15ch;overflow-wrap:normal;word-break:keep-all}
/* many metric columns can overflow the card; scroll the table, not the page */
.tscroll{overflow-x:auto}
table.runs tbody tr:hover td{background:%(hair)s}
.foot{margin-top:40px;color:%(faint)s;font-size:12.5px}
""" % _D

# Client-side behavior: facet filtering (re-request) and column sort.
_SCRIPT = """
function qs(){return new URLSearchParams(location.search)}
function go(p){location.search=p.toString()}
function applyFilters(){
  var p=qs();
  document.querySelectorAll('[data-facet]').forEach(function(s){
    if(s.value){p.set('f_'+s.dataset.facet,s.value)}else{p.delete('f_'+s.dataset.facet)}
  });
  go(p);
}
function resetFilters(){
  var p=qs();
  Array.from(p.keys()).filter(function(k){return k.indexOf('f_')===0}).forEach(function(k){p.delete(k)});
  go(p);
}
document.addEventListener('DOMContentLoaded',function(){
  var tbl=document.getElementById('runsTable');
  if(tbl){
    tbl.querySelectorAll('th.sortable').forEach(function(th,i){
      th.onclick=function(){
        var num=th.dataset.sort==='n', body=tbl.tBodies[0];
        var dir=th.dataset.dir==='asc'?-1:1; th.dataset.dir=dir===1?'asc':'desc';
        var rows=Array.from(body.rows);
        rows.sort(function(a,b){
          var x=a.cells[i].dataset.v, y=b.cells[i].dataset.v;
          if(num){return (parseFloat(x)-parseFloat(y))*dir}
          return String(x).localeCompare(String(y))*dir;
        });
        rows.forEach(function(r){body.appendChild(r)});
      };
    });
  }
});
"""


def render_dashboard(
    root: Any,
    rows: list[RunRow],
    *,
    total: int,
    active: dict[str, str],
    universe: dict[str, list[str]] | None = None,
) -> str:
    """The full dashboard page over an already-filtered ``rows`` slice.

    ``total`` is the unfiltered run count (so the filter bar can say how much is
    hidden), ``active`` is the facet→value map currently applied, and
    ``universe`` is the facet values of the whole tree (see
    :func:`facet_universe`) so the dropdowns stay complete under a filter.
    """
    # Rendered server-side so a reload keeps the bar in sync with the slice.
    bar = _filter_bar(
        universe if universe is not None else facet_universe(rows),
        active, shown=len(rows), total=total,
    )

    body = (
        f'<p class="crumb">a2-bench</p>'
        f'<h1>Runs</h1>'
        f'<p class="muted" style="margin:0 0 8px">'
        f'{len(rows)} of {total} run(s) under <code>{_esc(str(root))}</code>.</p>'
        f'{bar}'
        f'{_runs_tab(rows)}'
        f'<div class="foot">Re-read from disk on every load. '
        f'Metrics and facets come from <code>a2_run/transcript/metrics.py</code>.</div>'
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>a2-bench runs — {_esc(str(root))}</title>'
        f'<style>{_STYLE}</style></head><body><div class="wrap">{body}</div>'
        f'<script>{_SCRIPT}</script></body></html>'
    )


def facet_universe(rows: list[RunRow]) -> dict[str, list[str]]:
    """Every facet's full value list, from the UNFILTERED run set."""
    return {f.key: facet_values(rows, f.key) for f in _facets()}
