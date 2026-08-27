# Build

How the webapp and backend are packaged for distribution. The webapp is a Vite-built SPA served by a built-in HTTP static file server; the backend is a Python package optionally packaged as a PyInstaller single-file binary. Source installs can use a fallback served from GitHub Pages.

**The single-file binary's self-sufficiency now has a price tag**, and that is the headline of this file
after the conversion. AIC⚡DC used to contain its own inference client: bundle `litellm`, ship the binary,
and a user with an API key had a working application. Now the engine is a separate process this project
does not build — the `claude` CLI — and the only copy we can ship is the one the SDK wheel carries, which
is 296.76 MiB. So the packaging story is "bundle everything we can, decide deliberately about the large
thing we could, and diagnose its absence either way" (see
[§ The Engine Is Not Bundleable](#the-engine-is-not-bundleable)).

## Webapp Bundling

- Vite-based build producing a static bundle
- Base path configured to relative (`./`) so the built webapp can be served from any origin without path rewriting
- Output directory bundled into the Python package under a known subdirectory
- Build emits hashed asset filenames for long-term caching
- `index.html` is the entry point with all asset references relative

### Vite optimizeDeps Exclude

- The RPC library depends on a UMD/CJS package that exposes a global
- Vite's dependency optimizer (esbuild) mangles this during pre-bundling, causing runtime errors
- The RPC package must be excluded from optimizeDeps so the browser resolves the ESM import chain natively
- After config changes, Vite cache must be cleared

## Webapp Location Priority

The backend locates the webapp by checking, in order:

1. PyInstaller bundle directory (packaged binary)
2. Source tree (development after a build)
3. Installed package data (pip install)

If no local bundle is found, the server prints an error instructing the user to build the webapp or use dev mode. Source installs may fall back to a GitHub Pages deployment URL.

## Built-in Static File Server

- A threaded HTTP server runs in a daemon thread alongside the WebSocket server
- Serves files from the webapp dist directory
- **SPA fallback** — requests for paths without a file extension that don't match a real file are served the index page (for client-side routing)
- **Silent logging** — per-request logs suppressed
- **Bind address** — loopback by default, all interfaces when collaboration is enabled
- **Threading** — concurrent requests from multiple browser tabs and parallel asset loads supported
- **Error suppression** — broken-pipe and connection-reset errors silently caught, preventing noisy tracebacks when clients disconnect mid-transfer

## Version Baking

- A version file is written to the Python package at build time
- Format — timestamp plus short commit SHA (e.g., `YYYY.MM.DD-HH.MM-abcdef12`)
- Retained for display and logging
- Runtime detection cascade — baked VERSION file, `git rev-parse HEAD`, direct `.git/HEAD` read, fallback to a dev marker

## PyInstaller Packaging

Per-platform single-file binaries built in CI:

- Platforms — Linux, Windows, macOS (ARM)
- Version computed as timestamp plus commit SHA for same-day ordering
- VERSION file baked into the package before bundling
- `--onefile` with explicit config and webapp data inclusion
- Separator is platform-specific (`:` on Unix, `;` on Windows)
- Destination path within the package matches the Python package name so runtime path resolution works

### Dependency Collection

- `--collect-all` for packages with data files — the agent SDK, tree-sitter core and per-language grammars, the document-conversion extra
- **Whole-package collection, not an enumerated module list**, for our own package and the RPC library. Static analysis misses modules registered by class name or imported by string, and a hand-listed set drifts silently against the tree — a hidden import naming a deleted module only warns. Walk the package instead
- Runtime behavior verified in CI on each platform

Removed from collection: the LLM provider library and its provider SDKs, and the tokenizer with its
encoding data files. Both were large — the tokenizer's encodings and the provider library's model registry
were a meaningful fraction of the binary — and both existed to do work the engine now does. Nothing
counts tokens in this process any more (see
[`../3-engine/context-visibility.md`](../3-engine/context-visibility.md)).

The agent SDK needs `--collect-all` rather than a plain hidden import because it ships a data file that
matters: a bundled `claude` CLI under `claude_agent_sdk/_bundled/`. See below.

### The Engine Is Not Bundleable

PyInstaller bundles Python; it cannot build the `claude` CLI, whose release cadence is not ours. So the
binary carries no engine of its own and resolves one at startup, in the SDK transport's order:

1. An explicitly configured `cli_path` from `engine.json`
2. The SDK's own bundled copy under `claude_agent_sdk/_bundled/claude`
3. `claude` on `PATH`

**The order of 2 and 3 is not the intuitive one, and an earlier version of this list had it backwards.**
`SubprocessCLITransport._find_cli` checks the bundled copy *first* and only falls back to `shutil.which`
(verified 2026-08-27 against `claude-agent-sdk` 0.2.137). A machine with a newer system CLI still runs the
wheel's copy unless `engine.json`'s `cli_path` says otherwise — which is why `cli_path` exists and why
startup reports which binary it resolved rather than assuming. `EngineHealth` carries the answer;
`claude_code/health.py` § `_sdk_find_cli` carries the same warning next to the code.

Option 2 is the reason for `--collect-all` on the SDK: the bundled copy is package data that a plain import
collection would drop, and dropping it turns the default resolution into a `PATH` search.

**What that copy actually is, verified against the installed wheel** (`claude-agent-sdk` 0.2.137, CLI pin
2.1.229, checked 2026-08-27): a **single native executable, not a Node script** — on Linux an ELF x86-64
binary dynamically linked against glibc, 296.76 MiB. It needs no Node runtime, which an earlier version of
this section said it did; on its own platform it is a complete engine, not a half-measure. Two consequences
follow, and they pull in opposite directions:

- **Per-platform-ness is inherited, not invented.** The wheel is platform-tagged
  (`py3-none-manylinux_2_17_x86_64` for the Linux build), so each runner in a per-platform build matrix
  installs its own platform's engine without the build having to arrange it. Confirm `uv.lock` resolves
  wheels for every matrix platform before relying on this — `--frozen` will not fetch one that was never
  locked.
- **Collecting it adds ~297 MiB to every artefact.** That is the cost side of
  [`../plan/risks.md` § R-7](../plan/risks.md#r-7--bundled-cli-size-and-platform-specific-wheels), and the
  reason the choice stays a choice rather than a default: a self-sufficient binary at ~300 MiB+, or a small
  one that requires a `claude` on `PATH` and says so loudly when there is none.

**Build-time verification is required, and it landed with phase 7 (2026-08-27).** A CI smoke test asserts
that the bundled CLI is in the archive the build produced and that the binary answers `--version`. Both are
cheap, and both fail in the specific way this project has been bitten by before: a data file silently absent
from a bundle, producing a runtime error on a user's machine that no build-time test caught. **The
collection flags cannot police themselves** — a `--collect-all` for a package that is not installed only
warns, so the assertion has to look at what the build emitted rather than at what it was asked to emit.

Two consequences worth stating rather than discovering:

- **Version skew is a supported state, not an error.** A user's `PATH` CLI can be newer or older than the SDK's `__cli_version__` pin. Startup records both and warns on mismatch; it does not refuse to run. Refusing would make our release cadence a gate on theirs
- **The binary's version string describes AIC⚡DC only.** It says nothing about the engine, so bug reports need both — which is why `EngineHealth` carries `cli_path`, `cli_version`, `sdk_version`, and `sdk_cli_pin` together ([`../../specs-reference/3-engine/session.md`](../../specs-reference/3-engine/session.md) § `EngineHealth`)

### The SDK Brings a Server Stack We Do Not Serve

`claude-agent-sdk` requires `mcp` ≥ 1.29.0, and `mcp` in turn pulls `starlette`, `uvicorn`,
`sse-starlette`, `python-multipart`, `httpx-sse`, and `pydantic-settings` — the HTTP/SSE transport half
of the MCP protocol. AIC⚡DC's MCP server is **in-process** ([`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md)),
so none of that transport is used at runtime, and `pywin32` arrives for Windows only.

This is a packaging decision, not a resolution problem: the stack is dead weight in the binary unless
excluded, and excluding it means asserting that no code path reaches MCP's HTTP transport. Either choice
is defensible; making it silently is not, because a `--exclude-module` that turns out to be wrong fails
at MCP-server registration on a user's machine, which is the one code path every tool call goes through.

An earlier draft of this section claimed the floor collided with a `doc_convert` pin of `mcp` 1.14.1.
It does not, and there was never such a pin: `markitdown[all]` depends on `beautifulsoup4`,
`charset-normalizer`, `defusedxml`, `magika`, `markdownify`, and `requests`, none of which is `mcp`. The
1.14.1 sighting was `litellm`'s `proxy` extra in an unrelated virtualenv on the author's machine.
Adding the SDK added nine packages and changed the version of none — `doc_convert` re-tested green
(188 tests), full suite 3 480 passed.

### Release Workflow

- GitHub Actions workflow triggers on release tag
- Builds all platforms in parallel
- Attaches all platform binaries to the GitHub Release

## GitHub Pages Deployment

- For users running from source (`pip install -e .`) without a local webapp build
- Webapp deployed to a GitHub Pages URL
- Allows pip-install users to skip building the webapp manually — the Python backend redirects to the hosted webapp when no local bundle is found
- GitHub Actions workflow builds the webapp and deploys to Pages on push to main

## Source Install Paths

| Install type | Webapp source |
|---|---|
| PyInstaller binary | Bundled inside binary |
| Development (`pip install -e .` + `npm run build`) | Source tree's dist directory |
| pip install from PyPI | Installed package data (if included) or GitHub Pages fallback |
| Dev mode (`--dev`) | Vite dev server as child process |
| Preview mode (`--preview`) | Vite preview server as child process |

## Vite Dev/Preview Management

For dev and preview modes only (webapp development, not normal usage):

- Port check — skip if port already in use (another instance running)
- Prerequisite check — verify dependency directory exists, prompt for install if not
- Process lifecycle — launched as child process, terminated on exit
- Bind address — loopback by default, all interfaces when collaboration is enabled
- Cleanup — terminate with a timeout, then kill if needed

## Invariants

- Webapp bundle is always self-contained — no absolute paths, no external CDN dependencies for core features
- Base path is always relative so the bundle can serve from any origin
- PyInstaller binary contains every **Python-side** requirement — config defaults, webapp bundle, version file, all Python dependencies, and the SDK's bundled CLI as package data
- The binary never builds or substitutes for an engine of its own. Whether or not the SDK's bundled copy is collected, a missing engine degrades to a working editor with a health banner, never a failed launch ([startup.md](startup.md#engine-health-in-the-overlay))
- CI asserts the SDK's bundled CLI path is present in the built binary before the release is published
- Module collection covers every submodule of our own package without naming any of them — no "module not found" at runtime, and no list to keep in step with the tree
- SPA fallback ensures client-side routing works when users bookmark deep links
- Webapp location priority always tried in order; first hit wins
- Broken-pipe errors during client disconnect never surface as tracebacks