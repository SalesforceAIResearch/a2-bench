"""Logical image-family registry.

Each module under this package declares an :class:`Image` describing
one image family the framework knows how to provision against:

* identity (``name``, ``os``)
* sandbox-side path conventions (``work_dir_base`` / ``task_data_root``
  / ``node`` / ``python`` / ``mcp_server_dir``) — splatted into
  :class:`SandboxHandle`
* docker realization (``docker_image`` ref, optional ``dockerfile_dir``
  for provider-side auto-build, and the a2a ``hub_port``)

Adding a new family = add a module here, declare ``IMAGE = Image(...)``,
register in ``_REGISTRY``. SandboxHandle / Providers / deployers never
hard-code an image-family literal — they consult :func:`get` /
:func:`registered`.

Currently one family: ``ubuntu-base`` — the minimal Ubuntu 24.04 a2a
sandbox (see ``docker/ubuntu-base/Dockerfile``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OS = Literal["linux", "windows"]


@dataclass(frozen=True)
class Image:
    """One logical image family — paths + docker realization."""

    # ─── identity ───
    name: str                    # registry key, e.g. "ubuntu-base"
    os: OS

    # ─── sandbox-side paths (splatted into SandboxHandle) ───
    work_dir_base: str
    """Per-run scratch root, e.g. ``/opt/a2a/sessions`` (under the control
    plane, out of the agent's home)."""

    task_data_root: str
    """Where staged task data lives (input/, reference/, output/, ...).
    An opaque, flat mount: ``data_staging`` writes the generic
    ``<task_data_root>/<subdir>`` (no domain/task/variant segments)."""

    node: str
    python: str
    mcp_server_dir: str
    """Where the in-image MCP tooling lives (the a2a tools stdio
    server for a2a runs from here)."""

    # ─── docker realization (consumed by the docker provider) ───
    docker_image: str
    """Container image ref the docker provider boots (and the tag the
    auto-build produces)."""

    dockerfile_dir: str | None = None
    """Repo-relative directory holding this image's Dockerfile. When set,
    the docker provider auto-builds ``docker_image`` from it if the tag
    is missing locally."""

    hub_port: int = 8765
    """In-container port of the a2a hub HTTP server (conversation-control
    state). The provider publishes it to a random host port and records
    the host URL in ``SandboxHandle.metadata['hub_url']``."""

    def sandbox_paths(self) -> dict[str, object]:
        """Field dict for ``SandboxHandle(**image.sandbox_paths(), ...)``."""
        return {
            "work_dir_base":  self.work_dir_base,
            "task_data_root": self.task_data_root,
            "node":           self.node,
            "python":         self.python,
            "mcp_server_dir": self.mcp_server_dir,
            "hub_port":       self.hub_port,
        }


from .ubuntu_base import IMAGE as _UBUNTU_BASE  # noqa: E402
from .odoo_erp import IMAGE as _ODOO_ERP  # noqa: E402


_REGISTRY: dict[str, Image] = {
    _UBUNTU_BASE.name: _UBUNTU_BASE,
    _ODOO_ERP.name: _ODOO_ERP,
}


def get(name: str) -> Image:
    """Look up an image family by name. Raise on unknown."""
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown image family {name!r}; "
            f"registered: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


__all__ = ["Image", "OS", "get"]
