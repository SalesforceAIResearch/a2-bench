#!/usr/bin/env bash
# egress_init.sh — per-container network egress control, applied ONCE by the
# docker provider (via `docker exec -u root`) right after the container boots
# and BEFORE any agent runs.
#
# It is a deliberate no-op unless A2_EGRESS_MODE is set, so a container that
# opts into nothing behaves exactly as it did before this file existed.
#
#   A2_EGRESS_MODE=open        (or unset)  → no filtering; full default network.
#   A2_EGRESS_MODE=restricted              → default-DROP egress except:
#                                              * loopback (the a2a hub + tools)
#                                              * DNS to the in-container resolver
#                                              * ESTABLISHED/RELATED return traffic
#                                              * hosts resolved from A2_EGRESS_ALLOW
#
# The allowlist is a comma/space-separated list of DOMAINS in A2_EGRESS_ALLOW
# (e.g. "api.openai.com,bedrock-runtime.us-east-1.amazonaws.com"). Because the
# model backends are CDN-fronted with rotating IPs, we do NOT pin IPs: dnsmasq
# forwards ONLY the allowlisted domains upstream (a per-domain `server=/d/`
# line; with no-resolv and NO global server, every other query is REFUSED) and
# auto-populates an ipset that iptables accepts, so every fresh resolution
# keeps the set current. A domain not on the list neither resolves nor lands in
# the set. The direct-to-resolver path is pinned to dnsmasq's own uid, so an
# agent cannot run its own lookups against the upstream resolver — i.e. DNS
# itself cannot be turned into a data channel to an arbitrary domain.
#
# LIMITATION: filtering is IPv4 only (iptables + a `family inet` ipset). The
# script REFUSES to launch (fail-loud) if the container has global IPv6 egress,
# which would otherwise reach any AAAA host unfiltered.
#
# Requires NET_ADMIN (the provider adds --cap-add=NET_ADMIN when a restricted
# mode is requested). Fails LOUD: any step that cannot be completed exits non-
# zero so the provider aborts the run rather than launching an agent whose
# network posture is not what the experiment intends (a silently-open
# "restricted" agent would void the result).
set -euo pipefail

MODE="${A2_EGRESS_MODE:-open}"

log() { echo "[egress_init] $*" >&2; }

if [[ "${MODE}" == "open" ]]; then
    log "mode=open — no egress filtering applied"
    exit 0
fi

if [[ "${MODE}" != "restricted" ]]; then
    log "FATAL: unknown A2_EGRESS_MODE='${MODE}' (expected open|restricted)"
    exit 2
fi

ALLOW_RAW="${A2_EGRESS_ALLOW:-}"
if [[ -z "${ALLOW_RAW// /}" ]]; then
    log "FATAL: A2_EGRESS_MODE=restricted but A2_EGRESS_ALLOW is empty — a"
    log "       restricted agent with no allowlist could not reach its own model"
    log "       backend and would fail to think. Refusing to launch."
    exit 3
fi

# Normalise "a.com, b.com c.com" → newline list, drop blanks/dups.
mapfile -t ALLOW < <(echo "${ALLOW_RAW}" | tr ', ' '\n\n' | sed '/^$/d' | sort -u)
log "mode=restricted; allowlisted domains: ${ALLOW[*]}"

command -v iptables >/dev/null || { log "FATAL: iptables not installed in image"; exit 4; }
command -v ipset    >/dev/null || { log "FATAL: ipset not installed in image";    exit 4; }
command -v dnsmasq  >/dev/null || { log "FATAL: dnsmasq not installed in image";   exit 4; }

# ── IPv6: refuse to launch when the container has global v6 egress ─────────
# This script filters IPv4 ONLY. If the container has a working global IPv6
# route, a restricted agent could reach any AAAA-resolvable host completely
# unfiltered, silently voiding the "restricted" posture. We deliberately do NOT
# mirror the rules into ip6tables here: the ipset is v4 and dnsmasq only adds A
# records to it, so a blanket v6 default-DROP could also strand a v6-preferring
# model backend. Per this file's fail-loud contract, refuse rather than run a
# mis-postured agent.
_has_global_ipv6() {
    # iproute2 if present: a global IPv6 default route means real v6 egress.
    if command -v ip >/dev/null 2>&1; then
        ip -6 route show default 2>/dev/null | grep -q . && return 0
        return 1
    fi
    # Fallback (no iproute2): a global-scope (scope 00) address on any
    # non-loopback iface. /proc/net/if_inet6 cols: addr ifidx prefix scope flags dev
    if [[ -r /proc/net/if_inet6 ]]; then
        awk '$4 == "00" && $6 != "lo" { f = 1 } END { exit(f ? 0 : 1) }' \
            /proc/net/if_inet6 && return 0
    fi
    return 1
}
if _has_global_ipv6; then
    log "FATAL: container has global IPv6 egress but this filter is IPv4-only —"
    log "       a restricted agent could reach AAAA hosts unfiltered. Disable IPv6"
    log "       for this container/network before launching a restricted role"
    log "       (docker daemon without IPv6, or run with"
    log "        --sysctl net.ipv6.conf.all.disable_ipv6=1)."
    exit 7
fi

# ── dnsmasq runs as a fixed, non-root uid so egress can be pinned to it ────
# The upstream-forwarding iptables rules below use `-m owner --uid-owner` to
# permit ONLY dnsmasq to reach the resolver; that requires dnsmasq to run under
# a known uid distinct from the agent's (uid 1000). Prefer the packaged user;
# create a dedicated system user if the image lacks it. dnsmasq drops to this
# uid after binding, so its per-query upstream sockets carry it.
DNSMASQ_USER="dnsmasq"
if ! id -u "${DNSMASQ_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${DNSMASQ_USER}" \
        || { log "FATAL: could not create '${DNSMASQ_USER}' system user"; exit 6; }
fi
DNSMASQ_UID="$(id -u  "${DNSMASQ_USER}")"
DNSMASQ_GROUP="$(id -gn "${DNSMASQ_USER}")"
if [[ "${DNSMASQ_UID}" == "0" || "${DNSMASQ_UID}" == "1000" ]]; then
    log "FATAL: dnsmasq user resolved to uid ${DNSMASQ_UID} — the owner-match"
    log "       egress pin would be void (shared with root/agent). Refusing."
    exit 6
fi
log "dnsmasq runs as ${DNSMASQ_USER} (uid=${DNSMASQ_UID}, group=${DNSMASQ_GROUP})"

# Upstream resolver the local dnsmasq forwards to. Read the ORIGINAL nameserver
# before we overwrite resolv.conf; fall back to a public resolver.
UPSTREAM_DNS="$(awk '/^nameserver/ {print $2; exit}' /etc/resolv.conf 2>/dev/null || true)"
if [[ -z "${UPSTREAM_DNS}" || "${UPSTREAM_DNS}" == "127.0.0.1" ]]; then
    UPSTREAM_DNS="8.8.8.8"
fi
log "upstream DNS: ${UPSTREAM_DNS}"

# ── ipset that iptables matches on ────────────────────────────────────────
ipset destroy a2_allow 2>/dev/null || true
ipset create a2_allow hash:ip family inet timeout 0

# ── dnsmasq: resolve ONLY allowlisted domains, add answers to the ipset ────
DNSMASQ_CONF="/etc/dnsmasq.d/a2_egress.conf"
mkdir -p /etc/dnsmasq.d
{
    echo "no-resolv"
    echo "no-hosts"
    echo "listen-address=127.0.0.1"
    echo "bind-interfaces"
    echo "cache-size=1000"
    # Drop to the fixed uid resolved above so the port-53 egress rules can pin
    # upstream forwarding to dnsmasq alone (-m owner --uid-owner).
    echo "user=${DNSMASQ_USER}"
    echo "group=${DNSMASQ_GROUP}"
    for d in "${ALLOW[@]}"; do
        # server=/domain/ forwards ONLY the allowlisted domains (and their
        # subdomains) upstream. With no-resolv and NO global `server=` line,
        # every other query is REFUSED — so a name outside the allowlist never
        # resolves, and DNS itself cannot be used as a data channel to an
        # arbitrary attacker-controlled domain.
        echo "server=/${d}/${UPSTREAM_DNS}"
        # ipset= adds every A-record answer for the domain (and its subdomains)
        # into a2_allow as it resolves, so iptables then accepts egress to it.
        echo "ipset=/${d}/a2_allow"
    done
} > "${DNSMASQ_CONF}"

# Stop any stray dnsmasq, then start ours in the background.
pkill -x dnsmasq 2>/dev/null || true
sleep 0.3
dnsmasq --conf-file="${DNSMASQ_CONF}" --pid-file=/run/a2_dnsmasq.pid
sleep 0.3
if ! pgrep -x dnsmasq >/dev/null; then
    log "FATAL: dnsmasq failed to start"
    exit 5
fi

# Point the container's resolver at our dnsmasq so every lookup is filtered.
# (Best-effort chattr -i first: some images pin resolv.conf immutable.)
chattr -i /etc/resolv.conf 2>/dev/null || true
printf 'nameserver 127.0.0.1\noptions timeout:2 attempts:2\n' > /etc/resolv.conf

# ── iptables: default-drop egress with the allowlist punch-throughs ────────
# Only OUTPUT is constrained; INPUT is left alone (the hub listens on lo and
# the host reaches it via `docker exec`, not the network).
iptables -F OUTPUT
iptables -P OUTPUT ACCEPT   # keep ACCEPT while we add rules, flip to DROP last

iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
# DNS to our local dnsmasq …
iptables -A OUTPUT -p udp -d 127.0.0.1 --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp -d 127.0.0.1 --dport 53 -j ACCEPT
# … and dnsmasq's OWN forwarding to the upstream resolver — pinned to the
# dnsmasq uid (-m owner) so an agent process cannot bypass dnsmasq and query
# the upstream resolver directly, which would re-open arbitrary DNS resolution
# and a DNS data channel.
iptables -A OUTPUT -m owner --uid-owner "${DNSMASQ_UID}" -p udp -d "${UPSTREAM_DNS}" --dport 53 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "${DNSMASQ_UID}" -p tcp -d "${UPSTREAM_DNS}" --dport 53 -j ACCEPT
# Allowlisted destinations (populated by dnsmasq as they resolve).
iptables -A OUTPUT -m set --match-set a2_allow dst -j ACCEPT
# Everything else is denied. REJECT (not DROP) so a blocked agent fails fast
# with a clear "connection refused" instead of hanging on a timeout.
iptables -A OUTPUT -j REJECT --reject-with icmp-port-unreachable
iptables -P OUTPUT DROP

log "egress lockdown active (default DROP; ${#ALLOW[@]} domain(s) allowlisted)"
