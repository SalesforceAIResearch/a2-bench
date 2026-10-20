# odoo-erp — the a2a sandbox image with a live Odoo 19 ERP + PostgreSQL 17.
#
# ── PREREQUISITE ────────────────────────────────────────────────────────────
# This image is `FROM a2-bench-ubuntu:latest`, so THAT tag must exist locally
# first. a2-bench-ubuntu is the DEFAULT image family (docker/ubuntu-base) and is
# always built by the docker provider on first use, but a COLD build of this
# image needs it built explicitly beforehand. `docker/odoo-erp/build.sh` builds
# ubuntu-base then odoo-erp in the right order — use it for a cold build.
#
# ── WHAT THIS ADDS (data plane only) ────────────────────────────────────────
# Inherited UNCHANGED from ubuntu-base: the a2a hub as PID 1 on :8765, the a2a
# tools MCP server, the baked coding CLIs under /home/user/.local, the egress
# initializer at /opt/a2a/egress_init.sh, and the sandbox path conventions. This
# Dockerfile adds ONLY:
#   * PostgreSQL 17 (PGDG apt) — cluster owned by a dedicated `odoo` service
#     account (uid 1001), NOT the agent's `user` (uid 1000).
#   * Odoo 19 (nightly .deb) — python entrypoint + addons, run as `odoo`.
#   * A regenerable `AgentService` PostgreSQL TEMPLATE database — a fresh Odoo 19
#     DB with the supply-chain workflow's modules installed and admin creds set,
#     ready for `createdb <db> TEMPLATE AgentService` at runtime.
#   * /opt/odoo/bin/odoo_ctl — a root-owned, scoped control helper the TASK's
#     start() hook invokes via `sudo -u odoo` to BOOT + RESET per run (see
#     tasks/capability/odoo_write_n2/main.py). This image never starts a service.
#
# ── CAPABILITY-ASYMMETRY ENFORCEMENT (why uid 1001 ≠ uid 1000) ───────────────
# The agent runs as `user` (uid 1000). Odoo + PostgreSQL run as `odoo` (uid
# 1001). PostgreSQL requires a password on TCP (scram-sha-256) and peer auth on
# the local socket, and the only role passwords live in odoo-owned mode-0600
# files the agent cannot read. So the agent's ONLY path to the DB is the Odoo
# app login staged in its odoo_credentials.json — it CANNOT bypass a read-only
# credential with a direct `psql` (no password; peer maps to a role it isn't).
# The oracle's superuser password is held host-side (task.yaml metadata) and in a
# root/odoo-owned file, never in anything the agent can read.
#
# ── DO NOT change CMD/ENTRYPOINT ─────────────────────────────────────────────
# The inherited PID 1 (the a2a hub) MUST remain PID 1. Services are started at
# runtime by the task's start() hook, never here.

FROM a2-bench-ubuntu:latest

USER root
ENV DEBIAN_FRONTEND=noninteractive

# Pins (kept as ARGs so build.sh / smoke.sh can override for a re-bake).
ARG PG_MAJOR=17
ARG ODOO_DB_TEMPLATE=AgentService
# The workflow's Odoo 19 module set (technical names). The auto-install bridges
# (sale_stock, purchase_stock, stock_account, mrp_account, analytic, mail, …)
# come in as dependencies. Rationale for each is in provision_template.sh.
ARG ODOO_MODULES=base,sale_management,purchase,stock,stock_landed_costs,mrp,mrp_subcontracting,stock_dropshipping,account,hr_expense
# PostgreSQL role passwords. The SUPERUSER password is the one the host-side
# oracle uses (must equal task.yaml metadata_extra.pg_password). The READ-ONLY
# password backs the none_k0 control's read-only DB connection. Both are baked
# into odoo-owned mode-0600 files — never readable by the agent (uid 1000).
ARG ODOO_PG_SUPER_PW=a2erp_oracle_super_pw
ARG ODOO_PG_RO_PW=a2erp_readonly_pw

# ── PostgreSQL 17 (PGDG) + sudo + passlib (for Odoo password hashing) ────────
RUN install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      "postgresql-${PG_MAJOR}" "postgresql-client-${PG_MAJOR}" \
      sudo gnupg python3-passlib \
 && rm -rf /var/lib/apt/lists/*

# ── Dedicated service account (uid 1001, distinct from the agent's uid 1000) ──
# Pre-created so Odoo's own .deb postinst reuses it rather than making its own.
RUN groupadd -g 1001 odoo \
 && useradd -m -u 1001 -g 1001 -s /bin/bash odoo \
 && install -d -o odoo -g odoo -m 0750 /opt/odoo /opt/odoo/bin /opt/odoo/state \
      /opt/odoo/secret /opt/odoo/log /opt/odoo/run /opt/odoo/data

# ── Odoo 19 (nightly .deb repo, pinned to the 19.0 channel) ──────────────────
# policy-rc.d denies service auto-start during dpkg configure (there is no init
# system in a build layer). The .deb's optional postgres-role step is best-effort
# in its postinst; our own cluster is provisioned separately below.
RUN curl -fsSL https://nightly.odoo.com/odoo.key \
      | gpg --dearmor -o /usr/share/keyrings/odoo-archive-keyring.gpg \
 && echo 'deb [signed-by=/usr/share/keyrings/odoo-archive-keyring.gpg] https://nightly.odoo.com/19.0/nightly/deb/ ./' \
      > /etc/apt/sources.list.d/odoo.list \
 && printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && apt-get install -y --no-install-recommends odoo \
 && rm -f /usr/sbin/policy-rc.d \
 && rm -rf /var/lib/apt/lists/*

# ── Baked secrets + control helper + scoped sudoers ──────────────────────────
# Secrets are odoo-owned 0600 — the agent (uid 1000) cannot read them.
RUN printf '%s' "${ODOO_PG_SUPER_PW}" > /opt/odoo/secret/pg_super.pw \
 && printf '%s' "${ODOO_PG_RO_PW}"    > /opt/odoo/secret/pg_ro.pw \
 && chown odoo:odoo /opt/odoo/secret/pg_super.pw /opt/odoo/secret/pg_ro.pw \
 && chmod 0600 /opt/odoo/secret/pg_super.pw /opt/odoo/secret/pg_ro.pw
COPY bin/odoo_ctl /opt/odoo/bin/odoo_ctl
COPY provision_template.sh /opt/odoo/bin/provision_template.sh
COPY sudoers.odoo /etc/sudoers.d/50-odoo
RUN chown root:root /opt/odoo/bin/odoo_ctl /opt/odoo/bin/provision_template.sh \
 && chmod 0755 /opt/odoo/bin/odoo_ctl /opt/odoo/bin/provision_template.sh \
 && chmod 0440 /etc/sudoers.d/50-odoo \
 && visudo -cf /etc/sudoers.d/50-odoo

# ── Initialize the PostgreSQL cluster + build the AgentService template DB ────
# All PG/Odoo work runs AS the odoo service account. See provision_template.sh.
RUN env PG_MAJOR="${PG_MAJOR}" ODOO_DB_TEMPLATE="${ODOO_DB_TEMPLATE}" \
        ODOO_MODULES="${ODOO_MODULES}" \
    bash /opt/odoo/bin/provision_template.sh

# Restore the base image's runtime posture so PID 1 (the a2a hub) runs as the
# unprivileged agent user exactly as ubuntu-base intends. ENTRYPOINT/CMD are
# inherited from ubuntu-base and deliberately NOT overridden.
USER user
WORKDIR /home/user
