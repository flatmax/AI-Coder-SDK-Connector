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

The second is that **the six index tools reach the model saying what the
other two engines say** (AG-34). ``TestTheIndexToolsAreDeclaredProperly``
drives the SDK's own ``ToolRunner`` rather than asserting on our wrapper,
because the defect it guards is a tool that exists in Python and does not
exist to a model: handed a bare callable the SDK derives the declaration
from the *signature*, and six handlers sharing one ``**arguments`` shape
would advertise one empty schema between them.
``TestACustomToolIsNotRefusedByTheWildcardDeny`` covers the other half of
the same failure — declared, shown to the model, and then denied by
``deny_all()`` for not being a ``BuiltinTools`` member.

Everything here runs offline. ``build_config_kwargs`` imports no SDK, and
the tests that do construct a real ``LocalAgentConfig`` — or touch
``ToolWithSchema`` — skip when the wheel is absent, because a base install
is a one-engine install (AG-R-10).
"""

from __future__ import annotations

import asyncio

import pytest

from aic_dc import index_tools
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


# ----------------------------------------------------------------------
# AG-34: the six index tools, as this transport declares them
# ----------------------------------------------------------------------


class StubBridge:
    """An ``McpBridge``-shaped object that echoes what it was asked.

    Six coroutines returning the MCP envelope the real bridge returns,
    ``{"content": [{"type": "text", …}]}``, because the thing worth
    asserting on this transport is that the envelope is *unwrapped* before
    the model sees it — ``ToolResult.result`` is ``Any`` here, so a content
    array would go out as a nested structure the model reads through.

    Keyword-only signatures matching ``ToolSpec.parameters`` exactly, and
    that is deliberate: a stub with invented parameter names is a stub that
    passes while the real bridge raises ``TypeError`` mid-turn. This one was
    written by reading the specs after a throwaway version failed on
    ``path_prefix``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _answer(self, name: str, **kw) -> dict:
        self.calls.append((name, kw))
        return {"content": [{"type": "text", "text": f"{name} {sorted(kw.items())}"}]}

    async def symbol_map(self, *, path_prefix=None, language=None, cursor=None) -> dict:
        return self._answer(
            "symbol_map", path_prefix=path_prefix, language=language, cursor=cursor
        )

    async def file_symbols(self, *, paths=None) -> dict:
        return self._answer("file_symbols", paths=paths)

    async def find_references(self, *, symbol=None) -> dict:
        return self._answer("find_references", symbol=symbol)

    async def doc_outline(self, *, path_prefix=None, cursor=None) -> dict:
        return self._answer("doc_outline", path_prefix=path_prefix, cursor=cursor)

    async def review_state(self) -> dict:
        return self._answer("review_state")

    async def ui_state(self) -> dict:
        return self._answer("ui_state")


def sdk_tools(bridge=None):
    """The six callables, skipping where the SDK is not installed."""
    pytest.importorskip("google.antigravity")
    return options.index_tool_callables(bridge or StubBridge())


class TestTheIndexToolsAreDeclaredProperly:
    """AG-34, on the transport where a tool is a Python object.

    The failure this guards is not "the tool errors" — it is **the tool is
    not there**, or is there under a name and description the other two
    engines do not use. So the assertions are about the declaration: the
    name the runner registers, the description the proto carries, and the
    schema the model is shown, each compared against
    :data:`aic_dc.index_tools.SPECS` rather than against a literal, since
    ``tests/test_index_tools.py`` is where the literals are pinned.

    Three attributes carry all of that and **none of them is an
    argument**: ``__name__``, ``__doc__`` and ``input_schema``
    (``connections/local/local_connection.py:222-230``). A wrapper that set
    two of the three would produce a tool the model can see and cannot
    understand, which is why each is asserted separately.
    """

    def test_all_six_are_produced_in_the_tables_order(self):
        """The order the model is shown them in is the spec table's.

        Compared against ``[spec.name for spec in SPECS]`` rather than
        ``TOOL_NAMES``, which is a frozenset and would compare in an
        arbitrary order — passing whatever this returned.
        """
        assert [t.__name__ for t in sdk_tools()] == [
            spec.name for spec in index_tools.SPECS
        ]

    def test_the_description_is_the_docstring(self):
        """Because ``__doc__`` *is* the description on this path.

        There is no description argument anywhere between here and the
        proto, so prose that is not attached to the function object does
        not reach the model at all.
        """
        for tool, spec in zip(sdk_tools(), index_tools.SPECS, strict=True):
            assert tool.__doc__ == spec.description, spec.name

    def test_the_schema_is_the_specs_own(self):
        for tool, spec in zip(sdk_tools(), index_tools.SPECS, strict=True):
            assert tool.input_schema == spec.schema, spec.name

    def test_the_sdk_does_not_rewrite_our_schemas(self):
        """Measured 2026-09-15: ``normalize_schema`` returns all six unchanged.

        That measurement is what lets AG-34 claim the model sees the *same*
        schema on all three transports rather than an equivalent one. It is
        the SDK's function, so it can change under us — which is what this
        test is for.
        """
        pytest.importorskip("google.antigravity")
        from google.antigravity.tools import schema_utils

        for spec in index_tools.SPECS:
            assert schema_utils.normalize_schema(spec.schema) == spec.schema, spec.name

    def test_a_bare_callable_would_have_flattened_them(self):
        """Why ``ToolWithSchema`` and not a plain function.

        The other branch derives the declaration from the signature, and
        every one of our handlers has the same signature — ``**arguments``.
        Six tools, one empty schema between them, and no per-argument prose:
        a failure that looks like the model refusing to use a tool that is
        plainly there. Pins the branch condition rather than the outcome,
        because the outcome is a proto this test would have to build a
        connection to see.
        """
        pytest.importorskip("google.antigravity")
        from google.antigravity.tools.tool_runner import ToolWithSchema

        assert all(isinstance(t, ToolWithSchema) for t in sdk_tools())


class TestTheIndexToolsAnswerThroughTheSdksOwnRunner:
    """Driven through ``ToolRunner``, not by calling our closure.

    ``ToolRunner._coerce_args`` inspects the *inner* function's signature
    before dispatching, and a ``**arguments`` handler is exactly the shape
    that could plausibly lose its arguments there. It does not — a
    ``VAR_KEYWORD`` parameter is skipped by name and the unmatched kwargs
    are restored (``tools/tool_runner.py:325-328``) — but "does not" is a
    claim about somebody else's code, so it is measured rather than
    asserted in a comment.
    """

    def runner(self, bridge):
        pytest.importorskip("google.antigravity")
        from google.antigravity.tools.tool_runner import ToolRunner

        return ToolRunner(list(options.index_tool_callables(bridge)))

    def test_the_runner_registers_our_names(self):
        bridge = StubBridge()
        assert set(self.runner(bridge).tool_names) == set(index_tools.TOOL_NAMES)

    def test_arguments_reach_the_bridge(self):
        bridge = StubBridge()
        runner = self.runner(bridge)
        asyncio.run(runner.execute("symbol_map", path_prefix="src", language="python"))
        name, kw = bridge.calls[-1]
        assert name == "symbol_map"
        assert kw["path_prefix"] == "src"
        assert kw["language"] == "python"

    def test_an_absent_optional_argument_arrives_as_none(self):
        """Which is what the bridge's own defaults expect.

        ``ToolSpec.arguments`` passes every declared parameter, absent ones
        as ``None``, rather than omitting them — the behaviour the ``@tool``
        closures had with ``args.get(...)``, so the bridge does not need to
        agree with a schema it cannot see.
        """
        bridge = StubBridge()
        asyncio.run(self.runner(bridge).execute("symbol_map"))
        assert bridge.calls[-1][1] == {
            "path_prefix": None,
            "language": None,
            "cursor": None,
        }

    def test_a_no_argument_tool_works(self):
        bridge = StubBridge()
        asyncio.run(self.runner(bridge).execute("review_state"))
        assert bridge.calls[-1] == ("review_state", {})

    def test_the_answer_is_text_not_the_mcp_envelope(self):
        """The model gets a sentence, as it does on the Claude engine.

        ``ToolResult.result`` is ``Any`` on this transport, so the envelope
        would go through untouched and the model would be reading a content
        array to find the map.
        """
        result = asyncio.run(self.runner(StubBridge()).execute("review_state"))
        assert isinstance(result, str)
        assert result.startswith("review_state")


class TestACustomToolIsNotRefusedByTheWildcardDeny:
    """The other half of AG-34, and the one that fails silently.

    ``policies`` starts with ``deny_all()`` — ``deny("*")`` — and every
    allow after it names a ``BuiltinTools`` member. A custom Python tool is
    not one, so before this the six index tools were **declared to the
    model and then refused**: a wasted turn that reads to the user as the
    tool being broken rather than as a policy. The Go harness is what
    evaluates these rules, so the bug would not have shown up anywhere in
    Python.
    """

    def test_every_custom_tool_is_named_in_the_policies(self):
        pytest.importorskip("google.antigravity")
        tools = sdk_tools()
        config = options.build_config(**kwargs(tools=tools))
        rendered = repr(config.policies)
        for tool in tools:
            assert tool.__name__ in rendered, tool.__name__

    def test_the_allow_is_specific_rather_than_a_second_wildcard(self):
        """Widening the wildcard would ungate everything else with it.

        The SDK's documented precedence is *Specific Deny > Specific Ask >
        Specific Allow > Wildcard Deny*, so a named allow is enough — and
        an ``allow("*")`` would beat nothing extra while giving away the
        write seam AG-5 exists to hold.
        """
        pytest.importorskip("google.antigravity")
        config = options.build_config(**kwargs(tools=sdk_tools()))
        wildcards = [p for p in config.policies if repr(p).count("'*'")]
        assert len(wildcards) == 1, (
            "more than one wildcard rule in the policy list; the only one "
            "there should be is deny_all()"
        )

    def test_a_config_with_no_custom_tools_is_unchanged(self):
        """A check that cannot fail the other way is decoration."""
        pytest.importorskip("google.antigravity")
        assert repr(options.build_config(**kwargs()).policies) == repr(
            options.build_config(**kwargs(tools=())).policies
        )

    def test_the_names_come_from_dunder_name(self):
        """The SDK's own rule, so a policy cannot name something else.

        ``ToolRunner.register`` falls back to ``type(tool).__name__`` for an
        object without one; a policy naming the *class* would allow every
        instance of it, so an unnamed callable is left out and warned about
        rather than guessed at.
        """

        def named() -> None: ...

        assert options._custom_tool_names([named]) == ["named"]
        assert options._custom_tool_names([object()]) == []
