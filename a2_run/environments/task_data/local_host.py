"""``task_data_source: local:<host_dir>`` — stage task data from a HOST directory
into the Docker container via ``docker cp``.

For the local Docker provider the host holds the task data (fetched once, e.g.
from Hugging Face) in the canonical layout:

    <host_dir>/<domain>/<task>/<variant>/{input, software, reference}

``stage_input`` copies ``input/`` (+ ``software/``) into the container before the
agent runs. ``stage_reference`` copies ``reference/`` in AFTER the agent, just
before ``evaluate`` — so the reference (the answers) is never inside the
container while the agent runs, exactly like the gs:// path. No encryption: the
answer is hidden by *timing*, not a password, so the host data can be plain
(no reference.7z).

Per-role (a2a) layout
---------------------
An agent-to-agent task has TWO sandboxes (one per role) whose data must
differ — e.g. the oracle sees the secret but the asker must not. So the host
layout gains an optional per-role level:

    <host_dir>/<domain>/<task>/<variant>/<role>/{input, software, reference}

``stage_input``/``stage_reference`` take an optional ``role``: when a
``<variant>/<role>/`` dir exists it is used; otherwise we fall back to the
role-less ``<variant>/`` dir (single-agent tasks, and any role that shares the
common data). Missing ``input/`` is not an error under a role — an a2a role may
ship only ``reference/`` (the asker) or only ``input/`` (the oracle).

Docker-only: it shells out to ``docker cp`` against the running container, whose
name is ``sandbox.id``. ``docker cp`` moves a whole directory tree in one fast
local-disk operation (no per-file HTTP, unlike ``upload_local_file``).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ...base_interface import SandboxHandle, TaskDataSpec
from . import join, shell_q, task_subdir

logger = logging.getLogger(__name__)

_PREFIX = "local:"


def _host_task_dir(
    source: str, task_data: TaskDataSpec, *, role: str | None = None,
) -> str:
    """Host-side data dir for ``local:<root>``.

    ``<root>/<domain>/<task>/<variant>/<role>`` when a per-role dir exists,
    else the role-less ``<root>/<domain>/<task>/<variant>``.
    """
    root = source[len(_PREFIX):]
    base = os.path.join(
        root,
        task_data.domain_name or "",
        task_data.task_name or "",
        task_data.variant_name or "",
    )
    if role:
        role_dir = os.path.join(base, role)
        if os.path.isdir(role_dir):
            return role_dir
    return base


async def _docker_cp(src: str, container: str, dst: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker", "cp", src, f"{container}:{dst}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker cp {src} -> {container}:{dst} failed "
            f"(rc={proc.returncode}): {err.decode(errors='replace')[:300]}"
        )


async def stage_input(
    sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
    role: str | None = None,
) -> dict[str, Any]:
    """Copy input/ (+ software/) from the host into the container; make output/.

    ``role`` selects a per-role host dir when present (a2a). A role with no
    ``input/`` is fine (e.g. the asker ships only a reference); we still make
    ``output/`` and stage ``software/`` if it exists.
    """
    if sandbox.metadata.get("provider") != "docker":
        raise NotImplementedError(
            "task_data_source=local is currently supported only by the direct "
            "Docker provider. QEMU guests need a host-to-container share plus "
            "guest-side CIFS/Samba mounting, which is not implemented yet."
        )
    host = _host_task_dir(source, task_data, role=role)
    base = task_subdir(sandbox, task_data)
    # Always make output/ so the task's start()/agent have somewhere to write.
    await sandbox.mkdir(join(sandbox, base, "output"))
    staged: list[str] = []
    have_input = os.path.isdir(os.path.join(host, "input"))
    if not have_input and not role:
        # Single-agent tasks that require data MUST ship input/. (Per-role a2a
        # tasks are allowed to omit it — see the module docstring.)
        raise RuntimeError(
            f"task_data_source=local: expected input/ at {host!r} on the host, "
            f"not found. Did you fetch the task data? (see the local-docker README)"
        )
    if have_input:
        # Make the target dir first, then ``docker cp <src>/. <dst>`` copies the
        # CONTENTS into it — correct whether or not the dir pre-exists (a baked
        # image may already have <base>/input; plain `cp src dst` would nest it).
        in_dst = join(sandbox, base, "input")
        await sandbox.mkdir(in_dst)
        await _docker_cp(os.path.join(host, "input") + "/.", sandbox.id, in_dst)
        staged.append("input")
    sw = os.path.join(host, "software")
    if os.path.isdir(sw):
        sw_dst = join(sandbox, base, "software")
        await sandbox.mkdir(sw_dst)
        await _docker_cp(sw + "/.", sandbox.id, sw_dst)
        # mirror baked_in_sandbox: make software wrappers/binaries executable.
        await sandbox.run_command(
            f"find {shell_q(sandbox, join(sandbox, base, 'software'))} "
            f"-type f -exec chmod +x {{}} +",
            timeout=60,
        )
        staged.append("software")
    logger.info(
        "local: staged %s for %s%s from %s",
        staged or ["(output only)"], task_data.task_name,
        f" role={role}" if role else "", host,
    )
    return {"staged": staged, "source": "local", "role": role}


async def stage_reference(
    sandbox: SandboxHandle, task_data: TaskDataSpec, *, source: str,
    role: str | None = None,
) -> dict[str, Any]:
    """Copy reference/ from the host into the container — AFTER the agent, just
    before evaluate, so the agent never sees the answers during its run.

    ``role`` selects a per-role host dir when present (a2a) — e.g. only the
    asker's sandbox gets the expected answer.
    """
    if sandbox.metadata.get("provider") != "docker":
        raise NotImplementedError(
            "task_data_source=local is currently supported only by the direct "
            "Docker provider. QEMU guest staging is not implemented yet."
        )
    host = _host_task_dir(source, task_data, role=role)
    ref = os.path.join(host, "reference")
    if not os.path.isdir(ref):
        return {"skipped": True, "reason": "no_reference"}
    base = task_subdir(sandbox, task_data)
    target = join(sandbox, base, "reference")
    await sandbox.rm([target])  # defend against stale reference from a prior run
    await _docker_cp(ref, sandbox.id, target)
    logger.info("local: staged reference %s -> %s:%s", ref, sandbox.id, target)
    return {"staged": ["reference"], "source": "local", "role": role}
