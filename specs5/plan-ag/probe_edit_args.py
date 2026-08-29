#!/usr/bin/env python3
"""Probe: does `ToolCall.args` carry enough to render a proposed-edit diff?

This answers the single load-bearing question under the "drive the Python SDK,
not the `agy` CLI" decision (see decisions.md):

  At `pre_tool_call_decide` time -- i.e. BEFORE the edit is applied -- does the
  host receive enough information to render a diff in the browser and gate the
  edit on a human's answer?

`agy --output-format stream-json` demonstrably cannot do this: its
`write_to_file` step carries `parameters: {"TargetFile": ...}` with no content
and no `output`. The SDK is believed to, because `ToolCall.args` is documented
as the complete argument dict and the harness protocol models edits as
`ActionEditFile{file_path, DiffBlock{start_line, end_line, lines:
DiffLine{text, action}}}`. That inference is what this script tests.

The probe deliberately DENIES every mutating call. Two things are proven at
once: what the host sees, and that returning `allow=False` actually blocks the
write (asserted against the file's bytes on disk afterwards).

Requires a Gemini API key -- the SDK has no OAuth path at 0.1.15:

    export GEMINI_API_KEY=...
    .venv/bin/python specs5/plan-ag/probe_edit_args.py

Exits 0 if the probe reached a verdict, 1 if it could not run.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

from google.antigravity import Agent
from google.antigravity import LocalAgentConfig
from google.antigravity import types
from google.antigravity.hooks import hooks
from google.antigravity.hooks import policy

# gemini-2.5-flash is still listed by the models endpoint but 404s for new
# users ("no longer available to new users ... use models/gemini-3.6-flash").
# Free tier throttles at 5 RPM and one agent turn is many model calls, so 429s
# mid-turn are expected -- the verdict still prints. Override with PROBE_MODEL.
MODEL = os.environ.get("PROBE_MODEL", "gemini-3.6-flash")

SEED_FILE = "target.py"
SEED_CONTENT = """def greet(name):
    return "Hello, " + name


def add(a, b):
    return a + b
"""

# Populated by the hooks, drained by main().
observed_calls: list[types.ToolCall] = []
observed_results: list[types.ToolResult] = []

# The file tools whose args we are testing for diff-renderability.
MUTATING = {
    types.BuiltinTools.EDIT_FILE.value,
    types.BuiltinTools.CREATE_FILE.value,
}

# run_command is ALSO a mutation path. An earlier run of this probe denied only
# the file tools, and the agent immediately routed around the denial with
# `sed -i`. Gating the file tools alone is not a containment boundary, so the
# probe denies the shell too and records any such attempt as a bypass.
BYPASS = {types.BuiltinTools.RUN_COMMAND.value}
DENIED = MUTATING | BYPASS

bypass_attempts: list[types.ToolCall] = []


@hooks.pre_tool_call_decide
async def capture_and_deny(data: types.ToolCall) -> types.HookResult:
  """Records every tool call; denies the mutating ones.

  This is the exact seam a host UI would use: it is async, so in the real
  application this would emit a WebSocket frame, await the human's answer, and
  return their verdict. Here it just refuses, so the probe never writes.
  """
  observed_calls.append(data)
  print(f"\n[pre_tool_call_decide] tool={data.name!r} step_id={data.step_id!r}")
  print(f"  canonical_path: {data.canonical_path!r}")
  print("  args:")
  print(_indent(_dump(data.args), 4))

  if data.name in BYPASS:
    bypass_attempts.append(data)
    print("  -> DENYING (shell: mutation path around the file-tool gate)")
    return types.HookResult(
        allow=False,
        message="Denied by probe: host rejected the command.",
    )

  if data.name in DENIED:
    print("  -> DENYING (probe never writes; proves the gate holds)")
    return types.HookResult(
        allow=False,
        message="Denied by probe: host rejected the proposed edit.",
    )
  return types.HookResult(allow=True)


@hooks.post_tool_call
async def capture_result(data: types.ToolResult) -> None:
  """Records what the result side yields, for comparison against args."""
  observed_results.append(data)
  print(f"\n[post_tool_call] tool={data.name!r} step_id={data.step_id!r}")
  print(f"  error: {data.error!r}")
  print("  result:")
  print(_indent(_dump(data.result), 4))


def _dump(obj) -> str:
  """Best-effort pretty JSON, falling back to repr for non-serialisables."""
  try:
    return json.dumps(obj, indent=2, default=str)
  except (TypeError, ValueError):
    return repr(obj)


def _indent(text: str, n: int) -> str:
  pad = " " * n
  return "\n".join(pad + line for line in text.splitlines())


def _verdict(workspace: str) -> int:
  """Prints the verdict. Returns a process exit code."""
  print("\n" + "=" * 72)
  print("VERDICT")
  print("=" * 72)

  edits = [c for c in observed_calls if c.name in MUTATING]
  if not edits:
    print("INCONCLUSIVE: the agent never attempted a mutating file tool.")
    print(f"  tools it did call: {sorted({c.name for c in observed_calls})}")
    print("  Re-run, or sharpen the prompt so an edit is unavoidable.")
    return 1

  # 1. Did the denial actually hold?
  on_disk = open(os.path.join(workspace, SEED_FILE)).read()
  unchanged = on_disk == SEED_CONTENT
  print(f"\n1. Denial enforced (file unmodified on disk): {_yn(unchanged)}")
  if not unchanged:
    print("   !! allow=False did NOT prevent the write. This is a blocker.")

  # 2. Is there enough in args to render a diff?
  print("\n2. Per mutating call, is `args` enough to render a diff?")
  renderable = 0
  for call in edits:
    blob = json.dumps(call.args, default=str)
    # A diff needs the new text, not merely a path.
    has_path = bool(call.canonical_path) or any(
        "path" in k.lower() or "file" in k.lower() for k in call.args
    )
    has_payload = len(blob) > len(json.dumps({"path": "x"})) and any(
        isinstance(v, str) and ("\n" in v or len(v) > 40)
        for v in call.args.values()
    )
    print(f"   - {call.name}: path={_yn(has_path)} payload={_yn(has_payload)}"
          f"  args_keys={sorted(call.args)}")
    if has_path and has_payload:
      renderable += 1

  # 3. Did the agent try to route around the file-tool gate via the shell?
  print(f"\n3. Shell bypass attempts after denial: {len(bypass_attempts)}")
  for call in bypass_attempts:
    print(f"   - {call.args.get('CommandLine', call.args)!r}")
  if bypass_attempts:
    print("   NOTE: gating only edit_file/create_file is NOT a containment")
    print("   boundary. A host must gate run_command on the same seam.")

  ok = unchanged and renderable == len(edits)
  print(f"\n4. Overall: {'CONFIRMED' if ok else 'NOT CONFIRMED'}"
        f" ({renderable}/{len(edits)} calls carried a renderable payload)")
  if ok:
    print("   The SDK gives the host the proposed edit before it lands, and")
    print("   the host's answer decides whether it lands. Build on this.")
  else:
    print("   Read the dumped `args` above before trusting the decision.")
  return 0


def _yn(b: bool) -> str:
  return "yes" if b else "NO"


async def run() -> int:
  if not os.environ.get("GEMINI_API_KEY"):
    print("GEMINI_API_KEY is not set. The SDK has no OAuth path at 0.1.15,")
    print("so this probe cannot run without a billed key.", file=sys.stderr)
    return 1

  workspace = tempfile.mkdtemp(prefix="ag-probe-")
  try:
    with open(os.path.join(workspace, SEED_FILE), "w") as f:
      f.write(SEED_CONTENT)
    print(f"workspace: {workspace}")
    print(f"model:     {MODEL}")

    config = LocalAgentConfig(
        model=MODEL,
        workspaces=[workspace],
        hooks=[capture_and_deny, capture_result],
        # Let policy permit everything, so the *hook* is unambiguously the
        # thing doing the gating. run_command stays unused by this prompt.
        policies=[policy.allow_all()],
        capabilities=types.CapabilitiesConfig(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
            enable_subagents=False,
        ),
    )

    prompt = (
        f"In {SEED_FILE}, change the `add` function so it returns"
        " `a + b + 1` instead of `a + b`. Use the edit_file tool to make"
        " this change directly. Do not ask for confirmation."
    )
    print(f"\nprompt: {prompt}\n" + "-" * 72)

    # The turn is expected to end in an error: every mutating tool is denied,
    # and the free tier throttles at 5 RPM. Neither invalidates the probe --
    # the hooks have already captured what we came for -- so report regardless.
    try:
      async with Agent(config) as agent:
        response = await agent.chat(prompt)
        async for chunk in response:
          sys.stdout.write(chunk)
          sys.stdout.flush()
        print()
    except Exception as exc:  # pylint: disable=broad-except
      first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc
      print(f"\n[turn ended with an error, continuing to verdict]\n  {first}")

    return _verdict(workspace)
  finally:
    shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
  sys.exit(asyncio.run(run()))
