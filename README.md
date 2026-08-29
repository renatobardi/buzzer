# buzzer

A [Buzz](https://github.com/block/buzz) agent runtime you can run on a machine
that stays on: `buzz-acp` + Claude Code, under systemd.

Buzz Desktop runs an agent as a child process of the app, so the agent lives and
dies with your laptop. But the agent was never really the process — in Buzz, an
agent *is* a Nostr keypair, and its identity, history and presence live on the
relay. Move the key to a machine that doesn't sleep and the same agent keeps
working, with the same name and the same history, laptop closed.

That's all this repo does: install the harness, point it at your relay, and keep
it running.

```
Buzz Desktop ─┐
              ├─→  your relay  ←──  buzzer (buzz-acp → claude-agent-acp)
mobile ───────┘
```

## Install

Debian 12/13, on anything that can reach your relay — LXC, VM, VPS.

```bash
git clone https://github.com/renatobardi/buzzer.git
cd buzzer
sudo ./install.sh
```

It installs Node, Claude Code, the ACP adapter, and builds `buzz-acp` and
`buzz-admin` from `block/buzz` (pinned with `BUZZ_REF=`, default `main`). The
Rust build is the slow part of a first run — several minutes, and it wants a
couple of GB of scratch space. Everything is idempotent; re-run it to upgrade.

Then fill in `/etc/buzzer/environment` and start it:

```bash
sudo systemctl start buzzer && journalctl -u buzzer -f
```

### Giving the agent a browser

```bash
sudo ./install.sh --with-browser
```

Adds headless Chromium and Playwright MCP, and then you point
`BUZZ_ACP_MCP_COMMAND` at it. The agent can navigate, click, fill forms and
screenshot — which is how it *sees* a page. What you don't get is a desktop to
watch over VNC; if that's what you want, look at
[buzznode](https://github.com/pdparchitect/buzznode) instead, which trades ~3 GB
of image for a full graphical body.

## Getting the agent onto the relay

Two things, and the second one is easy to miss:

1. **A keypair.** Buzz Desktop's agent setup mints one, or `buzz-admin
   generate-key` does. The secret key goes in `BUZZ_PRIVATE_KEY` and nowhere
   else. It is not recoverable.
2. **Relay membership.** The agent's *public* key must be registered on the
   relay before it can read or publish:

   ```bash
   BUZZ_RELAY_PRIVATE_KEY=<relay signing key> \
     buzz-admin add-member --pubkey <agent pubkey>
   ```

   This needs `BUZZ_RELAY_PRIVATE_KEY` set in the relay's own environment (it
   ships commented out in Buzz's `.env`) and the relay restarted first.

## One key, one body

If the agent is also configured as a managed agent in Buzz Desktop, turn it off
there. Two processes signing with the same key both answer the same mention, and
the channel shows the agent talking over itself.

## Secrets

`BUZZ_PRIVATE_KEY` is the agent's identity: whoever holds it can post as the
agent, permanently, in your workspace's history. It lives in
`/etc/buzzer/environment`, `0640 root:buzzer`, read by systemd — never in this
repo, never on a command line, never in a log.

## Development

```bash
./tests/run.sh
shellcheck -x install.sh lib/buzzer.sh tests/run.sh
```

## License

MIT. Not affiliated with Block. `buzz-acp` and `buzz-admin` are built from
[block/buzz](https://github.com/block/buzz) under its own license.
