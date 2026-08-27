"""Tests for aic_dc.claude_code.health credential resolution — phase 6.

``detect_credentials`` is the only billing-mode signal the browser gets, and
it answers a question this process cannot see the answer to: the CLI resolves
its own credentials in a child that may be running under a config dir of its
own. So the function's whole job is to report *what it looked for and found*
without predicting what the CLI will do — and to warn about the combinations
where the answer will surprise whoever is paying (R-9).

Every test pins the environment. That is not tidiness: the function reads the
real ``os.environ`` and the real ``$HOME``, so a developer machine with a
subscription login or an ``ANTHROPIC_API_KEY`` in its shell would otherwise
decide the outcome. The read-only contract (§ Credential resolution must not
be polluted) is itself asserted at the bottom.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aic_dc.claude_code.health import (
    _API_KEY_VARS,
    _ENDPOINT_VARS,
    _GATEWAY_VARS,
    _credential_base,
    detect_credentials,
)

_ALL_VARS = (*_API_KEY_VARS, *_GATEWAY_VARS, *_ENDPOINT_VARS, "CLAUDE_CONFIG_DIR")


@pytest.fixture
def config_dir(monkeypatch, tmp_path):
    """An empty credential directory, and an environment with nothing in it.

    Returns the directory so a test can put a login file in it. Redirecting
    ``CLAUDE_CONFIG_DIR`` is what keeps the assertions off the developer's
    own ``~/.claude``.
    """
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    base = tmp_path / "config"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    return base


def login(config_dir, name=".credentials.json"):
    """Write a credential file. The contents are never read, only the path."""
    (config_dir / name).write_text("{}", encoding="utf-8")
    return config_dir / name


# ---------------------------------------------------------------------------
# Which source was found
# ---------------------------------------------------------------------------


class TestSource:
    """The sentence the Debug section shows, one branch at a time."""

    def test_bedrock_names_the_provider_and_the_variable(self, monkeypatch, config_dir):
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        source, _ = detect_credentials()
        assert source == "Amazon Bedrock (via CLAUDE_CODE_USE_BEDROCK)"

    def test_vertex_names_the_provider_and_the_variable(self, monkeypatch, config_dir):
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        source, _ = detect_credentials()
        assert source == "Google Vertex AI (via CLAUDE_CODE_USE_VERTEX)"

    @pytest.mark.parametrize("var", _API_KEY_VARS)
    def test_an_api_key_names_the_variable_it_came_from(
        self, monkeypatch, config_dir, var
    ):
        """The variable, never the value: this string is broadcast to a
        browser and rendered in a table."""
        monkeypatch.setenv(var, "sk-ant-secret")
        source, _ = detect_credentials()
        assert source == f"API key from ${var}"
        assert "secret" not in source

    @pytest.mark.parametrize("name", [".credentials.json", "credentials.json"])
    def test_a_login_file_names_the_path_it_was_found_at(self, config_dir, name):
        path = login(config_dir, name)
        source, _ = detect_credentials()
        assert source == f"subscription login ({path})"

    def test_the_dotted_name_wins_when_both_exist(self, config_dir):
        """Only which one is reported is at stake — the CLI's own precedence
        is not something this function gets to decide."""
        login(config_dir, ".credentials.json")
        login(config_dir, "credentials.json")
        source, _ = detect_credentials()
        assert source == f"subscription login ({config_dir / '.credentials.json'})"

    def test_nothing_found_says_where_it_looked(self, config_dir):
        source, _ = detect_credentials()
        assert source == (
            f"unknown — no key, gateway or login file in {config_dir}"
        )

    def test_nothing_found_predicts_nothing(self, config_dir):
        """This branch used to say the CLI would prompt for login. It was
        observed live in phase 6 against a fully authenticated session that
        was never going to prompt for anything: the CLI child of a resumed
        session runs under a materialised config dir this process cannot
        see, so an empty directory here means "unknown", not "unauthenticated".
        """
        source, _ = detect_credentials()
        assert "prompt" not in source
        assert source.startswith("unknown")


class TestPrecedence:
    """Which source is reported when several are present.

    The order mirrors what the CLI actually does, and it is the reason the
    warnings below exist: the thing that wins is not the thing a user with a
    subscription expects to win.
    """

    def test_a_gateway_beats_an_api_key(self, monkeypatch, config_dir):
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        source, _ = detect_credentials()
        assert source.startswith("Amazon Bedrock")

    def test_an_api_key_beats_a_login_file(self, monkeypatch, config_dir):
        login(config_dir)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        source, _ = detect_credentials()
        assert source == "API key from $ANTHROPIC_API_KEY"

    def test_the_first_api_key_variable_wins(self, monkeypatch, config_dir):
        for var in _API_KEY_VARS:
            monkeypatch.setenv(var, "sk-ant-x")
        source, _ = detect_credentials()
        assert source == f"API key from ${_API_KEY_VARS[0]}"

    @pytest.mark.parametrize("value", ["0", "false", "no", "FALSE", " no ", ""])
    def test_a_gateway_switched_off_is_not_a_gateway(
        self, monkeypatch, config_dir, value
    ):
        """``CLAUDE_CODE_USE_BEDROCK=0`` is how a gateway is turned off in a
        shell that exports it unconditionally. Treating "0" as set would
        report Bedrock billing for a subscription session."""
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", value)
        login(config_dir)
        source, _ = detect_credentials()
        assert source.startswith("subscription login")

    @pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
    def test_anything_else_is_a_gateway(self, monkeypatch, config_dir, value):
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", value)
        source, _ = detect_credentials()
        assert source.startswith("Google Vertex AI")


# ---------------------------------------------------------------------------
# The combinations worth a warning
# ---------------------------------------------------------------------------


class TestWarnings:
    """R-9: a credential the user did not choose silently bills someone else.

    A warning is returned rather than raised, and the caller folds it into
    ``EngineHealth.auth_warning`` — the session still starts, because the
    configuration is legal and might even be deliberate.
    """

    def test_one_source_alone_is_never_a_warning(self, monkeypatch, config_dir):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert detect_credentials()[1] is None

    def test_a_login_file_alone_is_never_a_warning(self, config_dir):
        login(config_dir)
        assert detect_credentials()[1] is None

    def test_nothing_at_all_is_never_a_warning(self, config_dir):
        """An unknown source is a fact about this process's visibility, not
        a misconfiguration to warn about."""
        assert detect_credentials()[1] is None

    def test_a_key_over_a_login_says_who_gets_billed(self, monkeypatch, config_dir):
        path = login(config_dir)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        _, warning = detect_credentials()
        assert "$ANTHROPIC_API_KEY is set" in warning
        assert str(path) in warning
        assert "bill to that key rather than the subscription" in warning

    def test_a_gateway_over_a_login_names_both(self, monkeypatch, config_dir):
        path = login(config_dir)
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        _, warning = detect_credentials()
        assert "$CLAUDE_CODE_USE_BEDROCK redirects the CLI" in warning
        assert str(path) in warning

    @pytest.mark.parametrize("var", _ENDPOINT_VARS)
    def test_an_endpoint_override_is_worth_saying_on_its_own(
        self, monkeypatch, config_dir, var
    ):
        """No credential conflict, but the turns are going somewhere other
        than the API the cost figures are priced against."""
        monkeypatch.setenv(var, "https://proxy.internal")
        source, warning = detect_credentials()
        assert source.startswith("unknown")
        assert f"${var}" in warning
        assert "not the default API" in warning
        assert "proxy.internal" not in warning

    def test_every_override_is_named(self, monkeypatch, config_dir):
        for var in _ENDPOINT_VARS:
            monkeypatch.setenv(var, "https://proxy.internal")
        _, warning = detect_credentials()
        for var in _ENDPOINT_VARS:
            assert f"${var}" in warning

    def test_two_problems_arrive_as_one_sentence_each(self, monkeypatch, config_dir):
        """The field is one string in the health record, so the joining
        happens here rather than in the browser."""
        login(config_dir)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.internal")
        _, warning = detect_credentials()
        assert warning.count("; ") == 1
        assert "$ANTHROPIC_API_KEY is set" in warning
        assert "$ANTHROPIC_BASE_URL" in warning


# ---------------------------------------------------------------------------
# The limits of what the answer can know
# ---------------------------------------------------------------------------


class TestCredentialBase:
    """Where the login file is looked for, and why that is worth stating."""

    def test_the_default_is_the_cli_s_own_directory(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert _credential_base() == Path.home() / ".claude"

    def test_a_redirected_config_dir_is_honoured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert _credential_base() == tmp_path

    def test_a_tilde_in_the_config_dir_is_expanded(self, monkeypatch):
        """The variable is a user-typed path, and ``Path("~/x").exists()``
        is always False — which would report "no login file" for a session
        that has one."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/somewhere")
        assert _credential_base() == Path.home() / "somewhere"

    def test_the_directory_is_read_at_call_time(self, monkeypatch, tmp_path):
        """Not captured at import: the service resolves credentials during
        connect, after any config the user supplied has been applied."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "first"))
        assert _credential_base() == tmp_path / "first"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "second"))
        assert _credential_base() == tmp_path / "second"


class TestReadOnlyContract:
    """§ Credential resolution must not be polluted.

    The engine never writes a credential variable, because the CLI child
    inherits this environment and a value invented here would silently
    become the account that pays.
    """

    @pytest.mark.parametrize(
        "setup",
        [
            pytest.param({}, id="nothing-set"),
            pytest.param({"ANTHROPIC_API_KEY": "sk-ant-x"}, id="api-key"),
            pytest.param({"CLAUDE_CODE_USE_BEDROCK": "1"}, id="gateway"),
            pytest.param(
                {"ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "https://p"},
                id="key-and-override",
            ),
        ],
    )
    def test_detection_leaves_the_environment_exactly_as_it_found_it(
        self, monkeypatch, config_dir, setup
    ):
        login(config_dir)
        for var, value in setup.items():
            monkeypatch.setenv(var, value)
        before = dict(os.environ)
        detect_credentials()
        assert dict(os.environ) == before

    def test_detection_does_not_create_the_directory_it_looked_in(
        self, monkeypatch, tmp_path
    ):
        """A probe that made ``~/.claude`` would leave the CLI reading an
        empty directory it was about to populate itself."""
        for var in _ALL_VARS:
            monkeypatch.delenv(var, raising=False)
        missing = tmp_path / "absent"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(missing))
        detect_credentials()
        assert not missing.exists()


class TestNoDeclaredAndEmptyFields:
    """A serialised field that nothing writes is worse than no field.

    ``EngineHealth`` carried an ``mcp`` list for three phases: declared,
    serialised by ``to_dict()``, and assigned by nothing in ``src/``. Every
    consumer therefore read ``[]`` — and an empty list does not say "no
    servers", it says "no answer", which is the shape that made the Context
    tab's own MCP claim wrong for a week before anyone checked.

    Deleted rather than filled in, because the question it looked like it
    answered has a better answer already: ``get_mcp_status()`` asks the CLI
    and is allowed to fail visibly, so a status pill can be absent instead
    of confidently blank. ``degradations`` answers the different question of
    what the session started *without*, and it has a writer.

    Pinned here so the field cannot come back by looking useful.
    """

    def test_the_payload_has_no_mcp_key(self):
        from aic_dc.claude_code.health import EngineHealth

        assert "mcp" not in EngineHealth().to_dict()

    def test_the_dataclass_has_no_mcp_field(self):
        import dataclasses

        from aic_dc.claude_code.health import EngineHealth

        names = {f.name for f in dataclasses.fields(EngineHealth)}
        assert "mcp" not in names

    def test_every_serialised_key_has_a_writer_or_a_default_that_means_it(
        self,
    ):
        """The general form of the rule, as far as a test can state it.

        Not "every key is non-empty" — plenty are legitimately empty on a
        fresh session. What this asserts is narrower and is the thing that
        actually went wrong: every key in the payload corresponds to a
        dataclass field or a computed property, so a key cannot be
        serialised out of nothing at all.
        """
        import dataclasses

        from aic_dc.claude_code.health import EngineHealth

        health = EngineHealth()
        payload = health.to_dict()
        declared = {f.name for f in dataclasses.fields(EngineHealth)}
        computed = {"mirror_gaps_escalated"}
        assert set(payload) <= declared | computed
