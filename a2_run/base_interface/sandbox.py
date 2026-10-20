"""SandboxHandle — the sandbox container the framework hosts a task in.

``SandboxHandle`` IS:

* The identity of a live sandbox (id = container name, OS).
* The framework-level path conventions baked into the image
  (work_dir_base / task_data_root / node / python / mcp_server_dir).
* The I/O surface other a2-bench code uses to act on that sandbox
  (``run_command`` / ``write_file`` / ``read_file`` / ``exists`` /
  ``mkdir`` / ``rm`` / ``list_dir`` / ``download_to_local``).

The wire implementation is ``docker exec`` / ``docker cp`` against the
container named by :attr:`SandboxHandle.id`.

Agent-specific binary locations (e.g. the claude CLI) are NOT in here.
Each deployer discovers its own binary at install time (``which claude``).
"""
from __future__ import annotations

import abc
import asyncio
import logging
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal


OS = Literal["linux", "windows"]
ReleaseMode = Literal["delete", "stop", "keep"]


logger = logging.getLogger(__name__)


# ============================================================================
# Spec — pre-provision request shape
# ============================================================================

@dataclass(frozen=True)
class SandboxSpec:
    """Pre-provision: what kind of sandbox the task wants.

    Built from the task's ``vm`` block (``task.yaml``). Handed to ``Provider.acquire``.
    """

    snapshot: str
    os: OS = "linux"
    machine_type: str | None = None
    vcpus: int | None = None
    memory_gb: int | None = None
    gpu: str | None = None
    task_id: str = ""
    harness: str = ""
    model_tag: str = ""

    # ─── network egress control (opt-in; default = unrestricted) ───
    egress_mode: str = "open"
    """``"open"`` (default) leaves the container's network untouched — the
    historical behaviour. ``"restricted"`` asks the provider to lock egress
    down to loopback + DNS + ``egress_allow`` (see the docker provider and
    ``docker/ubuntu-base/egress_init.sh``)."""

    egress_allow: tuple[str, ...] = ()
    """Domains the container may reach when ``egress_mode == "restricted"``
    (e.g. the agent's own model-backend hosts). Ignored when open."""


# ============================================================================
# SandboxHandle — data + API
# ============================================================================

@dataclass
class SandboxHandle:
    """A live sandbox container + the API to act on it.

    Returned by ``Provider.acquire``. Carried through the whole
    framework. Deployer code reads identity / path fields directly, and
    calls the I/O methods to make things happen on the sandbox.
    """

    # ─── identity ───
    id: str
    endpoint: str
    os: OS

    # ─── baked path conventions ───
    work_dir_base: str
    """Per-run scratch root. e.g. ``/opt/a2a/sessions`` (under the control
    plane, out of the agent's home). Deployers compose
    ``<work_dir_base>/<agent>/<run_id>/`` as their work dir."""

    task_data_root: str
    """Sandbox-side root for staged task data (input/, reference/,
    output/, ...). An opaque, flat mount: staging writes the generic
    ``<task_data_root>/<subdir>`` (no domain/task/variant, which would leak
    the task identity + hidden variant to an agent listing its data mount)."""

    node: str
    """Absolute path to the ``node`` binary."""

    python: str
    """Absolute path to the ``python`` binary."""

    mcp_server_dir: str
    """Where an MCP server may be installed on this image."""

    hub_port: int = 8765
    """In-container port of the a2a hub HTTP server. The host-side URL of
    the docker-published mapping lives in ``metadata['hub_url']``."""

    # ─── provider extras (rarely-used, free-form) ───
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_linux(self) -> bool:
        return self.os == "linux"

    @property
    def container(self) -> str:
        """Docker container name this handle drives."""
        return self.metadata.get("container_name") or self.id

    # ========================================================================
    # I/O — async API, thin wrappers around the sync wire impl below
    # ========================================================================

    async def run_command(
        self, command: str, *, timeout: float = 60,
    ) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(_run_remote_sync, self, command, timeout)

    async def write_file(self, path: str, content: str | bytes) -> None:
        if isinstance(content, bytes):
            await asyncio.to_thread(_write_binary_sync, self, path, content)
        else:
            await asyncio.to_thread(_write_binary_sync, self, path, content.encode("utf-8"))

    async def read_file(self, path: str) -> bytes:
        """Read ``path`` as bytes. Raises :class:`FileNotFoundError` if the
        path does not exist; :class:`RuntimeError` on a transport-level
        failure. Empty file → ``b""``."""
        return await asyncio.to_thread(_read_bytes_sync, self, path)

    async def read_text(self, path: str) -> str:
        """UTF-8 decode of :meth:`read_file`. Raises the same exceptions."""
        return (await self.read_file(path)).decode("utf-8", errors="replace")

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(_exists_sync, self, path)

    async def mkdir(self, path: str) -> None:
        await asyncio.to_thread(_mkdir_sync, self, path)

    async def make_user_writable(self, path: str) -> None:
        """Give the sandbox's normal user ownership and write access to a tree.

        Task-data backends may create files as a transport user (for example,
        ``docker cp`` creates root-owned files). This normalizes only the
        sandbox copy; it never changes the source artifact on the host.
        """
        await asyncio.to_thread(_make_user_writable_sync, self, path)

    async def rm(self, paths: Iterable[str]) -> None:
        await asyncio.to_thread(_rm_sync, self, list(paths))

    async def list_dir(self, path: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(_list_dir_sync, self, path)

    async def download_to_local(
        self, remote_path: str, local_path: str, *, timeout: float = 60,
    ) -> bool:
        """Download a sandbox-side file to HOST disk. Returns True on
        success. Used by gather."""
        return await asyncio.to_thread(
            _download_to_local_sync, self, remote_path, local_path, timeout,
        )


# ============================================================================
# Provider ABC
# ============================================================================

class Provider(abc.ABC):
    """ABC for sandbox lifecycle. The framework consumes Provider; nothing
    else."""

    @abc.abstractmethod
    async def acquire(self, spec: SandboxSpec) -> SandboxHandle: ...

    @abc.abstractmethod
    async def release(
        self, sandbox: SandboxHandle, *, mode: ReleaseMode = "delete",
    ) -> None: ...


# ============================================================================
# Wire impl (private — sync; SandboxHandle's async methods to_thread these)
# ============================================================================


def _docker(
    args: list[str],
    *,
    timeout: float,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Run a ``docker ...`` CLI command, capturing bytes."""
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["docker", *args], returncode=-1, stdout=b"",
            stderr=f"docker command timed out after {timeout}s".encode(),
        )
    except OSError as e:
        return subprocess.CompletedProcess(
            args=["docker", *args], returncode=-1, stdout=b"",
            stderr=str(e).encode(),
        )


def _run_remote_sync(
    sandbox: SandboxHandle, command: str, timeout: float,
) -> subprocess.CompletedProcess:
    r = _docker(
        ["exec", sandbox.container, "bash", "-lc", command],
        timeout=timeout,
    )
    return subprocess.CompletedProcess(
        args=command,
        returncode=r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", errors="replace"),
        stderr=(r.stderr or b"").decode("utf-8", errors="replace"),
    )


def _write_binary_sync(sandbox: SandboxHandle, remote_path: str, content: bytes) -> None:
    """Stream ``content`` into the container over ``docker exec -i`` stdin,
    so the file is created by the container's default user (a host-side
    ``docker cp`` would land it with the docker daemon's uid)."""
    q = shlex.quote(remote_path)
    parent = shlex.quote(remote_path.rsplit("/", 1)[0]) if "/" in remote_path else "."
    r = _docker(
        ["exec", "-i", sandbox.container, "bash", "-c",
         f"mkdir -p {parent} && cat > {q}"],
        timeout=300,
        input_bytes=content,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"write_file({remote_path}) failed rc={r.returncode}: "
            f"{(r.stderr or b'').decode(errors='replace')[:300]}"
        )


def _read_bytes_sync(sandbox: SandboxHandle, remote_path: str) -> bytes:
    # Pre-check existence so callers get a clean FileNotFoundError instead of
    # a vague transport failure.
    if not _exists_sync(sandbox, remote_path):
        raise FileNotFoundError(remote_path)
    fd, tmp = tempfile.mkstemp(prefix="a2_dl_")
    os.close(fd)
    try:
        ok = _download_to_local_sync(sandbox, remote_path, tmp, 60)
        if not ok:
            raise RuntimeError(
                f"sandbox read_file: transport failure for {remote_path}"
            )
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _exists_sync(sandbox: SandboxHandle, path: str) -> bool:
    r = _run_remote_sync(sandbox, f"test -e {shlex.quote(path)}", timeout=15)
    return r.returncode == 0


def _mkdir_sync(sandbox: SandboxHandle, path: str) -> None:
    result = _run_remote_sync(sandbox, f"mkdir -p {shlex.quote(path)}", timeout=30)
    if result.returncode != 0:
        raise RuntimeError(
            f"mkdir {path} failed rc={result.returncode}: "
            f"{(result.stderr or '')[:200]}"
        )


def _make_user_writable_sync(sandbox: SandboxHandle, path: str) -> None:
    """Normalize a staged tree for the container's configured user."""
    if not sandbox.is_linux:
        raise NotImplementedError(
            "make_user_writable is currently implemented for Linux sandboxes"
        )

    identity = _docker(
        ["exec", sandbox.container, "sh", "-c", "printf '%s:%s' \"$(id -u)\" \"$(id -g)\""],
        timeout=15,
    )
    owner = (identity.stdout or b"").decode(errors="replace").strip()
    owner_parts = owner.split(":")
    if (
        identity.returncode != 0
        or len(owner_parts) != 2
        or not all(part.isdigit() for part in owner_parts)
    ):
        raise RuntimeError(
            f"could not resolve sandbox user for {sandbox.container}: "
            f"{(identity.stderr or b'').decode(errors='replace')[:200]}"
        )

    q = shlex.quote(path)
    result = _docker(
        ["exec", "-u", "root", sandbox.container, "sh", "-c",
         f"chown -R {owner} {q} && chmod -R u+rwX {q}"],
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"make_user_writable({path}) failed rc={result.returncode}: "
            f"{(result.stderr or b'').decode(errors='replace')[:300]}"
        )


def _rm_sync(sandbox: SandboxHandle, paths: list[str]) -> None:
    if not paths:
        return
    quoted = " ".join(shlex.quote(p) for p in paths)
    _run_remote_sync(sandbox, f"rm -rf {quoted}", timeout=30)


def _list_dir_sync(sandbox: SandboxHandle, remote_dir: str) -> list[dict[str, Any]]:
    """Recursive walk: emits {"relpath", "is_dir", "size"} per entry.
    Returns [] on missing dir or transport error."""
    safe = remote_dir.replace("'", "'\\''")
    cmd = (
        f"if [ ! -d '{safe}' ]; then echo '[]'; exit 0; fi; "
        f"cd '{safe}' && find . -mindepth 1 -printf '%P\\t%y\\t%s\\n'"
    )
    result = _run_remote_sync(sandbox, cmd, timeout=30)
    if result.returncode != 0:
        return []
    out = (result.stdout or "").strip()
    if not out or out == "[]":
        return []
    entries: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rel, kind, size = parts[0], parts[1], parts[2]
        try:
            size_int = int(size)
        except ValueError:
            size_int = 0
        entries.append({"relpath": rel, "is_dir": kind == "d", "size": size_int})
    return entries


def _download_to_local_sync(
    sandbox: SandboxHandle, remote_path: str, local_path: str, timeout: float,
) -> bool:
    """``docker cp`` the file out of the container onto host disk."""
    r = _docker(
        ["cp", f"{sandbox.container}:{remote_path}", local_path],
        timeout=timeout,
    )
    if r.returncode != 0:
        logger.debug(
            "download_to_local failed for %s: %s",
            remote_path, (r.stderr or b"").decode(errors="replace")[:200],
        )
        return False
    return True
