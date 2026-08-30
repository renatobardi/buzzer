#!/usr/bin/env bash
#
# Build the Buzz binaries the substrate needs, from a pinned source ref.
#
# block/buzz publishes no Linux release of these binaries — the releases carry
# the desktop app only — so building from source is not a preference here, it
# is the only path. On arm64 this matters twice: there is no Linux arm64
# desktop build either.
#
# Run it on a machine you are willing to put a Rust toolchain on. That does not
# have to be the machine that runs the agents:
#
#   BUZZ_REF=<sha> ./scripts/build-binaries.sh                 # build here
#   BUZZ_REF=<sha> OUT_DIR=/tmp/out ./scripts/build-binaries.sh # then copy
#
# Idempotent: with the same BUZZ_REF and binaries already installed, it exits
# early.
set -euo pipefail

# Pin the runtime. Buzz is pre-1.0 with near-daily releases; a moving ref means
# the next run silently installs different software.
BUZZ_REF=${BUZZ_REF:?set BUZZ_REF to a block/buzz commit SHA or tag}
BUZZ_REPO=${BUZZ_REPO:-https://github.com/block/buzz.git}
SRC_DIR=${SRC_DIR:-/usr/local/src/buzz}
OUT_DIR=${OUT_DIR:-/usr/local/bin}
REF_FILE=${REF_FILE:-/etc/buzz/BUILD_REF}

# buzz-acp is the harness that holds the identity and talks to the relay,
# buzz-agent the ACP agent that runs the model loop, buzz-dev-mcp the tool
# server the agent reaches over MCP. buzz-agent has no built-in tools, so
# without an MCP server a persona can only talk.
CRATES=${CRATES:-buzz-acp buzz-agent buzz-dev-mcp}

say() { printf '==> %s\n' "$*"; }

if [ -f "$REF_FILE" ] && [ "$(cat "$REF_FILE")" = "$BUZZ_REF" ]; then
  installed=1
  for crate in $CRATES; do
    [ -x "$OUT_DIR/$crate" ] || installed=0
  done
  if [ "$installed" -eq 1 ]; then
    say "$BUZZ_REF is already installed — nothing to do"
    exit 0
  fi
fi

say "build dependencies"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git curl build-essential pkg-config libssl-dev \
    protobuf-compiler clang
else
  echo "no apt-get: install git, a C toolchain, pkg-config, OpenSSL headers," >&2
  echo "protobuf-compiler and clang yourself, then re-run." >&2
fi

say "rust toolchain"
# rust-toolchain.toml in the source pins the channel, so rustup fetches the
# right one on the first cargo invocation; only rustup itself is needed here.
if ! command -v cargo >/dev/null; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path
fi
# shellcheck disable=SC1091
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

say "source @ $BUZZ_REF"
if [ ! -d "$SRC_DIR/.git" ]; then
  # blob:none keeps the clone small; the build only needs one revision's trees.
  git clone --filter=blob:none "$BUZZ_REPO" "$SRC_DIR"
fi
git -C "$SRC_DIR" fetch --tags origin
git -C "$SRC_DIR" switch --detach "$BUZZ_REF"

say "cargo build --release (slow on first run; slower on arm64)"
build_args=""
for crate in $CRATES; do build_args="$build_args -p $crate"; done
# shellcheck disable=SC2086
(cd "$SRC_DIR" && cargo build --release $build_args)

say "install into $OUT_DIR"
install -d -m 0755 "$OUT_DIR"
for crate in $CRATES; do
  install -m 0755 -o root -g root "$SRC_DIR/target/release/$crate" "$OUT_DIR/$crate"
done
install -d -m 0755 "$(dirname "$REF_FILE")"
printf '%s\n' "$BUZZ_REF" > "$REF_FILE"

say "verify"
# A binary built for the wrong architecture installs fine and fails only at
# exec time; catch it here rather than inside a systemd unit at 3am.
"$OUT_DIR/buzz-acp" --help >/dev/null 2>&1 \
  || { echo "    buzz-acp does not run here (wrong architecture?)" >&2; exit 1; }

say "installed $BUZZ_REF:$(for c in $CRATES; do printf ' %s' "$c"; done)"
