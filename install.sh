#!/usr/bin/env bash
#
# Install a Buzz agent runtime: buzz-acp + Claude Code, under systemd.
#
# Target: a fresh Debian 12/13 machine (LXC, VM, VPS) that can reach a Buzz
# relay. Idempotent — re-running upgrades in place and never rewrites secrets.
#
#   sudo ./install.sh                 # base runtime
#   sudo ./install.sh --with-browser  # + headless Chromium and Playwright MCP
#
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib/buzzer.sh
. "$HERE/lib/buzzer.sh"

BUZZ_REF=${BUZZ_REF:-main}          # block/buzz commit or tag to build buzz-acp from
NODE_MAJOR=${NODE_MAJOR:-24}
PREFIX=${PREFIX:-/usr/local/bin}
ETC=${ETC:-/etc/buzzer}
SRC=${SRC:-/opt/buzzer/src}
WORKDIR=${WORKDIR:-/var/lib/buzzer}
RUN_USER=${RUN_USER:-buzzer}
WITH_BROWSER=0

for arg in "$@"; do
  case $arg in
    --with-browser) WITH_BROWSER=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

say() { printf '==> %s\n' "$*"; }

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates git build-essential pkg-config libssl-dev jq

if ! command -v node >/dev/null || [ "$(node -v | cut -c2- | cut -d. -f1)" -lt "$NODE_MAJOR" ]; then
  say "node ${NODE_MAJOR}"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y -qq nodejs
fi

say "claude code + acp adapter"
npm install -g --silent @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp

# buzz-acp ships no binary: it is a crate in the Buzz workspace, so it is built
# from source and pinned to BUZZ_REF. This is the slow part of a first install.
if [ ! -x "$PREFIX/buzz-acp" ] || [ "${FORCE_BUILD:-0}" = 1 ]; then
  say "rust toolchain"
  command -v cargo >/dev/null || {
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
  }
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env"

  say "buzz-acp @ ${BUZZ_REF} (this takes a while)"
  mkdir -p "$(dirname "$SRC")"
  if [ -d "$SRC/.git" ]; then git -C "$SRC" fetch --depth 1 origin "$BUZZ_REF"
  else git clone --depth 1 --branch "$BUZZ_REF" https://github.com/block/buzz.git "$SRC"; fi
  git -C "$SRC" checkout --detach FETCH_HEAD 2>/dev/null || true
  ( cd "$SRC" && cargo build --release -p buzz-acp -p buzz-admin )
  install -m 0755 "$SRC/target/release/buzz-acp" "$PREFIX/buzz-acp"
  install -m 0755 "$SRC/target/release/buzz-admin" "$PREFIX/buzz-admin"
fi

if [ "$WITH_BROWSER" = 1 ]; then
  say "headless chromium + playwright mcp"
  npm install -g --silent @playwright/mcp
  npx --yes playwright install --with-deps chromium
fi

say "service account"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --home-dir "$WORKDIR" --create-home --shell /usr/sbin/nologin "$RUN_USER"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0750 "$WORKDIR"

say "config"
install -d -m 0700 "$ETC"
if [ -f "$ETC/environment" ]; then
  echo "    keeping existing $ETC/environment"
else
  install -m 0600 "$HERE/environment.example" "$ETC/environment"
  echo "    wrote $ETC/environment from the template — fill it in before starting"
fi
chown -R root:"$RUN_USER" "$ETC"
chmod 0640 "$ETC/environment"

say "systemd unit"
install -m 0644 "$HERE/systemd/buzzer.service" /etc/systemd/system/buzzer.service
systemctl daemon-reload
systemctl enable buzzer.service >/dev/null

# Fail loudly here rather than in a crash loop three minutes from now.
set +u
# shellcheck disable=SC1091
. "$ETC/environment"
set -u
missing=0
for var in BUZZ_PRIVATE_KEY BUZZ_RELAY_URL; do
  require_env "$var" || { echo "    missing: $var" >&2; missing=1; }
done
if validate_relay_url "${BUZZ_RELAY_URL-}"; then :; else
  echo "    BUZZ_RELAY_URL must be ws:// or wss:// (got: ${BUZZ_RELAY_URL-<empty>})" >&2
  missing=1
fi

echo
if [ "$missing" = 0 ]; then
  say "ready — key $(redact "$BUZZ_PRIVATE_KEY") on $BUZZ_RELAY_URL"
  echo "    systemctl start buzzer"
else
  say "installed, NOT started — edit $ETC/environment, then: systemctl start buzzer"
  exit 3
fi
