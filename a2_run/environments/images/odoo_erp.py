"""``odoo-erp`` — the a2a sandbox image that carries a live Odoo 19 ERP + PostgreSQL 17.

Built from ``docker/odoo-erp/Dockerfile``, which is ``FROM a2-bench-ubuntu:latest``
so the ENTIRE a2a control plane / harness / egress layer is inherited unchanged:
the hub HTTP server is still PID 1 on :8765, the a2a tools MCP server, the baked
coding CLIs under ``/home/user/.local``, and ``/opt/a2a/egress_init.sh`` are all
exactly as ``ubuntu-base`` ships them. This family ONLY adds a data plane on top:

  * PostgreSQL 17 (PGDG apt) — a cluster owned by a dedicated ``odoo`` service
    account (uid 1001), NOT the agent's ``user`` (uid 1000), so the agent cannot
    read the cluster's config/secrets or connect as a privileged role.
  * Odoo 19 (pinned Odoo nightly .deb) — its python entrypoint + addons, run as
    the ``odoo`` service account against that cluster.
  * A regenerable ``AgentService`` PostgreSQL TEMPLATE database — a FRESH Odoo 19
    DB with the supply-chain workflow's modules installed and admin creds set,
    created at build time (``odoo -d AgentService -i <modules> --without-demo=all
    --stop-after-init``). It is NOT proprietary data: the agent creates all master
    data itself during the workflow, so ``AgentService`` is just "Odoo 19 with the
    right modules". At runtime a task resets its working DB with
    ``createdb <db> TEMPLATE AgentService``.

The image only PROVISIONS these. It does NOT start PostgreSQL or Odoo in its
CMD/ENTRYPOINT — the inherited PID 1 (the a2a hub) must stay PID 1. Booting the
services and resetting the working DB per run is the TASK's job, done from its
``start()`` hook via the scoped, one-shot ``sudo -u odoo /opt/odoo/bin/odoo_ctl``
helper baked by the Dockerfile (see ``docker/odoo-erp/Dockerfile`` and
``tasks/capability/odoo_write_n2/main.py``). Keeping boot/reset in the task means
this image family adds no framework coupling.

Sandbox-side paths are reused VERBATIM from ``ubuntu-base`` (same control plane,
same home, same node/python), so every deployer, executor and staging path behaves
identically — the only difference from ``ubuntu-base`` is the extra data plane and
the ``a2-bench-odoo-erp:latest`` container ref.
"""
from __future__ import annotations

from . import Image


IMAGE = Image(
    name="odoo-erp",
    os="linux",

    # Reused verbatim from ubuntu-base (control plane + home conventions).
    work_dir_base="/opt/a2a/sessions",
    task_data_root="/media/user/data/work",
    node="/usr/bin/node",
    python="/usr/bin/python3",
    mcp_server_dir="/opt/a2a",

    docker_image="a2-bench-odoo-erp:latest",
    dockerfile_dir="docker/odoo-erp",
    hub_port=8765,
)
