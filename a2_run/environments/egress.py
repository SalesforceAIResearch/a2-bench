"""Derive a network-restricted role's egress allowlist from its harness.

A task's ``egress:`` block says only WHICH roles are network-restricted for a
variant (host-side; never agent-visible). The concrete set of domains a
restricted role may still reach is not a task concern — it is whatever that
role's coding CLI needs to talk to its own model backend, so it is derived here
from the agent spec bound to the role.

The principle: a restricted agent must still be able to *think* (reach its LLM
endpoint) but must NOT be able to reach the open internet. So the allowlist is
exactly the backend hosts for the harness+provider in play, and nothing else.

Endpoint, not vendor. When the environment points a harness at a custom
endpoint — an internal LLM gateway or proxy via a ``*_BASE_URL`` override — the
agent talks ONLY to that host and never to the vendor's native domain. So the
allowlist is derived from the override URL's host when one is set (which is both
CORRECT — the native domain is never hit — and TIGHTER — the native suffix like
``amazonaws.com`` would whitelist unrelated services such as S3, an open
side-channel). Only when no override is configured do we fall back to the
harness's native backend domains.

Kept deliberately small and declarative so it is unit-testable offline and so a
new harness/provider fails LOUD (``UnknownBackend``) rather than silently
producing an empty allowlist — an empty allowlist would strand the agent with
no path to its model, and a too-wide one would defeat the restriction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class UnknownBackend(ValueError):
    """No known backend domains for a (harness, provider) — refuse to guess."""


@dataclass(frozen=True)
class _Backend:
    """How a (harness, provider) reaches its model.

    ``env_url`` names the environment variable that, when set, overrides the
    endpoint (an internal gateway / proxy). ``default`` is the native backend
    domain suffix used when no override is configured. dnsmasq's
    ``ipset=/<domain>/`` matching covers a domain and all its subdomains, so a
    registrable suffix (or a full gateway host) is enough.
    """

    default: tuple[str, ...]
    env_url: str | None = None


# (harness, provider) -> how it reaches its backend. provider "" matches when
# the harness has a single fixed backend (codex, cursor).
_BACKENDS: dict[tuple[str, str], _Backend] = {
    # Claude Code, direct Anthropic API (or a gateway via ANTHROPIC_BASE_URL).
    ("claude_code", "direct"): _Backend(
        default=("api.anthropic.com", "statsig.anthropic.com"),
        env_url="ANTHROPIC_BASE_URL",
    ),
    # Claude Code driving an OpenRouter model. Native: openrouter.ai (the
    # Anthropic-skin endpoint). A custom ANTHROPIC_BASE_URL (self-hosted proxy)
    # narrows the allowlist to exactly that host.
    ("claude_code", "openrouter"): _Backend(
        default=("openrouter.ai",),
        env_url="ANTHROPIC_BASE_URL",
    ),
    # Claude Code via AWS Bedrock. Native: region-fronted bedrock-runtime under
    # amazonaws.com. When routed through an internal LLM gateway (token auth,
    # CLAUDE_CODE_SKIP_BEDROCK_AUTH=1) the agent talks ONLY to that gateway.
    ("claude_code", "bedrock"): _Backend(
        default=("amazonaws.com",),
        env_url="ANTHROPIC_BEDROCK_BASE_URL",
    ),
    # Claude Code via GCP Vertex. Native: aiplatform + OAuth token hosts.
    ("claude_code", "vertex"): _Backend(
        default=("googleapis.com", "oauth2.googleapis.com", "sts.googleapis.com"),
        env_url="ANTHROPIC_VERTEX_BASE_URL",
    ),
    # OpenAI Codex CLI — native api.openai.com, or a gateway via OPENAI_BASE_URL.
    ("codex", ""): _Backend(
        default=("api.openai.com",),
        env_url="OPENAI_BASE_URL",
    ),
    # cursor-agent — Anysphere's own backend only (no BYOK / gateway path).
    ("cursor", ""): _Backend(default=("cursor.sh", "cursor.com")),
}


def _host_of(url: str) -> str | None:
    """Registrable host of a base-url override (no scheme/port/path), or None.

    Tolerates a scheme-less value (``host[:port][/path]``) by prepending a
    scheme so ``urlsplit`` populates ``netloc`` rather than ``path``.
    """
    raw = url.strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"//{raw}", scheme="https")
    return parsed.hostname or None


def backend_domains(
    harness: str,
    provider: str = "",
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Allowlist domains a restricted ``harness``/``provider`` agent may reach.

    Resolves the ``(harness, provider)`` backend (falling back to
    ``(harness, "")`` for harnesses whose backend does not depend on a provider
    knob), then:

    - if the backend has a ``*_BASE_URL`` override set in ``env`` (default:
      ``os.environ``, where ``secret/.env`` has already been loaded), returns
      exactly that endpoint's host — the agent talks only there;
    - otherwise returns the harness's native backend domains.

    Raises :class:`UnknownBackend` if the harness/provider is not known, and
    ``ValueError`` if an override URL is set but has no parseable host (a
    misconfiguration that would otherwise strand the agent).
    """
    environ = os.environ if env is None else env
    for key in ((harness, provider), (harness, "")):
        backend = _BACKENDS.get(key)
        if backend is None:
            continue
        if backend.env_url:
            override = environ.get(backend.env_url, "").strip()
            if override:
                host = _host_of(override)
                if not host:
                    raise ValueError(
                        f"{backend.env_url} is set but has no parseable host "
                        f"({override!r}); a restricted {harness}/{provider} "
                        "agent cannot be given an egress allowlist"
                    )
                return (host,)
        return backend.default
    raise UnknownBackend(
        f"no known model-backend domains for harness={harness!r} "
        f"provider={provider!r}; add it to a2_run/environments/egress.py "
        f"before running a restricted-egress variant with this harness"
    )
