#!/usr/bin/python3
"""Buzz Desktop backend provider that deploys agents as systemd units in LXC.

Buzz Desktop discovers any executable named `buzz-backend-<id>` on the exe
directory, PATH, or ~/.local/bin, and speaks one JSON object in on stdin, one
out on stdout (block/buzz `docs/remote-agents.md`). Symlink this file as
`~/.local/bin/buzz-backend-lxc` and the desktop offers "Run on: lxc".

The obligations implemented here are the [L1] launcher and [L2] provider
conformance lists in that document; the code names the clause it satisfies
rather than restating the rule.

Interpreter: the shebang is `/usr/bin/python3` on purpose — an absolute path
that exists on every macOS, because the desktop may launch this from launchd's
minimal PATH. That interpreter is 3.9, older than the repo's own floor, so
**this file must stay 3.9-compatible** even though the tests run on 3.10+.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import unicodedata

PROTOCOL_VERSION = 1
VERSION = "1.0.0"

BIN_DIR = "/usr/local/bin"
ENV_DIR = "/etc/buzz/agents"
PROMPT_DIR = "/etc/buzz/prompts"
WORKSPACE_DIR = "/srv/agents"

# The harnesses the container actually has, from build-buzz-binaries.sh. A
# deploy naming anything else is refused here rather than left to fail at exec
# time inside a unit nobody is watching.
KNOWN_HARNESSES = ("buzz-agent",)

# The auto-repair fence (L2.4): destructive writes only ever touch env files
# carrying this marker, so a hand-written unit is never clobbered.
MANAGEMENT_MARKER = "# managed-by: buzz-backend-lxc"

# Identity comes from the top-level payload fields, never from env_vars —
# reading env_vars for these yields an identityless agent, and a user-supplied
# value must not be able to reconstruct one.
RESERVED_KEYS = (
    "BUZZ_PRIVATE_KEY",
    "NOSTR_PRIVATE_KEY",
    "BUZZ_AUTH_TAG",
    "BUZZ_RELAY_URL",
    # Reserved to stop a user env from defeating protocol guarantees: presence
    # is the only remote status signal (L1.2), and the inactivity knob carries
    # the owner's deliberate lifetime policy (I5), not an accidental
    # passthrough.
    "BUZZ_ACP_NO_PRESENCE",
    "BUZZ_ACP_EXIT_AFTER_INACTIVITY",
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Topology belongs to the operator, not to this file: the code carries no host
# or container name so it can be shared, and the operator's values prefill the
# desktop's form from here. Only these keys are read — a site file can never
# introduce a field, least of all a credential-shaped one (I2).
SITE_DEFAULTS_PATH = os.path.expanduser(
    "~/.config/buzz-backend-lxc/defaults.json")
SITE_DEFAULT_KEYS = ("host", "container", "channels")


class DeployRefused(Exception):
    """A deploy that must not proceed. The message reaches the user's screen."""


def load_site_defaults(path=None):
    """The operator's own defaults, or nothing.

    A missing or corrupt file yields no defaults rather than an error: `info`
    is what gates the nsec handoff, so it must always answer.
    """
    try:
        with open(str(path or SITE_DEFAULTS_PATH)) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items()
            if k in SITE_DEFAULT_KEYS and isinstance(v, (str, int))}


def info(defaults_path=None):
    # Field order in the desktop's form is alphabetical by property key, not
    # schema order (observed, 0.5.20) — the names below are what the user reads
    # top to bottom, so they are chosen to sort into a sane sequence.
    site = load_site_defaults(defaults_path)
    return {
        "ok": True,
        "name": "lxc",
        "version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "description": "Runs agents as systemd units in an LXC container",
        "config_schema": {
            "type": "object",
            "required": ["host", "container"],
            "properties": {
                "channels": {
                    "type": "string",
                    "title": "Channels",
                    "default": site.get("channels", ""),
                    "description": "Comma-separated. Empty means every channel "
                                   "the agent is a member of.",
                },
                "container": {
                    "type": "string",
                    "title": "Container",
                    "default": site.get("container", ""),
                    "description": "Name of the LXC container that holds the "
                                   "buzz-acp binaries.",
                },
                "host": {
                    "type": "string",
                    "title": "SSH host",
                    "default": site.get("host", ""),
                    "description": "ssh alias or address of the LXD host.",
                },
                "slug": {
                    "type": "string",
                    "title": "Instance slug",
                    "description": "systemd instance name: buzz-agent@<slug>. "
                                   "Leave blank to derive it from the agent's "
                                   "name. Lowercase, no spaces.",
                },
            },
        },
    }


def slug_from_name(name):
    """A systemd instance name derived from the agent's display name.

    The desktop already sends the name, and it does not re-expose
    provider_config when an agent is edited — so a slug the operator forgot at
    creation could only be fixed by deleting the agent. Deriving one removes
    that trap.
    """
    # Strip accents to their base letter rather than to a separator: personas
    # get named in the operator's own language, and "Ação" deserves "acao",
    # not "a-o".
    decomposed = unicodedata.normalize("NFKD", (name or "").lower())
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    slug = "".join(c if ("a" <= c <= "z" or "0" <= c <= "9") else "-"
                   for c in folded)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:64].rstrip("-")
    if not slug:
        raise DeployRefused(
            "cannot derive an instance name from %r — set one explicitly in "
            "the provider's Instance slug field" % (name,)
        )
    return validate_slug(slug)


def validate_slug(slug):
    """Reject anything that could escape the paths built from it."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise DeployRefused(
            "invalid instance slug: must be 1-64 chars, lowercase letters, "
            "digits, '-' or '_', starting with a letter or digit"
        )
    return slug


def resolve_harness(agent):
    """The ACP agent the harness spawns, as an absolute path in the container.

    `launch.command` is the normative source; `agent_command` is the legacy
    field kept for builds that do not emit the launch block.
    """
    launch = agent.get("launch") or {}
    command = launch.get("command") or agent.get("agent_command") or ""
    name = command.rsplit("/", 1)[-1]
    if name not in KNOWN_HARNESSES:
        raise DeployRefused(
            "this container does not have the harness '%s'. It provides: %s. "
            "Change the agent's harness in Buzz Desktop, or install that "
            "binary in the container first." % (name, ", ".join(KNOWN_HARNESSES))
        )
    return "%s/%s" % (BIN_DIR, name)


def system_prompt_of(agent):
    """The effective system prompt, or '' when the agent has none."""
    launch = agent.get("launch") or {}
    policy = launch.get("policy_env") or {}
    return policy.get("BUZZ_ACP_SYSTEM_PROMPT") or agent.get("system_prompt") or ""


def build_env(agent, config):
    """The agent's environment, in the precedence the contract requires.

    Order, weakest to strongest: user env_vars, then the desktop-resolved
    policy_env, then identity. Identity is last because nothing may override it.
    """
    given = (config.get("slug") or "").strip()
    slug = validate_slug(given) if given else slug_from_name(agent.get("name"))
    nsec = agent.get("private_key_nsec") or ""
    relay_url = agent.get("relay_url") or ""
    # I1: refuse rather than launch identityless.
    if not nsec:
        raise DeployRefused("refusing to deploy: the agent has no private key")
    if not relay_url:
        raise DeployRefused("refusing to deploy: the agent has no relay URL")

    launch = agent.get("launch") or {}
    auth_tag = agent.get("auth_tag") or ""
    owner_pubkey = launch.get("owner_pubkey") or ""
    # L2.3: refuse a deploy that resolves neither.
    if not auth_tag and not owner_pubkey:
        raise DeployRefused(
            "refusing to deploy: neither an auth tag nor an owner pubkey "
            "resolved, so the relay cannot authorize this agent"
        )

    env = {}

    # Weakest tier: what the user typed into Environment variables. Reserved
    # keys are dropped here, not overwritten later, so a smuggled key cannot
    # survive into the file at all.
    for key, value in (agent.get("env_vars") or {}).items():
        if key in RESERVED_KEYS:
            continue
        env[key] = value

    # The desktop already resolved persona, model and knobs into policy_env.
    # This provider materializes that; it does not re-derive it.
    for key, value in (launch.get("policy_env") or {}).items():
        if key in RESERVED_KEYS:
            continue
        env[key] = value

    # A multiline prompt has no portable form in a systemd EnvironmentFile, so
    # it lives in its own file and the env points at it.
    if "BUZZ_ACP_SYSTEM_PROMPT" in env:
        del env["BUZZ_ACP_SYSTEM_PROMPT"]
    if system_prompt_of(agent):
        env["BUZZ_ACP_SYSTEM_PROMPT_FILE"] = prompt_path(slug)

    env["BUZZ_ACP_AGENT_COMMAND"] = resolve_harness(agent)
    args = launch.get("args") or agent.get("agent_args") or []
    if args:
        env["BUZZ_ACP_AGENT_ARGS"] = ",".join(args)

    # An empty channel list is not the same as no channel list: it would scope
    # the agent to a single channel whose name is the empty string.
    channels = (config.get("channels") or "").strip()
    if channels:
        env["BUZZ_ACP_CHANNELS"] = channels

    # Strongest tier: identity, from top-level fields only.
    env["BUZZ_PRIVATE_KEY"] = nsec
    env["NOSTR_PRIVATE_KEY"] = nsec
    env["BUZZ_RELAY_URL"] = relay_url
    if auth_tag:
        env["BUZZ_AUTH_TAG"] = auth_tag

    return env


def prompt_path(slug):
    return "%s/%s.md" % (PROMPT_DIR, validate_slug(slug))


def env_path(slug):
    return "%s/%s.env" % (ENV_DIR, validate_slug(slug))


def render_env_file(env):
    """A systemd EnvironmentFile. Values are single-quoted, POSIX-escaped."""
    lines = [MANAGEMENT_MARKER]
    for key in sorted(env):
        value = "" if env[key] is None else str(env[key])
        if "\n" in value or "\r" in value:
            raise DeployRefused(
                "refusing to write %s: a newline in an EnvironmentFile value "
                "truncates silently" % key
            )
        lines.append("%s=%s" % (key, shlex.quote(value)))
    return "\n".join(lines) + "\n"


def handle(request):
    """Dispatch one request. Returns (response, exit_code).

    Exit code carries exactly one bit: zero means the response is trustworthy.
    An in-band `{"ok": false}` therefore exits 0 — a nonzero exit makes the
    desktop discard stdout entirely and show its own generic failure instead.
    """
    op = request.get("op") if isinstance(request, dict) else None
    if op == "info":
        return info(), 0
    if op == "deploy":
        try:
            return deploy(request), 0
        except DeployRefused as error:
            return {"ok": False, "error": str(error)}, 0
    return {"ok": False, "error": "unsupported op: %r" % (op,)}, 0


def deploy(request):
    agent = request.get("agent") or {}
    config = request.get("provider_config") or {}
    given = (config.get("slug") or "").strip()
    slug = validate_slug(given) if given else slug_from_name(agent.get("name"))
    host = config.get("host") or ""
    container = config.get("container") or ""
    if not host or not container:
        raise DeployRefused("host and container are required")

    env = build_env(agent, config)
    rendered = render_env_file(env)
    prompt = system_prompt_of(agent)

    remote = Remote(host, container)
    remote.require_binaries(env["BUZZ_ACP_AGENT_COMMAND"])

    # I4: a live unit with the same identity is a strict no-op — zero mutation.
    if remote.is_live(slug) and remote.env_matches(slug, rendered):
        return {"ok": True, "agent_id": agent_id(slug, container)}

    remote.write_files(slug, rendered, prompt)
    remote.enable(slug)
    # Success only on a confirmed start, never on "accepted".
    if not remote.is_live(slug):
        raise DeployRefused(
            "buzz-agent@%s did not stay running. Check it with: "
            "ssh %s \"lxc exec %s -- journalctl -u buzz-agent@%s -n 50\""
            % (slug, host, container, slug)
        )
    return {"ok": True, "agent_id": agent_id(slug, container)}


def agent_id(slug, container):
    return "%s@%s" % (slug, container)


class Remote(object):
    """Everything this provider does to the substrate, over ssh.

    There is no persistent management channel (M1): each call is one ssh
    invocation, and nothing here is kept open after deploy returns.
    """

    def __init__(self, host, container, runner=None):
        self.host = host
        self.container = container
        self._run = runner or self._ssh

    def _ssh(self, script, stdin=None):
        # Absolute path: under launchd the PATH may not contain ssh.
        # -n when nothing is piped, so the terminal's stdin stays out of the
        # remote command; -T otherwise, because the payload goes over stdin and
        # a pty would corrupt it.
        argv = ["/usr/bin/ssh", "-n" if stdin is None else "-T", self.host,
                "lxc exec %s -- bash -lc %s" % (self.container, shlex.quote(script))]
        return subprocess.run(
            argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, check=False,
        )

    def require_binaries(self, harness):
        result = self._run("test -x %s && test -x %s/buzz-acp"
                           % (shlex.quote(harness), BIN_DIR))
        if result.returncode != 0:
            raise DeployRefused(
                "%s is missing buzz-acp or %s — run build-buzz-binaries.sh first"
                % (self.container, harness)
            )

    def is_live(self, slug):
        return self._run("systemctl is-active --quiet buzz-agent@%s" % slug).returncode == 0

    def env_matches(self, slug, rendered):
        """True when the deployed env file is byte-identical and ours."""
        result = self._run("cat %s 2>/dev/null" % shlex.quote(env_path(slug)))
        current = result.stdout or ""
        return MANAGEMENT_MARKER in current and current == rendered

    def write_files(self, slug, rendered, prompt):
        existing = self._run("cat %s 2>/dev/null" % shlex.quote(env_path(slug)))
        # The fence: only overwrite what this provider wrote.
        if existing.returncode == 0 and existing.stdout \
                and MANAGEMENT_MARKER not in existing.stdout:
            raise DeployRefused(
                "%s exists and was not created by this provider — refusing to "
                "overwrite it" % env_path(slug)
            )
        # The env file carries the nsec, so it is written through stdin and
        # created with a restrictive mode before any content lands in it.
        script = (
            "install -d -o buzz -g buzz -m 0750 {ws}/{slug} && "
            "install -m 0600 -o buzz -g buzz /dev/null {envf} && "
            "cat > {envf}"
        ).format(ws=WORKSPACE_DIR, slug=slug, envf=env_path(slug))
        result = self._run(script, stdin=rendered)
        if result.returncode != 0:
            raise DeployRefused("could not write the agent's environment file")
        if prompt:
            result = self._run("cat > %s && chmod 0644 %s"
                               % (prompt_path(slug), prompt_path(slug)), stdin=prompt)
            if result.returncode != 0:
                raise DeployRefused("could not write the system prompt file")

    def enable(self, slug):
        # restart, not start: a redeploy of a changed config must pick it up.
        result = self._run("systemctl enable buzz-agent@%s && "
                           "systemctl restart buzz-agent@%s" % (slug, slug))
        if result.returncode != 0:
            raise DeployRefused("systemd refused to start buzz-agent@%s" % slug)


def main():
    try:
        request = json.load(sys.stdin)
    except ValueError:
        json.dump({"ok": False, "error": "request was not valid JSON"}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    response, code = handle(request)
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
