"""A tiny local HTTP server that renders runs on demand — no files written.

``serve(root)`` starts a stdlib :mod:`http.server` that, on each request,
re-reads the run directories under ``root`` and renders live:

    GET /                     → the runs dashboard (filterable table)
    GET /run/<rel-path>       → one run's merged timeline
    GET /run/<rel>/file/<p>   → a file inside that run dir, served as-is
                                (screenshots the transcript embeds, plus the raw
                                record behind it: each agent's own transcript
                                jsonl, prompts, stderr, task outputs)

Query parameters on ``/`` drive the dashboard, so any view is a shareable URL::

    ?f_task=demo__a2a_hello  one filter per facet (``f_<facet-key>``)

Rendering on demand (rather than baking ``index.html`` / ``view.html`` to disk)
means a run in progress shows its latest state on refresh, and nothing stale is
left behind. Read-only and localhost-bound.
"""

from __future__ import annotations

import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .dashboard import facet_universe, render_dashboard
from .loader import RunView, is_run_dir, iter_runs, load_run
from .metrics import FACETS_BY_KEY, RunRow, build_row, register_offline_metrics
from .viewer import render_run

logger = logging.getLogger(__name__)

_FILTER_PREFIX = "f_"


def _rows(root: Path) -> list[RunRow]:
    """Every run under ``root`` as a dashboard row, newest run first.

    Ordered by the ``YYYYMMDD_HHMMSS`` timestamp in each run's dir name (see
    :attr:`RunView.timestamp`) rather than the path-sorted ``iter_runs`` order,
    which would group by agent/model/task and only order by time within a group.
    """
    views = list(iter_runs(root))
    # Offline metrics (metrics.json) are declared by the `metrics` package, so
    # the registry learns them from the runs on disk before any row is built.
    register_offline_metrics(views)

    out: list[RunRow] = []
    for rv in views:
        try:
            slug = str(rv.run_dir.relative_to(root))
        except ValueError:
            slug = rv.run_dir.name
        # Absolute (leading "/") so a run link always resolves to /run/<slug>
        # regardless of the URL the dashboard is currently served at, and with a
        # TRAILING SLASH so the run page's relative "file/…" asset links (images
        # it embeds, links to the raw record) resolve under the run rather than
        # under /run/.
        out.append(build_row(rv, slug=slug, href="/run/" + quote(slug) + "/"))
    out.sort(key=lambda r: r.timestamp, reverse=True)
    return out


def _parse_query(query: str) -> dict[str, str]:
    """The active facet filters from the query string."""
    q = parse_qs(query, keep_blank_values=False)
    active = {}
    for key, vals in q.items():
        if key.startswith(_FILTER_PREFIX) and vals and vals[0]:
            facet = key[len(_FILTER_PREFIX):]
            if facet in FACETS_BY_KEY:
                active[facet] = vals[0]
    return active


def _apply(rows: list[RunRow], active: dict[str, str]) -> list[RunRow]:
    """Keep rows matching every active facet filter (AND across facets)."""
    return [
        r for r in rows
        if all(r.facets.get(k) == v for k, v in active.items())
    ]


#: Raster image types safe to serve with their real type so the page can embed
#: them. SVG is deliberately absent: it is a document format that can carry
#: script, and run dirs hold agent-written files.
_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/tiff", "image/avif",
})


def _content_type(path: Path) -> str:
    """A safe Content-Type for a run artifact.

    Everything a run dir holds is agent- or task-written, i.e. untrusted input to
    this server, and it is served from the same origin as the viewer. So only
    known raster images get their real type; every other file — jsonl
    transcripts, prompts, stderr, and anything that merely *looks* like HTML or
    SVG — is served as ``text/plain`` and therefore displayed rather than
    executed.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed in _IMAGE_TYPES:
        return guessed
    return "text/plain; charset=utf-8"


def _make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, code: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, run_dir: Path, rel: str) -> bool:
            """Serve one file from inside ``run_dir``. False if it isn't there.

            Containment is checked on the RESOLVED path, so neither ``..`` in the
            URL nor a symlink inside the run dir can reach outside it.
            """
            if not rel:
                return False
            target = (run_dir / rel).resolve()
            base = run_dir.resolve()
            if not target.is_relative_to(base) or not target.is_file():
                return False
            try:
                payload = target.read_bytes()
            except OSError as e:
                logger.warning("transcript: could not read %s: %s", target, e)
                return False
            self.send_response(200)
            self.send_header("Content-Type", _content_type(target))
            self.send_header("Content-Length", str(len(payload)))
            # Run dirs are immutable once finished, but a live run's files do
            # change — so revalidate rather than letting the browser pin a
            # half-written screenshot.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
            return True

        def _dashboard(self, query: str, code: int = 200) -> None:
            all_rows = _rows(root)
            active = _parse_query(query)
            shown = _apply(all_rows, active)
            self._send(render_dashboard(
                root, shown, total=len(all_rows), active=active,
                universe=facet_universe(all_rows),
            ), code=code)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path in ("/", "/index.html"):
                # A single run dir has no children to list — render it directly.
                if is_run_dir(root):
                    self._send(render_run(load_run(root)))
                else:
                    self._dashboard(parsed.query)
                return
            # Files of the single-run root, whose page is served at "/".
            if path.startswith("/file/") and is_run_dir(root):
                if self._send_file(root, path[len("/file/"):].strip("/")):
                    return
                self._send("not found", code=404)
                return
            if path.startswith("/run/"):
                rel = path[len("/run/"):].strip("/")
                # "<run>/file/<p>" asks for an artifact inside that run dir. Split
                # on the LAST marker so a run whose own path contains "file"
                # still resolves.
                asset = ""
                if "/file/" in rel:
                    rel, asset = rel.rsplit("/file/", 1)
                    rel = rel.strip("/")
                run_dir = (root / rel) if rel else root
                if not is_run_dir(run_dir) or not run_dir.resolve().is_relative_to(root.resolve()):
                    self._dashboard(parsed.query, code=404)
                    return
                if asset:
                    if self._send_file(run_dir, asset):
                        return
                    self._send("not found", code=404)
                    return
                # The page's asset links are relative ("file/…"), so it must be
                # served from a trailing-slash URL or they'd resolve one level up.
                if not path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", quote(path) + "/"
                                     + (f"?{parsed.query}" if parsed.query else ""))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                rv: RunView = load_run(run_dir)
                self._send(render_run(rv, back_href="/"))
                return
            self._dashboard(parsed.query, code=404)

        def log_message(self, fmt: str, *args) -> None:
            logger.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(root: str | Path, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve the viewer for ``root`` until interrupted (Ctrl-C)."""
    root = Path(root)
    if is_run_dir(root):
        # A single run dir: serve it directly at /run (root has no children to list).
        logger.info("note: %s is a single run dir", root)
    httpd = ThreadingHTTPServer((host, port), _make_handler(root))
    url = f"http://{host}:{port}"
    print(f"serving {root} at {url}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
