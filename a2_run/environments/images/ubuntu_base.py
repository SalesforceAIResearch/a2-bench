"""``ubuntu-base`` — the minimal Ubuntu 24.04 a2a sandbox image.

Built from ``docker/ubuntu-base/Dockerfile`` (auto-built by the docker
provider when the tag is missing locally). Plain Ubuntu userspace plus
python3 (+pydantic), node/npm with the pinned claude-code, codex, and
cursor-agent CLIs baked under ``/home/user/.local``, the a2a control
plane at ``/opt/a2a`` (hub HTTP server as PID 1 on :8765, a2a tools
stdio MCP server — a2a_send_message + a2a_task_complete), and the egress
initializer ``/opt/a2a/egress_init.sh`` (iptables/ipset/dnsmasq; inert
unless the provider sets ``A2_EGRESS_MODE`` — see the docker provider).
The per-run ``sessions/`` work dirs (work_dir_base) and the ``runtime-src/``
framework tree also live under ``/opt/a2a`` rather than the agent's home,
so ``ls -R ~`` does not surface framework-owned state.
"""
from __future__ import annotations

from . import Image


IMAGE = Image(
    name="ubuntu-base",
    os="linux",

    work_dir_base="/opt/a2a/sessions",
    task_data_root="/media/user/data/work",
    node="/usr/bin/node",
    python="/usr/bin/python3",
    mcp_server_dir="/opt/a2a",

    docker_image="a2-bench-ubuntu:latest",
    dockerfile_dir="docker/ubuntu-base",
    hub_port=8765,
)
