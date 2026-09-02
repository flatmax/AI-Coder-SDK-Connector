"""Tests for aic_dc.antigravity.options — the engine's config assembly.

The load-bearing assertion in this file is that **a write tool cannot be
enabled without something to gate it**.

AG-5 makes the permission dialog a requirement of the second engine
rather than a feature of it, and AG-R-11 is the measurement that says
gating the file tools alone is not a boundary: an agent refused an
``edit_file`` went after the same change with ``sed -i`` through
``run_command``, unprompted, on both probe runs. So the seam is *all*
mutating tools, and the check has to be structural — a posture nobody can
reach by forgetting an argument, rather than a default somebody can
override while debugging.

Everything here runs offline. ``build_config_kwargs`` imports no SDK, and
the two tests that do construct a real ``LocalAgentConfig`` skip when the
wheel is absent, because a base install is a one-engine install
(AG-R-10).
"""

from __future__ import annotations

import pytest

from aic_dc.antigravity import options
from aic_dc.antigravity.credentials import GEMINI_API, Credentials


def fake_credentials() -> Credentials:
    """A resolved Gemini-API credential with no live key behind it."""
    return Credentials(mode=GEMINI_API, api_key="test-key", source="test")


def kwargs(**overrides):
    base = {"repo_root": "/tmp/repo", "credentials": fake_credentials()}
    base.update(overrides)
    return options.build_config_kwargs(**base)


# ----------------------------------------------------------------------
# The one that matters
# ----------------------------------------------------------------------


class TestNoWriteToolWithoutAGate:
    """AG-5 and AG-R-11, enforced rather than documented."""

    def test_no_hook_means_no_mutating_tool_is_enabled(self):
        enabled = kwargs()["capabilities"]["enabled_tools"]
        assert not (set(enabled) & options.MUTATING_TOOLS), (
            "A session with no decide hook enabled a mutating tool. There is "
            "no posture in which a write reaches the model with nothing "
            "between it and the disk (AG-5)."
        )

    def test_asking_for_writes_without_a_hook_is_an_error(self):
        with pytest.raises(ValueError, match="no decide hook"):
            kwargs(write_tools=frozenset({"edit_file"}))

    def test_run_command_is_gated_with_the_file_tools(self):
        """AG-R-11: the finding that a denied edit comes back as a shell call."""
        assert "run_command" in options.MUTATING_TOOLS

    def test_subagents_are_gated_too(self):
        """A gate that stopped at the top trajectory is bypassed by delegating."""
        assert "start_subagent" in options.MUTATING_TOOLS

    def test_a_hook_enables_the_write_tools(self):
        enabled = kwargs(decide_hook=object())["capabilities"]["enabled_tools"]
        assert options.MUTATING_TOOLS <= set(enabled)

    def test_a_tool_outside_the_seam_is_refused(self):
        """Widening the set is not a thing ``write_tools`` can do."""
        with pytest.raises(ValueError, match="MUTATING_TOOLS"):
            kwargs(decide_hook=object(), write_tools=frozenset({"ask_question"}))

    def test_write_tools_can_be_narrowed(self):
        enabled = kwargs(
            decide_hook=object(), write_tools=frozenset({"edit_file"})
        )["capabilities"]["enabled_tools"]
        assert "edit_file" in enabled
        assert "run_command" not in enabled


class TestNondestructiveIsNotOurWriteBoundary:
    """The SDK's own classifier disagrees with ours, and ours is right here.

    ``BuiltinTools.nondestructive()`` counts ``create_file``, ``edit_file``
    and ``generate_image`` as nondestructive — everything but
    ``run_command``. That is defensible for "will this hurt the machine"
    and exactly backwards for "will this change the working tree", which
    is what the permission dialog exists to ask about. Borrowing it would
    enable the two tools AG-5 was written for.

    This test pins the difference so a release that redefines the SDK's
    set is a red test rather than a silent ungating.
    """

    def test_the_sdk_calls_our_write_tools_nondestructive(self):
        types = pytest.importorskip("google.antigravity").types
        nondestructive = {t.value for t in types.BuiltinTools.nondestructive()}
        overlap = options.MUTATING_TOOLS & nondestructive
        assert overlap, (
            "The SDK now agrees with MUTATING_TOOLS. If nondestructive() has "
            "become a real write boundary, this module could inherit it — "
            "re-read it before deleting our own table."
        )
        assert {"create_file", "edit_file"} <= overlap

    def test_read_only_holds_none_of_our_write_tools(self):
        types = pytest.importorskip("google.antigravity").types
        read_only = {t.value for t in types.BuiltinTools.read_only()}
        assert not (options.MUTATING_TOOLS & read_only), (
            "A tool is in both the SDK's read_only() set and our mutating "
            "set. READ_ONLY_SENTINEL expands to read_only(), so this would "
            "enable a write tool on a session with no hook."
        )


# ----------------------------------------------------------------------
# Containment and credentials
# ----------------------------------------------------------------------


class TestWorkspaceContainment:
    """AG-10: one repo root, and it is set rather than inherited."""

    def test_workspaces_is_the_repo_root_and_nothing_else(self, tmp_path):
        assert kwargs(repo_root=tmp_path)["workspaces"] == [str(tmp_path.resolve())]

    def test_the_root_is_resolved(self, tmp_path):
        nested = tmp_path / "a" / ".." / "b"
        (tmp_path / "b").mkdir(parents=True)
        assert kwargs(repo_root=nested)["workspaces"] == [str(tmp_path / "b")]


class TestCredentialsAreConfigFieldsNotEnvironment:
    """AG-11: the key is passed as a field and never exported."""

    def test_the_key_reaches_the_config(self):
        assert kwargs()["api_key"] == "test-key"

    def test_env_is_never_set(self):
        assert "env" not in kwargs()

    def test_env_is_a_standing_refusal_with_a_reason(self):
        assert "env" in options.NEVER_SET
        assert "GEMINI_API_KEY" in options.NEVER_SET["env"]


# ----------------------------------------------------------------------
# The table that moved
# ----------------------------------------------------------------------


class TestNeverSetMovedHere:
    """Phase 3's cut-and-paste, checked at both ends.

    ``surface._declined_config`` has preferred this module since phase 1
    and reads it with no other edit; the fallback it used before is now
    deliberately empty, so a broken import shows up as an ``unclassified``
    gate failure rather than as a stale duplicate table.
    """

    def test_the_probe_reads_the_refusals_from_here(self):
        from aic_dc.antigravity import surface

        assert surface._declined_config() == dict(options.NEVER_SET)

    def test_the_old_fallback_is_empty_rather_than_a_copy(self):
        from aic_dc.antigravity import surface

        assert surface.NEVER_SET_CONFIG == {}

    def test_response_schema_is_still_refused(self):
        assert "response_schema" in options.NEVER_SET

    def test_every_refusal_names_a_real_config_field(self):
        from aic_dc.antigravity import surface

        assert set(options.NEVER_SET) <= set(surface.config_fields())


# ----------------------------------------------------------------------
# Construction, where the SDK is present
# ----------------------------------------------------------------------


class TestConstruction:
    """The one line that imports the SDK, and the default it refuses."""

    def test_it_builds_a_config(self):
        pytest.importorskip("google.antigravity")
        config = options.build_config(**kwargs())
        assert config.model == options.DEFAULT_MODEL
        assert config.api_key == "test-key"

    def test_policies_are_always_set(self):
        """Unset is not "no policy" — it is approve-everything-but-shell.

        ``LocalAgentConfig`` defaults ``policies`` to
        ``confirm_run_command()``: deny ``run_command``, approve everything
        else. That is the blanket-bypass posture AG-5 says must never ship,
        arriving as a default nobody chose.
        """
        pytest.importorskip("google.antigravity")
        assert options.build_config(**kwargs()).policies

    def test_the_sdk_default_really_is_approve_all(self):
        """Pins the default this module exists to override.

        A release that fixes it turns this red, which is the point: the
        paragraph above should not outlive its reason.
        """
        sdk = pytest.importorskip("google.antigravity")
        config = sdk.LocalAgentConfig(model="m", api_key="k")
        assert config.policies, (
            "LocalAgentConfig no longer defaults `policies`. Re-read AG-5's "
            "note about the default nobody chose before relying on it."
        )

    def test_finish_is_always_enabled(self):
        """An agent with no way to end its turn runs until a cap stops it."""
        types = pytest.importorskip("google.antigravity").types
        config = options.build_config(**kwargs())
        assert types.BuiltinTools.FINISH in config.capabilities.enabled_tools

    def test_interactive_behavior_is_pinned(self):
        """AUTONOMOUS is the SDK default and the wrong posture for a UI."""
        types = pytest.importorskip("google.antigravity").types
        config = options.build_config(**kwargs())
        assert config.capabilities.agent_behavior == types.AgentBehavior.INTERACTIVE

    def test_a_read_only_config_starts_without_a_hook(self):
        """``Agent.__aenter__`` refuses a write tool with no gate.

        Here there is no write tool, so the config is startable — which is
        the phase-3 posture: a session that can read the repository and
        cannot change it.
        """
        types = pytest.importorskip("google.antigravity").types
        enabled = options.build_config(**kwargs()).capabilities.enabled_tools
        assert set(enabled) <= set(types.BuiltinTools.read_only())
