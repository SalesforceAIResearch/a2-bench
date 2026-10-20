"""DockerProvider — ephemeral local Docker containers via ``docker run``.

Each ``acquire`` launches a fresh container from the configured image,
publishes the image's a2a hub port to a random host port, waits until
the hub answers ``GET /status``, and returns a SandboxHandle whose
``id`` is the container name and whose ``metadata['hub_url']`` is the
host-side URL of the hub. All subsequent sandbox I/O goes through
``docker exec`` / ``docker cp`` (see ``base_interface/sandbox.py``);
conversation-control state is watched over the hub port.

If the image tag is missing locally and the Image entry declares a
``dockerfile_dir``, the provider builds it first (one-time cost).

Container naming follows a ``<prefix>-<task-slug>-<hash8>`` scheme for
log greppability.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...base_interface import SandboxSpec, Provider, ReleaseMode, SandboxHandle

logger = logging.getLogger(__name__)

_READY_TIMEOUT = 120
_READY_POLL_INTERVAL = 2

_BUILD_TIMEOUT_S = 1800          # first build pulls ubuntu + apt + npm
_CONTAINER_PREFIX = "a2a"        # container name prefix (not configurable)

# Serialize auto-builds: two concurrent acquires of a missing image must
# not both run `docker build`.
_BUILD_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class DockerProviderConfig:
    """Docker provider config (the per-snapshot ``docker:`` block).

    image       Image NAME (registry key, e.g. "ubuntu-base"). The container
                ref to boot + sandbox paths are read from that Image's
                registry entry.
    shm_size    Shared memory size (default: 512m).
    cpus        CPU limit per container. 0 (default) = use the task card's
                ``vm.vcpus`` if set, else no limit.
    memory      Memory limit per container (e.g. "8g"). "" (default) = use
                the task card's ``vm.memory_gb`` if set, else no limit.
    """

    image: str = "ubuntu-base"
    shm_size: str = "512m"
    cpus: float = 0
    memory: str = ""


def _build_provider_config(raw: dict[str, Any]) -> DockerProviderConfig:
    return DockerProviderConfig(
        image=str(raw.get("image") or "ubuntu-base"),
        shm_size=str(raw.get("shm_size") or "512m"),
        cpus=float(raw.get("cpus") or 0),
        memory=str(raw.get("memory") or ""),
    )


def _generate_container_name(
    prefix: str,
    *,
    snapshot: str,
    task_id: str = "",
    harness: str = "",
    model_tag: str = "",
) -> str:
    if task_id:
        body = re.sub(r"[^a-z0-9]", "-", task_id.lower()).strip("-")[:40]
    else:
        body = re.sub(r"[^a-z0-9]", "-", snapshot.lower()).strip("-")[:30]
    seed = f"{prefix}:{task_id}:{harness}:{model_tag}:{snapshot}:{time.time()}:{random.random()}"
    h = hashlib.sha256(seed.encode()).hexdigest()[:8]
    return f"{prefix}-{body}-{h}"


async def _run_docker(*args: str, timeout: float | None = None) -> tuple[int, str, str]:
    cmd = ["docker", *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"docker {' '.join(args[:2])} timed out after {timeout}s"
    return (
        proc.returncode or 0,
        stdout_b.decode(errors="replace").strip(),
        stderr_b.decode(errors="replace").strip(),
    )


def _repo_root() -> Path:
    """benchmark/ — three levels up from this file's package dir."""
    return Path(__file__).resolve().parents[3]


async def _ensure_image(image) -> None:
    """Auto-build ``image.docker_image`` from its Dockerfile if the tag is
    missing locally. No-op when the tag exists or no dockerfile_dir is set."""
    rc, _, _ = await _run_docker("image", "inspect", image.docker_image, timeout=30)
    if rc == 0:
        return
    if not image.dockerfile_dir:
        raise RuntimeError(
            f"docker image {image.docker_image!r} not found locally and image "
            f"family {image.name!r} declares no dockerfile_dir to build from"
        )
    async with _BUILD_LOCK:
        # Re-check under the lock — another acquire may have just built it.
        rc, _, _ = await _run_docker("image", "inspect", image.docker_image, timeout=30)
        if rc == 0:
            return
        build_dir = _repo_root() / image.dockerfile_dir
        if not (build_dir / "Dockerfile").is_file():
            raise RuntimeError(f"no Dockerfile at {build_dir}")
        logger.info(
            "Building image %s from %s (first run only — this may take a few "
            "minutes)", image.docker_image, build_dir,
        )
        rc, stdout, stderr = await _run_docker(
            "build", "-t", image.docker_image, str(build_dir),
            timeout=_BUILD_TIMEOUT_S,
        )
        if rc != 0:
            raise RuntimeError(
                f"docker build {image.docker_image} failed: {stderr[-800:]}"
            )
        logger.info("Image %s built", image.docker_image)


async def _get_hub_hostport(name: str, hub_port: int) -> str:
    """Host ``ip:port`` that docker published for the container's hub port."""
    rc, stdout, stderr = await _run_docker("port", name, f"{hub_port}/tcp", timeout=30)
    if rc != 0 or not stdout:
        raise RuntimeError(f"docker port {name} {hub_port}/tcp failed: {stderr}")
    # Possibly one line per address family; take the first IPv4-ish line.
    line = stdout.splitlines()[0].strip()
    return line.replace("0.0.0.0", "127.0.0.1").replace("[::]", "127.0.0.1")


async def _apply_egress_lockdown(
    name: str, *, allow: tuple[str, ...],
) -> None:
    """Run the baked egress initializer as root in the container.

    ``egress_init.sh`` reads ``A2_EGRESS_MODE`` / ``A2_EGRESS_ALLOW`` and
    installs the iptables/ipset/dnsmasq allowlist. These are supplied here on
    the initializer's OWN ``docker exec`` env (not on ``docker run``), so they
    are scoped to this one-shot root process and never persist on PID 1 where
    an agent could read them. A non-zero exit is fatal — we must not launch an
    agent whose network posture is not what the experiment intends.
    """
    rc, stdout, stderr = await _run_docker(
        "exec", "-u", "root",
        "-e", "A2_EGRESS_MODE=restricted",
        "-e", f"A2_EGRESS_ALLOW={','.join(allow)}",
        name,
        "bash", "/opt/a2a/egress_init.sh",
        timeout=120,
    )
    if rc != 0:
        raise RuntimeError(
            f"egress lockdown failed in {name} (rc={rc}): "
            f"{(stderr or stdout)[-800:]}"
        )
    logger.info("Egress lockdown applied in %s", name)


def _probe_hub(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/status", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


async def _wait_hub_ready(
    url: str,
    timeout: float = _READY_TIMEOUT,
    poll_interval: float = _READY_POLL_INTERVAL,
) -> bool:
    """Poll the hub's /status endpoint until it answers ok."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_probe_hub, url):
            logger.info("Hub ready at %s", url)
            return True
        await asyncio.sleep(poll_interval)
    logger.error("Hub at %s did not become ready within %ss", url, timeout)
    return False


class DockerProvider(Provider):
    """Provider backed by ``docker run`` / ``docker rm``."""

    def __init__(self, config: DockerProviderConfig | dict[str, Any]):
        if isinstance(config, dict):
            config = _build_provider_config(config)
        self._cfg = config

    @property
    def config(self) -> DockerProviderConfig:
        return self._cfg

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        name = _generate_container_name(
            _CONTAINER_PREFIX,
            snapshot=spec.snapshot,
            task_id=spec.task_id,
            harness=spec.harness,
            model_tag=spec.model_tag,
        )

        from ..images import get as get_image
        image = get_image(self._cfg.image)
        await _ensure_image(image)

        run_args = [
            "run", "-d",
            "--name", name,
            f"--shm-size={self._cfg.shm_size}",
            # Publish the a2a hub to a random localhost port; the host watches
            # conversation-control state through it.
            "-p", f"127.0.0.1:0:{image.hub_port}",
        ]
        # Network egress control (opt-in). "restricted" needs NET_ADMIN to run
        # egress_init.sh's iptables/ipset lockdown. The mode + allowlist are
        # NOT set on `docker run` (they would land on PID 1's environ and be
        # inherited by every `docker exec`, so an agent reading `env` or
        # `/proc/1/environ` would learn it is restricted and see the exact
        # allowlist — an eval tell that also voids a task whose premise is that
        # the egress asymmetry is invisible). Instead they are passed only on
        # the one-shot root `docker exec` that runs the initializer (see
        # `_apply_egress_lockdown`), so they never persist on any
        # agent-readable process. "open" (the default) adds nothing, so an
        # ordinary run is byte-identical to before this feature existed.
        restricted = spec.egress_mode == "restricted"
        if restricted:
            run_args += ["--cap-add=NET_ADMIN"]
        # Resource sizing: provider-config override wins, else the task card's
        # explicit vcpus/memory_gb, else no limit.
        cpus = self._cfg.cpus or (float(spec.vcpus) if spec.vcpus else 0)
        memory = self._cfg.memory or (f"{spec.memory_gb}g" if spec.memory_gb else "")
        if cpus and cpus > 0:
            run_args.extend(["--cpus", str(cpus)])
        if memory:
            run_args.extend(["--memory", memory])
        run_args.append(image.docker_image)

        logger.info(
            "Creating container %s from %s (cpus=%s memory=%s)",
            name, image.docker_image, cpus or "unset", memory or "unset",
        )
        rc, stdout, stderr = await _run_docker(*run_args, timeout=120)
        if rc != 0:
            raise RuntimeError(f"docker run failed for {name}: {stderr}")
        logger.info("Container %s started (id=%s)", name, stdout[:12])

        try:
            hostport = await _get_hub_hostport(name, image.hub_port)
            hub_url = f"http://{hostport}"
            if not await _wait_hub_ready(hub_url):
                raise RuntimeError(f"hub in {name} did not become ready")
            # Apply the egress allowlist AFTER the hub is up (the hub binds
            # loopback and needs no external network). Run as root — agents
            # run as the unprivileged `user`, so the lockdown must be in place
            # before any agent starts. Fail loud: a "restricted" agent that is
            # silently left open would void the experiment.
            if restricted:
                await _apply_egress_lockdown(name, allow=spec.egress_allow)
        except Exception:
            logger.error("Container %s not ready, cleaning up", name)
            await _run_docker("rm", "-f", name, timeout=60)
            raise

        return SandboxHandle(
            id=name,
            endpoint=hub_url,
            os=image.os,
            **image.sandbox_paths(),
            metadata={
                "provider": "docker",
                "container_name": name,
                "container_id": stdout[:12],
                "image": self._cfg.image,
                "snapshot": spec.snapshot,
                "hub_url": hub_url,
            },
        )

    async def release(
        self, sandbox: SandboxHandle, *, mode: ReleaseMode = "delete",
    ) -> None:
        name = sandbox.id
        if mode == "delete":
            logger.info("Removing container %s", name)
            rc, _, stderr = await _run_docker("rm", "-f", name, timeout=60)
            if rc != 0:
                logger.error("Failed to remove container %s: %s", name, stderr)
        elif mode == "stop":
            logger.info("Stopping container %s", name)
            rc, _, stderr = await _run_docker("stop", name, timeout=60)
            if rc != 0:
                logger.error("Failed to stop container %s: %s", name, stderr)
        elif mode == "keep":
            logger.info("Container %s kept alive (mode=keep)", name)
        else:
            raise ValueError(f"unknown release mode: {mode!r}")
