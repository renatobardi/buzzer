"""Conformance tests for the buzz-backend-lxc provider.

The obligations tested here are not ours to choose: they come from [L1] and
[L2] in block/buzz `docs/remote-agents.md`, plus the payload shape observed
from Buzz Desktop 0.5.20 (captured during the spike, redacted).
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDER = REPO_ROOT / "providers" / "buzz-backend-lxc" / "buzz_backend_lxc.py"


def load_provider():
    spec = importlib.util.spec_from_file_location("buzz_backend_lxc", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provider = load_provider()


def payload(**overrides):
    """A deploy payload shaped like the one Buzz Desktop 0.5.20 actually sends."""
    agent = {
        "name": "Presenter",
        "relay_url": "wss://relay.example.com",
        "private_key_nsec": "nsec1" + "a" * 58,
        "auth_tag": "t" * 209,
        "agent_command": "buzz-agent",
        "agent_args": [],
        "model": "anthropic/claude-sonnet-4.5",
        "provider": "openrouter",
        "parallelism": 2,
        "respond_to": "owner-only",
        "respond_to_allowlist": [],
        "system_prompt": "you make self-contained HTML decks",
        "turn_timeout_seconds": 320,
        "idle_timeout_seconds": None,
        "max_turn_duration_seconds": None,
        "env_vars": {"OPENROUTER_API_KEY": "test-not-a-real-key"},
        "launch": {
            "command": "buzz-agent",
            "args": [],
            "env": {},
            "owner_pubkey": "e" * 64,
            "policy_env": {
                "BUZZ_ACP_DISPLAY_NAME": "Presenter",
                "BUZZ_ACP_AGENTS": "2",
                "BUZZ_ACP_SYSTEM_PROMPT": "you make self-contained HTML decks",
            },
        },
    }
    agent.update(overrides)
    return {
        "op": "deploy",
        "request_id": "r1",
        "agent": agent,
        "provider_config": {
            "host": "lxd-host.example",
            "container": "buzz-runtime.example",
            "slug": "presenter",
            "channels": "",
        },
    }


class TestInfo:
    def test_declares_protocol_version_1(self):
        # A missing protocol_version is an error at the desktop's pre-secret
        # gate, not a presumed 1 — the nsec never crosses without it.
        assert provider.info()["protocol_version"] == 1

    def test_config_schema_requires_the_fields_deploy_cannot_infer(self):
        schema = provider.info()["config_schema"]
        assert set(schema["required"]) <= set(schema["properties"])
        assert "container" in schema["properties"]
        assert "host" in schema["properties"]

    def test_schema_declares_no_credential_field(self):
        # I2 / L2.2: a provider never requests credentials through
        # provider_config.
        secretish = {"secret", "password", "token", "key", "credential",
                     "passphrase", "auth", "nsec"}
        for name in provider.info()["config_schema"]["properties"]:
            words = set(name.lower().replace("-", "_").split("_"))
            assert not (words & secretish), f"{name} looks credential-shaped"


class TestIdentity:
    def test_identity_env_comes_from_top_level_fields(self):
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert env["BUZZ_PRIVATE_KEY"] == "nsec1" + "a" * 58
        assert env["BUZZ_RELAY_URL"] == "wss://relay.example.com"
        assert env["BUZZ_AUTH_TAG"] == "t" * 209

    def test_reserved_keys_in_env_vars_never_win(self):
        # Reserved-key rule: reading env_vars for identity yields an
        # identityless agent, so a smuggled key must not override.
        agent = payload()["agent"]
        agent["env_vars"] = {
            "BUZZ_PRIVATE_KEY": "nsec1attacker",
            "BUZZ_RELAY_URL": "wss://evil.example",
            "BUZZ_AUTH_TAG": "forged",
        }
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_PRIVATE_KEY"] == "nsec1" + "a" * 58
        assert env["BUZZ_RELAY_URL"] == "wss://relay.example.com"
        assert env["BUZZ_AUTH_TAG"] == "t" * 209

    @pytest.mark.parametrize("missing", ["private_key_nsec", "relay_url"])
    def test_refuses_to_build_an_identityless_agent(self, missing):
        # I1: no agent is ever launched with an empty or missing private key.
        agent = payload()["agent"]
        agent[missing] = ""
        with pytest.raises(provider.DeployRefused):
            provider.build_env(agent, payload()["provider_config"])

    def test_refuses_when_neither_auth_tag_nor_owner_pubkey_resolves(self):
        agent = payload()["agent"]
        agent["auth_tag"] = ""
        agent["launch"] = dict(agent["launch"], owner_pubkey="")
        with pytest.raises(provider.DeployRefused):
            provider.build_env(agent, payload()["provider_config"])

    def test_owner_pubkey_alone_is_enough(self):
        agent = payload()["agent"]
        agent["auth_tag"] = ""
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_PRIVATE_KEY"]


class TestLaunchData:
    def test_policy_env_is_materialized_not_reconstructed(self):
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert env["BUZZ_ACP_DISPLAY_NAME"] == "Presenter"
        assert env["BUZZ_ACP_AGENTS"] == "2"

    def test_user_env_vars_are_kept(self):
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert env["OPENROUTER_API_KEY"] == "test-not-a-real-key"

    def test_policy_env_wins_over_user_env_vars(self):
        agent = payload()["agent"]
        agent["env_vars"] = dict(agent["env_vars"], BUZZ_ACP_AGENTS="99")
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_ACP_AGENTS"] == "2"

    def test_presence_can_never_be_suppressed(self):
        # L1.2: remotely, presence is the only status signal.
        agent = payload()["agent"]
        agent["env_vars"] = dict(agent["env_vars"], BUZZ_ACP_NO_PRESENCE="1")
        env = provider.build_env(agent, payload()["provider_config"])
        assert "BUZZ_ACP_NO_PRESENCE" not in env

    def test_channels_from_provider_config_reach_the_harness(self):
        config = dict(payload()["provider_config"], channels="infra,alertas")
        env = provider.build_env(payload()["agent"], config)
        assert env["BUZZ_ACP_CHANNELS"] == "infra,alertas"

    def test_empty_channels_is_omitted_not_blank(self):
        # An empty BUZZ_ACP_CHANNELS is not the same as an absent one: it would
        # scope the agent to a channel list of one empty name.
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert "BUZZ_ACP_CHANNELS" not in env


class TestHarnessGate:
    def test_accepts_the_harness_the_container_has(self):
        agent = payload()["agent"]
        assert provider.resolve_harness(agent) == "/usr/local/bin/buzz-agent"

    def test_refuses_a_harness_the_container_does_not_have(self):
        # Failing here beats failing inside a systemd unit at 3am.
        agent = payload()["agent"]
        agent["launch"] = dict(agent["launch"], command="claude-agent-acp")
        agent["agent_command"] = "claude-agent-acp"
        with pytest.raises(provider.DeployRefused) as excinfo:
            provider.resolve_harness(agent)
        assert "claude-agent-acp" in str(excinfo.value)

    def test_launch_command_wins_over_legacy_agent_command(self):
        agent = payload()["agent"]
        agent["agent_command"] = "claude-agent-acp"
        assert provider.resolve_harness(agent) == "/usr/local/bin/buzz-agent"


class TestEnvFileRendering:
    def test_quotes_values_so_a_prompt_cannot_break_out(self):
        rendered = provider.render_env_file({"B": "has space's and \"quotes\""})
        line = [ln for ln in rendered.splitlines() if ln.startswith("B=")][0]
        assert line.startswith("B='") and line.endswith("'")

    def test_one_line_per_variable(self):
        rendered = provider.render_env_file({"A": "1", "B": "2"})
        assert len([ln for ln in rendered.splitlines() if "=" in ln]) == 2

    def test_carries_the_management_marker(self):
        # The auto-repair fence: destructive writes only ever touch units this
        # provider created.
        rendered = provider.render_env_file({"A": "1"})
        assert provider.MANAGEMENT_MARKER in rendered

    def test_refuses_newlines_in_values(self):
        # systemd EnvironmentFile has no portable multiline form; a value with a
        # newline would silently truncate. Prompts go to a file instead.
        with pytest.raises(provider.DeployRefused):
            provider.render_env_file({"P": "line one\nline two"})


class TestSystemPrompt:
    def test_prompt_goes_to_a_file_not_into_the_env(self):
        agent = provider_payload_with_multiline_prompt()
        env = provider.build_env(agent, {"host": "h", "container": "c", "slug": "presenter"})
        assert "BUZZ_ACP_SYSTEM_PROMPT" not in env
        assert env["BUZZ_ACP_SYSTEM_PROMPT_FILE"] == "/etc/buzz/prompts/presenter.md"

    def test_prompt_text_is_recoverable_for_writing(self):
        agent = provider_payload_with_multiline_prompt()
        assert provider.system_prompt_of(agent).endswith("second line")


def provider_payload_with_multiline_prompt():
    agent = payload()["agent"]
    agent["system_prompt"] = "first line\nsecond line"
    agent["launch"] = dict(agent["launch"],
                           policy_env=dict(agent["launch"]["policy_env"],
                                           BUZZ_ACP_SYSTEM_PROMPT="first line\nsecond line"))
    return agent


class TestSlug:
    @pytest.mark.parametrize("bad", ["../etc/passwd", "a b", "UPPER", "", "x" * 65])
    def test_rejects_unsafe_slugs(self, bad):
        with pytest.raises(provider.DeployRefused):
            provider.validate_slug(bad)

    @pytest.mark.parametrize("good", ["presenter", "vigia-infra", "a1"])
    def test_accepts_sane_slugs(self, good):
        assert provider.validate_slug(good) == good


class TestOutputHygiene:
    def test_errors_never_echo_secret_material(self):
        agent = payload()["agent"]
        agent["launch"] = dict(agent["launch"], command="claude-agent-acp")
        agent["agent_command"] = "claude-agent-acp"
        try:
            provider.resolve_harness(agent)
        except provider.DeployRefused as error:
            assert "nsec1" not in str(error)
            assert "test-not-a-real-key" not in str(error)

    def test_error_response_is_in_band_with_exit_zero(self):
        # Observed the hard way: a nonzero exit makes the desktop discard
        # stdout and show "provider failed (exit code 1, empty stderr)".
        response, code = provider.handle({"op": "nope"})
        assert response["ok"] is False
        assert code == 0

    def test_malformed_request_is_still_in_band(self):
        response, code = provider.handle({"no_op_key": True})
        assert response["ok"] is False
        assert code == 0

    def test_info_response_is_json_serialisable(self):
        json.dumps(provider.info())


class TestSiteDefaults:
    """Topology is the operator's, not the provider's.

    The code ships with no host or container name in it, so it can live in a
    public repo; the operator's own values come from a local config file that
    prefills the desktop's form.
    """

    def test_ships_with_no_topology_in_it(self):
        # Generic, so the check survives being read by anyone: the file must
        # carry no hostname, no domain and no address of its own.
        import re
        source = PROVIDER.read_text()
        for pattern, what in (
            (r"\b\d{1,3}(\.\d{1,3}){3}\b", "an IP address"),
            (r"wss?://[a-z0-9.-]+\.[a-z]{2,}", "a concrete relay URL"),
            (r"\.ts\.net", "a tailnet name"),
        ):
            for hit in re.findall(pattern, source):
                assert False, f"{what} is hardcoded: {hit}"

    def test_schema_defaults_are_empty_without_a_site_file(self, tmp_path):
        schema = provider.info(defaults_path=tmp_path / "absent.json")["config_schema"]
        assert schema["properties"]["host"].get("default", "") == ""
        assert schema["properties"]["container"].get("default", "") == ""

    def test_site_file_prefills_the_form(self, tmp_path):
        path = tmp_path / "defaults.json"
        path.write_text(json.dumps({"host": "myhost", "container": "mycontainer"}))
        schema = provider.info(defaults_path=path)["config_schema"]
        assert schema["properties"]["host"]["default"] == "myhost"
        assert schema["properties"]["container"]["default"] == "mycontainer"

    def test_unknown_site_keys_are_ignored(self, tmp_path):
        path = tmp_path / "defaults.json"
        path.write_text(json.dumps({"host": "myhost", "nonsense": "x"}))
        schema = provider.info(defaults_path=path)["config_schema"]
        assert "nonsense" not in schema["properties"]

    def test_a_corrupt_site_file_does_not_break_discovery(self, tmp_path):
        # info() answering is what gates the nsec handoff; a typo in a local
        # config file must degrade to empty defaults, never to no provider.
        path = tmp_path / "defaults.json"
        path.write_text("{not json")
        assert provider.info(defaults_path=path)["protocol_version"] == 1

    def test_site_file_can_never_inject_a_credential_field(self, tmp_path):
        path = tmp_path / "defaults.json"
        path.write_text(json.dumps({"api_key": "sk-secret", "host": "h"}))
        schema = provider.info(defaults_path=path)["config_schema"]
        assert "api_key" not in schema["properties"]
        assert json.dumps(schema).find("sk-secret") == -1


class TestMissingSlug:
    """A bad slug must reach the user as a message, not as a traceback.

    build_env is reachable with an unvalidated config, and an uncaught KeyError
    exits nonzero — which makes the desktop discard stdout and show its own
    generic "provider failed" instead of the reason.
    """

    def test_absent_slug_and_unusable_name_is_refused_not_a_keyerror(self):
        config = {"host": "h", "container": "c"}
        agent = dict(payload()["agent"], name="!!!")
        with pytest.raises(provider.DeployRefused):
            provider.build_env(agent, config)

    def test_unsafe_slug_is_refused_before_a_path_is_built(self):
        config = dict(payload()["provider_config"], slug="../../etc/passwd")
        with pytest.raises(provider.DeployRefused):
            provider.build_env(payload()["agent"], config)


class TestUnitFile:
    """The unit is protocol surface, so its directives are asserted, not assumed.

    systemd does not fail on a directive in the wrong section — it logs
    "Unknown key name" and carries on, so a misplaced rate limit protects
    nothing while looking like it does.
    """

    UNIT = REPO_ROOT / "systemd" / "buzz-agent@.service"

    def sections(self):
        out, current = {}, None
        for line in self.UNIT.read_text().splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                out[current] = []
            elif line and not line.startswith("#") and current:
                out[current].append(line)
        return out

    def test_start_rate_limiting_is_in_the_unit_section(self):
        # systemd.unit(5) documents these, not systemd.service(5).
        service = " ".join(self.sections().get("Service", []))
        unit = " ".join(self.sections().get("Unit", []))
        for key in ("StartLimitIntervalSec", "StartLimitBurst"):
            assert key not in service, f"{key} is silently ignored in [Service]"
            assert key in unit, f"{key} must be in [Unit] to take effect"

    def test_a_clean_exit_is_never_restarted(self):
        service = " ".join(self.sections()["Service"])
        assert "Restart=on-failure" in service
        assert "Restart=always" not in service

    def test_the_harness_is_exec_directly(self):
        # L1.3: a wrapper that swallows the termination signal conforms to
        # nothing.
        service = " ".join(self.sections()["Service"])
        assert "ExecStart=/usr/local/bin/buzz-acp" in service
        for wrapper in ("/bin/sh", "/bin/bash"):
            assert "ExecStart=%s" % wrapper not in service

    def test_presence_is_not_suppressed(self):
        assert "BUZZ_ACP_NO_PRESENCE" not in self.UNIT.read_text()


class TestDerivedSlug:
    """The desktop already sends the agent's name; asking for it again was a
    design mistake that cost a failed deploy.

    Worse, the desktop does not re-expose provider_config when editing an
    existing agent, so a missing slug could only be fixed by deleting the agent
    and recreating it. Deriving one removes the whole class of failure; the
    field stays available for when a different instance name is wanted.
    """

    @pytest.mark.parametrize("name,expected", [
        ("Jack (Presenter)", "jack-presenter"),
        ("Presenter", "presenter"),
        ("Vigia  de   Infra", "vigia-de-infra"),
        ("  Trailing and leading  ", "trailing-and-leading"),
        ("Añó Válido", "ano-valido"),
        ("Ação de Vendas", "acao-de-vendas"),
        ("2Fast", "2fast"),
        ("---weird---", "weird"),
    ])
    def test_derives_a_usable_slug_from_the_name(self, name, expected):
        assert provider.slug_from_name(name) == expected

    def test_derived_slug_is_always_valid(self):
        for name in ["Jack (Presenter)", "x" * 200, "9 lives", "Ção"]:
            assert provider.validate_slug(provider.slug_from_name(name))

    def test_a_name_with_nothing_usable_is_refused(self):
        # Better an explicit refusal than a path built from an empty string.
        for name in ["", "()", "   ", "!!!"]:
            with pytest.raises(provider.DeployRefused):
                provider.slug_from_name(name)

    def test_an_explicit_slug_still_wins(self):
        config = dict(payload()["provider_config"], slug="chosen")
        env = provider.build_env(payload()["agent"], config)
        assert env["BUZZ_ACP_SYSTEM_PROMPT_FILE"].endswith("/chosen.md")

    def test_an_absent_slug_falls_back_to_the_name(self):
        config = {"host": "h", "container": "c"}
        agent = dict(payload()["agent"], name="Jack (Presenter)")
        env = provider.build_env(agent, config)
        assert env["BUZZ_ACP_SYSTEM_PROMPT_FILE"].endswith("/jack-presenter.md")

    def test_slug_is_no_longer_a_required_field(self):
        assert "slug" not in provider.info()["config_schema"]["required"]

    def test_an_invalid_explicit_slug_is_still_refused(self):
        # Deriving a fallback must not paper over a value the user did type.
        config = dict(payload()["provider_config"], slug="../etc/passwd")
        with pytest.raises(provider.DeployRefused):
            provider.build_env(payload()["agent"], config)


class TestNormativeMapping:
    """The payload-to-environment mapping is normative, not a matter of taste.

    Everything asserted here comes from the table in `docs/remote-agents.md`
    §K8s Entrypoint, which the spec introduces with "two conforming
    implementations must produce interchangeable pods".
    """

    def test_the_dev_mcp_server_is_always_provided(self):
        # Without it a persona can only talk: buzz-agent has no built-in tools
        # and reaches everything through MCP. The binding sets this itself
        # rather than waiting for the desktop to ask.
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert env["BUZZ_ACP_MCP_COMMAND"] == "/usr/local/bin/buzz-dev-mcp"

    def test_an_explicit_mcp_command_is_respected(self):
        agent = payload()["agent"]
        agent["env_vars"] = dict(agent["env_vars"],
                                 BUZZ_ACP_MCP_COMMAND="/usr/local/bin/other-mcp")
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_ACP_MCP_COMMAND"] == "/usr/local/bin/other-mcp"

    def test_respond_to_reaches_the_harness(self):
        agent = dict(payload()["agent"], respond_to="owner-only")
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_ACP_RESPOND_TO"] == "owner-only"

    def test_respond_to_allowlist_is_comma_joined(self):
        agent = dict(payload()["agent"], respond_to="allowlist",
                     respond_to_allowlist=["a" * 64, "b" * 64])
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_ACP_RESPOND_TO_ALLOWLIST"] == "a" * 64 + "," + "b" * 64

    def test_an_empty_allowlist_is_omitted(self):
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert "BUZZ_ACP_RESPOND_TO_ALLOWLIST" not in env

    def test_owner_pubkey_is_mapped_when_there_is_no_auth_tag(self):
        # The spec: with auth_tag null, launch.owner_pubkey ->
        # BUZZ_ACP_AGENT_OWNER is REQUIRED. Refusing to deploy was not enough —
        # the agent needs the owner to resolve at all.
        agent = dict(payload()["agent"], auth_tag="")
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_ACP_AGENT_OWNER"] == "e" * 64
        assert "BUZZ_AUTH_TAG" not in env

    def test_auth_tag_alone_needs_no_owner_var(self):
        env = provider.build_env(payload()["agent"], payload()["provider_config"])
        assert env["BUZZ_AUTH_TAG"]
        assert "BUZZ_ACP_AGENT_OWNER" not in env

    def test_provider_and_model_are_never_mapped_by_us(self):
        # "A provider MUST NOT map `provider` to any env var itself — that
        # mapping is per-runtime and lives in the desktop's resolver."
        agent = dict(payload()["agent"], provider="openrouter", model="some/model")
        agent["env_vars"] = {}
        env = provider.build_env(agent, payload()["provider_config"])
        assert "BUZZ_AGENT_PROVIDER" not in env
        assert "OPENROUTER_MODEL" not in env

    def test_the_user_can_still_supply_them_through_env_vars(self):
        # Which is the workaround while the desktop does not resolve them for
        # the buzz-agent + OpenRouter combination.
        agent = payload()["agent"]
        agent["env_vars"] = {"BUZZ_AGENT_PROVIDER": "openrouter",
                             "OPENROUTER_MODEL": "z-ai/glm-5.3"}
        env = provider.build_env(agent, payload()["provider_config"])
        assert env["BUZZ_AGENT_PROVIDER"] == "openrouter"
        assert env["OPENROUTER_MODEL"] == "z-ai/glm-5.3"
