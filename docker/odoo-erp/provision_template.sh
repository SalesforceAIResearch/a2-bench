#!/usr/bin/env bash
# provision_template.sh — build-time provisioning of the odoo-erp data plane.
#
# Runs ONCE, as ROOT, at the end of the Dockerfile build with PG_MAJOR /
# ODOO_DB_TEMPLATE / ODOO_MODULES in the environment. It stands up a
# self-contained PostgreSQL cluster owned by the `odoo` service account (NOT the
# agent's uid 1000) and builds the regenerable `AgentService` Odoo 19 TEMPLATE
# database (the workflow's modules installed, admin + read-only logins set), then
# marks it a locked template and shuts the cluster down so it bakes cleanly into
# the image. Nothing is started in the image's CMD/ENTRYPOINT — the a2a hub stays
# PID 1; the task's start() hook boots the services at runtime via odoo_ctl.
#
# ── CAPABILITY-ASYMMETRY SECURITY MODEL ─────────────────────────────────────
#   * pg_hba: `local ... peer` lets the OS `odoo` user reach PG role `odoo`
#     password-free (Odoo server + odoo_ctl). `host 127.0.0.1/32 scram-sha-256`
#     forces a password on TCP — the agent (uid 1000) has NO PG password and
#     peer maps it to a nonexistent role `user`, so it CANNOT bypass a read-only
#     Odoo app login with a direct psql. The host-side oracle authenticates over
#     TCP with the superuser password (task.yaml metadata_extra.pg_password).
#   * app-level read-only: the `report` login is an internal user with a
#     read-only security group only (no functional write groups), so the none_k0
#     control genuinely cannot mutate the DB even though it can log in and read.
set -euo pipefail

log() { echo "[provision] $*"; }
die() { echo "[provision] FATAL: $*" >&2; exit 1; }

PG_MAJOR="${PG_MAJOR:?PG_MAJOR must be set}"
ODOO_DB_TEMPLATE="${ODOO_DB_TEMPLATE:?ODOO_DB_TEMPLATE must be set}"
ODOO_MODULES="${ODOO_MODULES:?ODOO_MODULES must be set}"

PGBIN="/usr/lib/postgresql/${PG_MAJOR}/bin"
PGDATA="/opt/odoo/data"
SOCKDIR="/var/run/postgresql"
PGLOG="/opt/odoo/log/pg.log"
ODOO_BIN="/usr/bin/odoo"
ODOO_CONF="/opt/odoo/odoo.conf"
ODOO_LOG="/opt/odoo/log/odoo.log"
SETUP_PY="/opt/odoo/bin/setup_users.py"

[[ -x "${PGBIN}/initdb" ]] || die "initdb not found at ${PGBIN} (PG_MAJOR=${PG_MAJOR})"
[[ -x "${ODOO_BIN}" ]] || die "odoo binary not found at ${ODOO_BIN}"

# Convenience: run a command as the odoo service account.
as_odoo() { runuser -u odoo -- "$@"; }
# Run psql as odoo over the local socket (peer auth, no password) with fail-loud.
psql_odoo() { runuser -u odoo -- "${PGBIN}/psql" -v ON_ERROR_STOP=1 "$@"; }

# ── 0) Socket dir (postgresql-common may own it as postgres; we need odoo) ────
install -d -o odoo -g odoo -m 2775 "${SOCKDIR}"

# ── 1) initdb the cluster (owned by odoo) ────────────────────────────────────
log "initdb cluster at ${PGDATA} (PG ${PG_MAJOR})"
as_odoo "${PGBIN}/initdb" \
  -D "${PGDATA}" \
  -U odoo \
  --encoding=UTF8 \
  --locale=C.UTF-8 \
  --auth-local=peer \
  --auth-host=scram-sha-256

# ── 2) postgresql.conf: bind loopback, fixed port, socket dir, scram hashing ──
log "writing postgresql.conf overrides"
as_odoo bash -c "cat >> '${PGDATA}/postgresql.conf'" <<EOF

# ── a2-bench odoo-erp overrides ──
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = '${SOCKDIR}'
password_encryption = scram-sha-256
EOF

# ── 3) pg_hba.conf: peer on the socket, scram (password) on TCP loopback ──────
log "writing pg_hba.conf (peer local / scram TCP)"
as_odoo bash -c "cat > '${PGDATA}/pg_hba.conf'" <<'EOF'
# CAPABILITY-ASYMMETRY MODEL — see provision_template.sh header.
# The OS odoo user reaches PG role odoo password-free over the socket (peer).
local   all   all                     peer
# TCP loopback ALWAYS requires a password (scram). The agent (uid 1000) has no
# PG password, so it cannot psql the DB — its only path is the Odoo app login.
host    all   all   127.0.0.1/32       scram-sha-256
host    all   all   ::1/128            scram-sha-256
EOF

# ── 4) Start the cluster temporarily ─────────────────────────────────────────
log "starting cluster"
as_odoo "${PGBIN}/pg_ctl" -D "${PGDATA}" -l "${PGLOG}" -w -t 60 start \
  || { echo "---- pg.log ----"; cat "${PGLOG}" 2>/dev/null || true; die "pg_ctl start failed"; }

ready=0
for _ in $(seq 1 30); do
  if as_odoo "${PGBIN}/pg_isready" -h "${SOCKDIR}" -p 5432 >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ "${ready}" == "1" ]] || { cat "${PGLOG}" 2>/dev/null || true; die "cluster not ready"; }
log "cluster ready"

# ── 5) Set the superuser role password (for scram TCP; the oracle uses it) ────
SUPER_PW="$(cat /opt/odoo/secret/pg_super.pw)"
[[ -n "${SUPER_PW}" ]] || die "empty superuser password file"
log "setting PG role odoo superuser password"
# Inline the secret into the DDL (build-time only — this whole layer, incl. the
# odoo-owned secret file, is baked into the image; no agent-visible process).
# Double any single quotes to keep the SQL literal safe.
SUPER_PW_SQL="${SUPER_PW//\'/\'\'}"
psql_odoo -d postgres \
  -c "ALTER ROLE odoo WITH SUPERUSER LOGIN PASSWORD '${SUPER_PW_SQL}';"

# ── 6) Create the AgentService database (owner odoo) ─────────────────────────
log "creating database ${ODOO_DB_TEMPLATE}"
as_odoo "${PGBIN}/createdb" -O odoo "${ODOO_DB_TEMPLATE}"

# ── 7) Odoo config (reused verbatim at runtime by odoo_ctl) ──────────────────
# Discover the community addons dir from the installed package (robust to .deb
# layout drift); fail loud if base isn't there.
ADDONS=""
for c in \
  /usr/lib/python3/dist-packages/odoo/addons \
  /usr/lib/python3/dist-packages/odoo/odoo/addons; do
  if [[ -d "${c}/base" ]]; then ADDONS="${c}"; break; fi
done
[[ -n "${ADDONS}" ]] || die "could not locate Odoo base addons dir"
log "addons_path=${ADDONS}"

cat > "${ODOO_CONF}" <<EOF
[options]
addons_path = ${ADDONS}
data_dir = /opt/odoo/state
logfile = ${ODOO_LOG}
db_host = ${SOCKDIR}
db_port = 5432
db_user = odoo
db_password = False
http_interface = 127.0.0.1
http_port = 8069
db_name = odoo_write
dbfilter = ^odoo_write$
list_db = False
proxy_mode = False
EOF
chown odoo:odoo "${ODOO_CONF}"
chmod 0640 "${ODOO_CONF}"

# ── 8) Install the workflow modules into AgentService ────────────────────────
log "installing Odoo modules: ${ODOO_MODULES}"
as_odoo "${ODOO_BIN}" -c "${ODOO_CONF}" -d "${ODOO_DB_TEMPLATE}" \
  -i "${ODOO_MODULES}" --without-demo=all --stop-after-init --no-http \
  || { echo "---- odoo.log tail ----"; tail -n 80 "${ODOO_LOG}" 2>/dev/null || true; die "module install failed"; }

# Real gate: the installed schema must carry the workflow tables.
for t in sale_order purchase_order stock_landed_cost mrp_production account_move; do
  got="$(psql_odoo -d "${ODOO_DB_TEMPLATE}" -tAc "SELECT to_regclass('public.${t}');")"
  [[ "${got}" == "${t}" ]] || { tail -n 80 "${ODOO_LOG}" 2>/dev/null || true; die "table ${t} missing after install (got '${got}')"; }
done
log "module install verified (workflow tables present)"

# ── 9) Set the Odoo logins to match the staged credential files ──────────────
# admin@example.com / admin  -> full write (all functional manager groups)
# report@example.com / report -> internal user + read-only group only (no write)
cat > "${SETUP_PY}" <<'PYEOF'
# Executed via `odoo shell` (env = superuser). Fail loud: prints SETUP_USERS_OK
# only after a successful commit; the provisioner greps for that sentinel.
Users = env['res.users']
Groups = env['res.groups']
IMA = env['ir.model.access']
IModel = env['ir.model']

# Odoo 19 renamed the direct groups relation on res.users from `groups_id` to
# `group_ids`; detect whichever this build exposes and use it everywhere.
GF = 'groups_id' if 'groups_id' in Users._fields else 'group_ids'

# --- admin login/password + full functional write access ------------------
admin = env.ref('base.user_admin')
admin.write({'login': 'admin@example.com'})
admin.password = 'admin'

manager_xmlids = [
    'sales_team.group_sale_manager',
    'sales_team.group_sale_salesman_all_leads',
    'purchase.group_purchase_manager',
    'stock.group_stock_manager',
    'stock.group_production_lot',
    'stock.group_stock_multi_locations',
    'stock.group_adv_location',
    'mrp.group_mrp_manager',
    'mrp.group_mrp_routings',
    'account.group_account_manager',
    'account.group_account_user',
    'hr_expense.group_hr_expense_manager',
    'analytic.group_analytic_accounting',
    'product.group_product_variant',
    'uom.group_uom',
]
add = env['res.groups']
for x in manager_xmlids:
    g = env.ref(x, raise_if_not_found=False)
    if g:
        add |= g
if add:
    admin.write({GF: [(4, g.id) for g in add]})

# --- read-only security group + read-only ir.model.access ------------------
ro = Groups.search([('name', '=', 'ERP Read Only')], limit=1)
if not ro:
    ro = Groups.create({'name': 'ERP Read Only'})

ro_models = [
    'sale.order', 'sale.order.line',
    'purchase.order', 'purchase.order.line',
    'account.move', 'account.move.line',
    'stock.picking', 'stock.picking.type', 'stock.move', 'stock.move.line',
    'stock.quant', 'stock.lot', 'stock.scrap',
    'stock.landed.cost',
    'mrp.production', 'mrp.workorder',
    'stock.warehouse', 'stock.location',
    'product.product', 'product.template',
    'account.analytic.line', 'account.analytic.account',
    'res.partner', 'res.currency',
]
for mn in ro_models:
    model = IModel.search([('model', '=', mn)], limit=1)
    if not model:
        continue
    if not IMA.search([('model_id', '=', model.id), ('group_id', '=', ro.id)], limit=1):
        IMA.create({
            'name': 'ro_%s' % mn.replace('.', '_'),
            'model_id': model.id,
            'group_id': ro.id,
            'perm_read': True,
            'perm_write': False,
            'perm_create': False,
            'perm_unlink': False,
        })

# --- report login: internal + read-only group only ------------------------
internal = env.ref('base.group_user')
report = Users.search([('login', '=', 'report@example.com')], limit=1)
vals = {
    'name': 'Report Viewer',
    'login': 'report@example.com',
    GF: [(6, 0, [internal.id, ro.id])],
}
if report:
    report.write(vals)
    report.password = 'report'
else:
    report = Users.create(dict(vals, password='report'))

env.cr.commit()
print('SETUP_USERS_OK admin=%s report=%s ro_group_id=%s' % (admin.login, report.login, ro.id))
PYEOF
chown odoo:odoo "${SETUP_PY}"

log "configuring Odoo logins (admin + read-only report)"
SETUP_OUT="$(as_odoo "${ODOO_BIN}" shell -c "${ODOO_CONF}" -d "${ODOO_DB_TEMPLATE}" --no-http < "${SETUP_PY}" 2>&1)" || true
echo "${SETUP_OUT}" | sed 's/^/[odoo-shell] /'
echo "${SETUP_OUT}" | grep -q 'SETUP_USERS_OK' \
  || { tail -n 60 "${ODOO_LOG}" 2>/dev/null || true; die "user setup did not confirm (no SETUP_USERS_OK)"; }
log "logins configured"

# ── 10) Mark AgentService a locked template, then stop the cluster ───────────
log "marking ${ODOO_DB_TEMPLATE} as a locked template"
psql_odoo -d postgres -c \
  "UPDATE pg_database SET datistemplate=true, datallowconn=false WHERE datname='${ODOO_DB_TEMPLATE}';"

log "stopping cluster (fast)"
as_odoo "${PGBIN}/pg_ctl" -D "${PGDATA}" -m fast -w -t 60 stop || die "pg_ctl stop failed"

log "provisioning complete — AgentService template baked."
