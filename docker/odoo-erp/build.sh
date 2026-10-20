#!/usr/bin/env bash
# build.sh — cold-build wrapper for the odoo-erp image family.
#
# The odoo-erp Dockerfile is `FROM a2-bench-ubuntu:latest`, so a cold build
# needs that base tag present first. The docker provider auto-builds ubuntu-base
# on first use, but a cold `docker build docker/odoo-erp` would fail if the base
# tag is missing — so this wrapper builds ubuntu-base (only if absent) then
# odoo-erp, in the right order.
#
#   ./build.sh            # reuse an existing a2-bench-ubuntu:latest
#   ./build.sh --no-cache # force a clean rebuild of both layers
set -euo pipefail

NO_CACHE=""
if [[ "${1:-}" == "--no-cache" ]]; then
  NO_CACHE="--no-cache"
  echo "[build] --no-cache requested"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
BASE_DIR="${REPO_ROOT}/docker/ubuntu-base"

echo "[build] repo root: ${REPO_ROOT}"

if docker image inspect a2-bench-ubuntu:latest >/dev/null 2>&1; then
  echo "[build] a2-bench-ubuntu:latest present — skipping base build"
else
  echo "[build] a2-bench-ubuntu:latest missing — building from ${BASE_DIR}"
  docker build ${NO_CACHE} -t a2-bench-ubuntu:latest "${BASE_DIR}"
fi

echo "[build] building a2-bench-odoo-erp:latest from ${HERE}"
docker build ${NO_CACHE} -t a2-bench-odoo-erp:latest "${HERE}"

echo "[build] done:"
docker images a2-bench-odoo-erp:latest
