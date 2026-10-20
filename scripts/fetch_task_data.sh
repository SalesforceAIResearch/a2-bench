#!/usr/bin/env bash
# Fetch the a2-bench task data for the local Docker provider.
#
# The task data (input + software + reference for every domain/task/variant) is
# NOT committed to git — it lives as a PRIVATE HuggingFace dataset so the team
# can share, and update/add to, it from one place. This script snapshots that
# dataset into the dir docker.yaml reads from (`task_data_source: local:task-data`).
#
# It's a folder snapshot (not a tarball): re-running only pulls files that
# changed, so it doubles as your "git pull" for task data.
#
# Prerequisites (one-time):
#   1. Be granted access to the gated org dataset (org: a2a-salesforce).
#   2. Auth: export HF_TOKEN=...   (or run: hf auth login)
#   3. huggingface-hub installed:  uv sync   (it's a project dep)
#
# Usage:
#   scripts/fetch_task_data.sh [DEST] [REPO_ID]
#     DEST     target dir (default: task-data, matching `task_data_source: local:task-data`)
#     REPO_ID  HF dataset  (default: a2a-salesforce/a2-bench)
set -euo pipefail

REPO="${2:-a2a-salesforce/a2-bench}"
DEST="${1:-task-data}"

# Prefer the modern `hf` CLI; fall back to the (deprecated) huggingface-cli.
if command -v hf >/dev/null 2>&1; then
  DL=(hf download "$REPO" --repo-type dataset --local-dir "$DEST")
elif command -v huggingface-cli >/dev/null 2>&1; then
  DL=(huggingface-cli download "$REPO" --repo-type dataset --local-dir "$DEST")
else
  echo "ERROR: neither 'hf' nor 'huggingface-cli' found — run: uv sync (or pip install huggingface-hub)" >&2
  exit 1
fi

mkdir -p "$DEST"
echo ">> Downloading private dataset $REPO into $DEST/ (needs granted access + HF_TOKEN / hf auth login) ..."
"${DL[@]}"

echo ">> Done. Layout: $DEST/<domain>/<task>/<variant>/[<role>/]{input,software,reference}"
echo ">> docker.yaml already points at it via:  task_data_source: local:$DEST"
