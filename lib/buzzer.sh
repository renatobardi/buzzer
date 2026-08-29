#!/usr/bin/env bash
# Pure helpers shared by install.sh and the tests. No side effects on source.

# The relay is reached over a WebSocket; a http:// URL is the most common
# paste-o and fails much later, inside buzz-acp, with a worse message.
validate_relay_url() {
  case ${1-} in
    ws://?*|wss://?*) return 0 ;;
    *) return 1 ;;
  esac
}

require_env() {
  local name=${1:?}
  [ -n "${!name-}" ]
}

# Secrets reach logs by accident, never on purpose. Keep enough to recognise
# which key it was, not enough to use it.
redact() {
  printf '%.8s…(redacted)\n' "${1-}"
}
