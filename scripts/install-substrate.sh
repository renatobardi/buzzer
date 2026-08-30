#!/usr/bin/env bash
#
# Prepare a Debian/Ubuntu machine to host Buzz agent personas as systemd units.
#
# This installs the *substrate* only: the unprivileged user, the directories,
# and the templated unit. It creates no persona and holds no key — personas are
# deployed from Buzz Desktop through the buzz-backend-lxc provider, which is
# what writes /etc/buzz/agents/<slug>.env and enables the unit.
#
# Run it on the target machine, as root:
#
#   sudo ./scripts/install-substrate.sh
#
# Or on an LXC container from the LXD host:
#
#   lxc file push -p scripts/install-substrate.sh <container>/tmp/ \
#     && lxc exec <container> -- bash /tmp/install-substrate.sh
#
# Idempotent: re-running converges. It never touches an existing agent's env
# file or workspace.
set -euo pipefail

BIN_DIR=${BIN_DIR:-/usr/local/bin}
ETC_DIR=${ETC_DIR:-/etc/buzz}
WORKSPACE_DIR=${WORKSPACE_DIR:-/srv/agents}
AGENT_USER=${AGENT_USER:-buzz}
UNIT_SRC=${UNIT_SRC:-$(cd "$(dirname "$0")/.." && pwd)/systemd/buzz-agent@.service}

say() { printf '==> %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "this needs systemd" >&2; exit 1; }
[ -f "$UNIT_SRC" ] || { echo "unit template not found at $UNIT_SRC" >&2; exit 1; }

say "user: $AGENT_USER"
# No login shell and no password: this account exists to own files and run a
# process, and nothing should be able to log in as it.
id "$AGENT_USER" >/dev/null 2>&1 \
  || useradd --system --create-home --home-dir "$WORKSPACE_DIR" \
             --shell /usr/sbin/nologin "$AGENT_USER"

say "directories"
# The env files hold agent private keys, so their directory is not world
# readable and neither are they. Prompts are not secret and stay readable so
# they can be diffed and reviewed.
install -d -o root -g root -m 0755 "$ETC_DIR" "$ETC_DIR/prompts"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0750 "$ETC_DIR/agents" "$WORKSPACE_DIR"

say "unit"
install -m 0644 "$UNIT_SRC" /etc/systemd/system/buzz-agent@.service
systemctl daemon-reload

say "verify"
# systemd-analyze verify also resolves ExecStart, so it fails while the
# binaries are absent. That is a legitimate state: the substrate is meant to be
# installable before, or independently of, the build. So the unit's own
# problems are what gate here — an unknown key, a syntax error — and a missing
# binary is reported at the end instead.
verify_out=$(systemd-analyze verify buzz-agent@.service 2>&1 || true)
unit_problems=$(printf '%s\n' "$verify_out" \
  | grep -v "is not executable" \
  | grep -v "^$" || true)
if [ -n "$unit_problems" ]; then
  echo "    the unit does not verify:" >&2
  printf '    %s\n' "$unit_problems" >&2
  exit 1
fi
# [L1.2] Presence is the only status signal a remote agent has. A stray
# BUZZ_ACP_NO_PRESENCE anywhere would blind it silently.
if grep -rqs BUZZ_ACP_NO_PRESENCE "$ETC_DIR/agents"; then
  echo "    BUZZ_ACP_NO_PRESENCE is set in an env file — remove it" >&2
  exit 1
fi

missing=""
for binary in buzz-acp buzz-agent; do
  [ -x "$BIN_DIR/$binary" ] || missing="$missing $binary"
done

cat <<MSG

==> substrate ready. It runs no persona yet, by design.
MSG

if [ -n "$missing" ]; then
  cat <<MSG
    Still missing in $BIN_DIR:$missing
    Build them with scripts/build-binaries.sh, then deploy a persona from
    Buzz Desktop with "Run on: lxc".
MSG
else
  cat <<MSG
    Binaries are in place. Deploy a persona from Buzz Desktop with
    "Run on: lxc", then watch it come up:
        journalctl -u buzz-agent@<slug> -f
MSG
fi
