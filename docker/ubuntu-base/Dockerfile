# ubuntu-base — the minimal a2a sandbox image.
#
# A plain Ubuntu userspace with just enough for the a2a benchmark:
#   * python3 + pydantic       — runs the shipped a2_run sandbox entry
#   * node + npm               — runtime for the baked coding CLIs
#   * /opt/a2a hub + MCP tools — a2a control plane: the hub HTTP server is
#                                PID 1 (port 8765, published by the provider);
#                                the MCP script gives the agent the a2a tools
#                                (send_message, task_complete).
#   * baked coding CLIs        — claude-code, codex, and cursor-agent are
#                                installed at build time at PINNED versions
#                                (ARGs below). This removes the run-time
#                                install step and its network dependency, so a
#                                container whose egress is locked down can still
#                                boot its harness. The deployers keep their
#                                verify-and-correct probe: if a baked version
#                                ever drifts from the yaml pin the run fails
#                                loud.
#   * offline document tools   — read, generate, recalculate, render, OCR, and
#                                validate common legal-office formats without
#                                relying on runtime package installs or egress.
#   * egress tooling           — iptables/ipset/dnsmasq + egress_init.sh, used
#                                by the docker provider to apply a per-role
#                                network allowlist (see egress_init.sh). Inert
#                                unless A2_EGRESS_MODE is set.
#
# Build (the docker provider auto-builds this when the tag is missing):
#   docker build -t a2-bench-ubuntu:latest docker/ubuntu-base

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        nodejs npm \
        git curl ca-certificates file gawk gcc g++ jq ripgrep \
        libreoffice pandoc poppler-utils tesseract-ocr \
        zip unzip libxml2-utils \
        fonts-dejavu-core fonts-liberation fonts-noto-core \
        iptables ipset dnsmasq dnsutils e2fsprogs procps \
    && rm -rf /var/lib/apt/lists/*

# a2_run's base_interface (imported by the in-container sandbox entry) needs
# pydantic; Ubuntu 24.04 pip refuses system installs without the override.
RUN pip3 install --break-system-packages --no-cache-dir pydantic

# Offline legal-document workspace. EML parsing uses Python's stdlib ``email``;
# these packages cover Office extraction/generation, redlines, spreadsheet
# computation, PDF/OCR workflows, charts, and visual quality assurance.
RUN pip3 install --break-system-packages --no-cache-dir \
        defusedxml \
        diff-match-patch \
        docxtpl \
        lxml \
        markitdown \
        matplotlib \
        openpyxl \
        pandas \
        pdf2image \
        pdfplumber \
        pillow \
        pypdf \
        python-docx \
        python-pptx \
        seaborn \
        xlsxwriter

# CPU-only torch + safetensors for mlinfra/ckpt_release_handoff (both roles
# load .pt/.safetensors artifacts; evaluate() runs a forward pass in-sandbox).
# Pins from the anchor's runtime manifest; the /whl/cpu index avoids the
# multi-GB CUDA wheels on x86_64.
RUN pip3 install --break-system-packages --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.5.1 \
    && pip3 install --break-system-packages --no-cache-dir \
        safetensors==0.7.0 packaging numpy

# Conversation-control plane + the egress initializer (run as root by the
# provider before any agent starts).
COPY a2a_hub.py a2a_tools_mcp.py /opt/a2a/
COPY egress_init.sh /opt/a2a/egress_init.sh
RUN chmod +x /opt/a2a/egress_init.sh

# Framework runtime source lands here per-run (shipped by the sandbox executor,
# imported by the in-sandbox entry via PYTHONPATH); ``sessions`` holds the
# per-run control sidecars + work dirs (work_dir_base). Both are co-located with
# the a2a control plane instead of the agent's home so they read as machine
# runtime, not an eval kit under ``~`` an agent would find via ``ls -R ~``.
# Pre-created uid-1000-writable because /opt/a2a is root-owned (root COPY above)
# and both the ship writes and the in-sandbox entry run as the unprivileged user.
RUN mkdir -p /opt/a2a/runtime-src /opt/a2a/sessions \
    && chown 1000:1000 /opt/a2a/runtime-src /opt/a2a/sessions

# Non-root user matching the framework's baked path conventions.
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash user \
    && mkdir -p /media/user/data/work \
    && chown -R user:user /media/user

# ── Baked coding CLIs (pinned) ──────────────────────────────────────────────
# Single source of truth for the pins. Keep these in lockstep with
# configs/agents/*.yaml.
ARG CLAUDE_CODE_VERSION=2.1.207
ARG CODEX_VERSION=0.145.0
ARG CURSOR_VERSION=2026.07.09-c59fd9a

USER user
ENV npm_config_cache=/home/user/.npm-a2
ENV PATH=/home/user/.local/bin:$PATH
ENV NODE_PATH=/home/user/.local/lib/node_modules
WORKDIR /home/user

# claude-code + codex via npm, under the same ~/.local prefix the deployers'
# verify-and-correct probe expects (so a matching version is reused, not
# reinstalled). --force mirrors the bootstrap install path.
RUN npm install -g --force --prefix /home/user/.local \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm install -g --force --prefix /home/user/.local \
        "@openai/codex@${CODEX_VERSION}" \
    && npm install -g --prefix /home/user/.local \
        docx pptxgenjs

# cursor-agent from Anysphere's pinned tarball, in the official installer
# layout the CursorDeployer expects (versions/<v>/ + a bin symlink). The
# deployer still runs its idempotent MCP-timeout patch over this on start.
RUN set -e; \
    machine="$(uname -m)"; \
    case "$machine" in aarch64|arm64) arch=arm64;; *) arch=x64;; esac; \
    vdir="/home/user/.local/share/cursor-agent/versions/${CURSOR_VERSION}"; \
    mkdir -p "$vdir" /home/user/.local/bin; \
    curl -fSL -s \
      "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/${arch}/agent-cli-package.tar.gz" \
      | tar --strip-components=1 -xzf - -C "$vdir"; \
    ln -sf "$vdir/cursor-agent" /home/user/.local/bin/cursor-agent

EXPOSE 8765
ENTRYPOINT ["python3", "/opt/a2a/a2a_hub.py", "--port", "8765"]
