#!/usr/bin/env bash
# Plain-shell test runner: no framework, runs identically on macOS and CI.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
# shellcheck source=lib/buzzer.sh
. lib/buzzer.sh
# shellcheck source=tests/env.sh
. tests/env.sh
export FOO_SET FOO_EMPTY
env_check_set() { require_env "$1"; }

fails=0
ok()   { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

check() { # name, expected_status, command...
  local name=$1 want=$2; shift 2
  "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" -eq "$want" ]; then ok "$name"; else fail "$name (want exit $want, got $got)"; fi
}

echo "validate_relay_url"
check "accepts ws://"                0 validate_relay_url "ws://10.0.0.1:3000"
check "accepts wss://"               0 validate_relay_url "wss://buzz.example.org"
check "rejects http://"              1 validate_relay_url "http://10.0.0.1:3000"
check "rejects bare host"            1 validate_relay_url "10.0.0.1:3000"
check "rejects empty"                1 validate_relay_url ""

echo "require_env"
check "passes when set"              0 env_check_set FOO_SET
check "fails when unset"             1 env_check_set FOO_UNSET
check "fails when empty"             1 env_check_set FOO_EMPTY

echo "redact"
if [ "$(redact "nsec1qqqqqqqqqqqqqqqqqqzzzz")" = "nsec1qqq…(redacted)" ]; then
  ok "keeps prefix only"
else
  fail "keeps prefix only"
fi

[ "$fails" -eq 0 ] || { printf '\n%d failing\n' "$fails"; exit 1; }
printf '\nall passing\n'
