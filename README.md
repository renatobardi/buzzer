# buzzer

Run [Buzz](https://github.com/block/buzz) agent personas on a machine that stays
on — created and edited from the Buzz Desktop UI, executed by systemd somewhere
else.

Buzz Desktop runs a managed agent as a child process of the app, so the agent
lives and dies with your laptop. But the agent was never really the process — in
Buzz an agent *is* a Nostr keypair, and its identity, history and presence live
on the relay. Move the key to a machine that doesn't sleep and the same agent
keeps working, with the same name and the same history, laptop closed.

What this repo adds is the part that is easy to get wrong: keeping the desktop
in charge. Buzz specifies a **backend provider** contract — the desktop
discovers any executable named `buzz-backend-<id>` and delegates deployment to
it. `buzz-backend-lxc` is such a provider. You create the agent in the UI as
usual, pick **Run on: lxc**, and it lands as a systemd unit on your host.

```
Buzz Desktop ──deploy──→ buzz-backend-lxc ──ssh──→ your host
     │                                              buzz-agent@presenter
     │                                              buzz-agent@pesquisa
     └──────────────── your relay ←─────────────────┘
```

## Two halves

**The substrate** — a machine with the binaries, an unprivileged `buzz` user, and
a templated systemd unit. One machine hosts as many personas as its memory
allows; each is a unit instance with its own identity, workspace and caps.

**The provider** — a single Python file on your Mac. The desktop finds it, calls
it, and hands it the agent's configuration and key.

## Install

### On the host

```bash
BUZZ_REF=<a block/buzz commit sha> sudo ./scripts/build-binaries.sh
sudo ./scripts/install-substrate.sh
```

The build is the slow part of a first run: it compiles `buzz-acp`, `buzz-agent`
and `buzz-dev-mcp` from source, because block/buzz publishes no Linux release of
them. Pin `BUZZ_REF` to a commit — Buzz is pre-1.0 with near-daily releases, and
a moving ref means the next run installs different software without telling you.

If the host is an LXC container, build somewhere disposable and copy the three
binaries in; the container then carries no toolchain and no build cache.

### On your Mac

```bash
ln -sf "$PWD/providers/buzz-backend-lxc/buzz_backend_lxc.py" \
       ~/.local/bin/buzz-backend-lxc
```

That is the whole installation. The desktop scans its own directory, `PATH`, and
`~/.local/bin` for `buzz-backend-*`; there is nothing to register.

Optionally, prefill the deploy form with your own topology — the code carries
none, so it can live in a public repo:

```bash
mkdir -p ~/.config/buzz-backend-lxc
cat > ~/.config/buzz-backend-lxc/defaults.json <<'JSON'
{"host": "my-lxd-host", "container": "runtime-buzz-prd"}
JSON
```

## Creating a persona

In Buzz Desktop: new agent, set its name, prompt, model and parallelism as
usual, choose harness **buzz-agent**, add `OPENROUTER_API_KEY` to its
environment variables, pick **Run on: lxc**, fill in the instance slug, deploy.

The desktop will warn you that the provider receives the agent's private key.
It does — that is the job of a provider, and the warning is the contract working
as designed.

Then, on the host:

```bash
systemctl status buzz-agent@presenter
journalctl -u buzz-agent@presenter -f
```

## What the desktop does and does not give you

Deployment, configuration and identity come from the UI. **Observation does
not.** The protocol is explicit that the desktop keeps no management channel to
a deployed agent: status is relay presence, stopping is a `!shutdown` message on
the relay, and reconfiguring is a redeploy. There is no remote log view and no
exec. Real debugging is `journalctl` over ssh.

Presence is a renewed lease, so an agent whose host dies without ceremony can
look online for up to ~90 seconds. Use `systemctl`, not the green dot, to answer
"is it running".

## One key, one body

If an agent is also enabled as a managed agent in Buzz Desktop, turn it off
there. Two processes signing with the same key both answer the same mention, and
the channel shows the agent talking over itself.

## Secrets

`BUZZ_PRIVATE_KEY` is the agent's identity: whoever holds it can post as the
agent, permanently, in your workspace's history. It arrives in the deploy
payload, lands in `/etc/buzz/agents/<slug>.env` at mode `0600`, and is read by
systemd. It is never in this repo, never on a command line, and never in a log.

The provider requests no credential of its own. A backend provider that asks you
for an API key in its own config form is violating the contract.

## Conformance

The provider implements the [L1] launcher and [L2] provider obligations from
`docs/remote-agents.md` in block/buzz. The ones with teeth:

- **Identity fail-closed.** A deploy with no private key, no relay URL, or
  neither an auth tag nor an owner pubkey is refused rather than launched.
- **Identity from top-level fields only.** A `BUZZ_PRIVATE_KEY` smuggled through
  the agent's environment variables is dropped, never merged.
- **Presence cannot be suppressed.** `BUZZ_ACP_NO_PRESENCE` is stripped: it is
  the only status signal a remote agent has.
- **No restart of a clean exit.** The unit uses `Restart=on-failure`;
  `Restart=always` is explicitly non-conforming, because it would resurrect an
  agent that was deliberately shut down.
- **Signals reach the harness.** `ExecStart` is `buzz-acp` itself — no wrapper,
  no shell, so termination reaches the process that must handle it.
- **A redeploy that changes nothing mutates nothing.** A live unit with a
  byte-identical environment is a strict no-op.
- **The fence.** The provider only overwrites env files carrying its own
  management marker, so a hand-written unit is never clobbered.

`buzz-agent` has no built-in tools — everything it does runs through MCP over
stdio, and `buzz-acp` passes exactly one MCP server. Without one configured, a
persona can talk and nothing else.

## Development

```bash
python3 -m pytest tests/ -q
shellcheck scripts/*.sh
```

The provider's shebang is `/usr/bin/python3` on purpose: an absolute path that
exists on every macOS, because the desktop may launch it from launchd's minimal
`PATH`. That interpreter is Python 3.9, so the provider stays 3.9-compatible
even though the tests run on newer.

## License

MIT. Not affiliated with Block. The binaries are built from
[block/buzz](https://github.com/block/buzz) under its own license.
