"""Shared bootstrap helpers for agent deployers.

Provides common system-dependency installers (npm, unzip, etc.) that
multiple deployers need when running on minimal container images.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import platform
import re
import shutil
import subprocess
logger = logging.getLogger(__name__)

_NODE_MAJOR = "22"

_IS_WINDOWS = platform.system() == "Windows"

async def _sh(cmd: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run,
        ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _find_windows_node_dir() -> str | None:
    """Locate a portable Node.js install dir on the Windows VM.

    The win10 image ships node unpacked but NOT on PATH (e.g.
    ``C:\\Users\\User\\node-v24.12.0-win-x64\\node.exe``). Glob the
    user profile for any ``node-v*-win-x64`` dir that contains both
    ``node.exe`` and ``npm.cmd``; fall back to common fixed locations.
    """
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, "node-v*-win-x64"),
        os.path.join(home, "node-v*-win-*"),
        r"C:\Program Files\nodejs",
        r"C:\nodejs",
    ]
    for pat in patterns:
        for cand in sorted(glob.glob(pat), reverse=True):
            node_exe = os.path.join(cand, "node.exe")
            npm_cmd = os.path.join(cand, "npm.cmd")
            if os.path.isfile(node_exe) and os.path.isfile(npm_cmd):
                return cand
    return None


async def _ensure_node_npm_windows() -> tuple[str, str]:
    """Windows path for :func:`ensure_node_npm`.

    Node ships unpacked but off PATH on the win VM. Find the dir, prepend
    it (and the npm global bin dir) to ``PATH`` so ``node`` / ``npm`` /
    globally-installed CLIs resolve, then return the binary paths.
    """
    node = shutil.which("node")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if node and npm:
        _configure_npm_prefix_windows()
        return node, shutil.which("npm") or shutil.which("npm.cmd") or npm

    node_dir = _find_windows_node_dir()
    if not node_dir:
        raise RuntimeError(
            "bootstrap: node not found on Windows. Looked on PATH and for "
            r"node-v*-win-x64 under %USERPROFILE% / Program Files."
        )
    if node_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = node_dir + os.pathsep + os.environ.get("PATH", "")
    node = os.path.join(node_dir, "node.exe")
    npm = os.path.join(node_dir, "npm.cmd")
    logger.info("bootstrap: using Windows node at %s", node)
    _configure_npm_prefix_windows()
    return node, npm


def _configure_npm_prefix_windows() -> None:
    """Ensure the npm global-install bin dir is on PATH (Windows).

    With the default prefix, ``npm install -g`` drops shims into
    ``%APPDATA%\\npm``; some node dirs also use the node dir itself.
    Prepend both so freshly-installed global CLIs resolve via
    ``shutil.which`` without a shell restart.
    """
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming",
    )
    npm_bin = os.path.join(appdata, "npm")
    os.makedirs(npm_bin, exist_ok=True)
    if npm_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = npm_bin + os.pathsep + os.environ.get("PATH", "")


async def ensure_node_npm() -> tuple[str, str]:
    """Return (node_path, npm_path), installing Node 20 LTS if needed.

    Uses NodeSource apt repo to get a modern Node.js instead of the
    ancient v12 from Ubuntu 22.04's default repos.  Also configures
    npm's global prefix to ``~/.npm-global`` so no sudo is needed for
    ``npm install -g``.

    On Windows the VM ships node unpacked but off PATH; we locate it and
    fix PATH instead of installing.
    """
    if _IS_WINDOWS:
        return await _ensure_node_npm_windows()

    node = shutil.which("node")
    npm = shutil.which("npm")

    # Check if existing node is new enough (>= 16)
    if node and npm:
        try:
            ver = (await _sh(f"'{node}' --version", timeout=10)).stdout.strip()
            major = int(ver.lstrip("v").split(".")[0])
            if major >= 16:
                await _configure_npm_prefix()
                return node, npm
            logger.info("bootstrap: node %s too old (need >=16), upgrading ...", ver)
        except (ValueError, IndexError):
            pass

    logger.info("bootstrap: installing Node.js %s via NodeSource ...", _NODE_MAJOR)
    proc = await _sh(
        f"curl -fsSL https://deb.nodesource.com/setup_{_NODE_MAJOR}.x | sudo -E bash - "
        f"&& sudo apt-get install -y -qq nodejs 2>&1 | tail -10",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"bootstrap: NodeSource install failed (rc={proc.returncode}): "
            f"{(proc.stderr or '')[:500]}"
        )

    node = shutil.which("node") or "/usr/bin/node"
    npm = shutil.which("npm") or "/usr/bin/npm"
    if not os.path.isfile(node):
        raise RuntimeError("bootstrap: node still not found after install")
    logger.info("bootstrap: node installed at %s", node)

    await _configure_npm_prefix()
    return node, npm


async def _configure_npm_prefix() -> None:
    """Set npm global prefix to ~/.npm-global so installs don't need sudo."""
    home = os.path.expanduser("~")
    npm_global = f"{home}/.npm-global"
    npm_bin = f"{npm_global}/bin"
    os.makedirs(npm_global, exist_ok=True)

    await _sh(f"npm config set prefix '{npm_global}'", timeout=15)

    if npm_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{npm_bin}:{os.environ.get('PATH', '')}"


async def ensure_npm() -> str:
    """Return path to ``npm``, installing Node.js+npm if missing."""
    _, npm = await ensure_node_npm()
    return npm


# =============================================================================
# Pinned npm-distributed CLI install (claude_code + codex share this)
# =============================================================================

_CLI_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def expected_npm_version(npm_spec: str | None) -> str | None:
    """"@pkg/x@2.1.170" → "2.1.170"; None for unpinned specs (accept any)."""
    if not npm_spec:
        return None
    m = _CLI_VERSION_RE.search(npm_spec.rsplit("@", 1)[-1])
    return m.group(1) if m else None


def _find_npm_shim(prefix: str, command: str) -> str | None:
    # Resolve explicitly (not shutil.which) so the freshly-installed copy
    # wins over an older one elsewhere on PATH (e.g. AppData\Roaming\npm).
    for cand in (
        os.path.join(prefix, "bin", command),          # Linux
        os.path.join(prefix, f"{command}.cmd"),        # Windows
        os.path.join(prefix, "bin", f"{command}.cmd"),
    ):
        if os.path.exists(cand):
            return cand
    return None


def probe_cli_version(cli_path: str) -> str | None:
    """Best-effort ``<cli> --version`` → ``X.Y.Z``; ``None`` if unreadable."""
    # stdin=DEVNULL: a fresh .EXE on Windows can hang on Defender scan +
    # TTY check; 60s gives cold-install first-exec headroom.
    try:
        probe = subprocess.run(
            [cli_path, "--version"],
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _CLI_VERSION_RE.search((probe.stdout or "") + (probe.stderr or ""))
    return m.group(1) if m else None


async def npm_install_global(package: str, *, harness: str) -> None:
    """``npm install -g`` *package* under ``~/.local``; bootstrap npm if missing."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        npm = await ensure_npm()
    home = os.path.expanduser("~")
    prefix = os.path.join(home, ".local")
    env = {**os.environ, "npm_config_cache": os.path.join(home, ".npm-a2")}
    proc = await asyncio.to_thread(
        subprocess.run,
        [npm, "install", "-g", "--force", "--prefix", prefix, package],
        capture_output=True, text=True, timeout=900, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{harness}: npm install -g {package} failed "
            f"(rc={proc.returncode}): {(proc.stderr or '')[-800:]}"
        )
    for bin_dir in (prefix, os.path.join(prefix, "bin")):
        if bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    logger.info("%s: npm installed %s", harness, package)


async def ensure_pinned_npm_cli(
    *, command: str, npm_spec: str, default_package: str, harness: str,
) -> tuple[str, str | None]:
    """Verify-and-correct install of a pinned npm CLI → (path, version).

    Reuses a pre-installed *command* only when --version matches the pin;
    otherwise installs under ~/.local, prepended on PATH (a spec bump takes
    effect next run). Post-install mismatch is a hard error — no silent
    version drift.
    """
    cli_path = shutil.which(command)
    expected = expected_npm_version(npm_spec)
    installed = (
        await asyncio.to_thread(probe_cli_version, cli_path)
        if cli_path else None
    )
    if cli_path and not (expected and installed != expected):
        logger.info(
            "%s: reusing installed %s %s (version %s)",
            harness, command, cli_path, installed or "unknown",
        )
        return cli_path, installed

    if cli_path:
        logger.info(
            "%s: installed version %s != pinned %s — installing pinned version",
            harness, installed, expected,
        )
    else:
        logger.info("%s: %r not on PATH, installing via npm …", harness, command)
    await npm_install_global(npm_spec or default_package, harness=harness)
    prefix = os.path.join(os.path.expanduser("~"), ".local")
    cli_path = _find_npm_shim(prefix, command) or shutil.which(command)
    if not cli_path:
        raise RuntimeError(
            f"{harness}: {command!r} still not found after "
            f"npm install -g {npm_spec or default_package}"
        )
    installed = await asyncio.to_thread(probe_cli_version, cli_path)
    if expected and installed != expected:
        raise RuntimeError(
            f"{harness}: post-install version {installed!r} still != pinned "
            f"{expected!r} (path {cli_path})"
        )
    logger.info(
        "%s: %s CLI ok — version %s at %s",
        harness, command, installed or "unknown", cli_path,
    )
    return cli_path, installed

