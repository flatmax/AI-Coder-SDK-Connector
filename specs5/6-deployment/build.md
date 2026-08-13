# Build

How the webapp and backend are packaged for distribution. The webapp is a Vite-built SPA served by a built-in HTTP static file server; the backend is a Python package optionally packaged as a PyInstaller single-file binary. Source installs can use a fallback served from GitHub Pages.

**The single-file binary is no longer self-sufficient**, and that is the headline of this file after the
conversion. AC⚡DC used to contain its own inference client: bundle `litellm`, ship the binary, and a user
with an API key had a working application. Now the engine is a separate Node process the user must have —
the `claude` CLI. No amount of PyInstaller flags changes that, so the packaging story becomes "bundle
everything we can and diagnose the one thing we cannot" (see
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

- `--collect-all` for packages with data files — the agent SDK, tree-sitter core and per-language grammars, content extraction library
- `--hidden-import` for every ac_dc submodule and extractor (static analysis misses dynamically imported modules and modules only referenced via class registration)
- `--hidden-import` for the RPC library
- Runtime behavior verified in CI on each platform

Removed from collection: the LLM provider library and its provider SDKs, and the tokenizer with its
encoding data files. Both were large — the tokenizer's encodings and the provider library's model registry
were a meaningful fraction of the binary — and both existed to do work the engine now does. Nothing
counts tokens in this process any more (see
[`../3-engine/context-visibility.md`](../3-engine/context-visibility.md)).

The agent SDK needs `--collect-all` rather than a plain hidden import because it ships a data file that
matters: a bundled `claude` CLI under `claude_agent_sdk/_bundled/`. See below.

### The Engine Is Not Bundleable

The `claude` CLI is a Node application. PyInstaller bundles Python; it cannot make a Node runtime appear,
and shipping one would mean carrying a second language runtime plus a CLI whose release cadence is not
ours. So the binary ships without an engine and resolves one at startup, in the SDK transport's order:

1. An explicitly configured `cli_path` from `engine.json`
2. `claude` on `PATH`
3. The SDK's own bundled copy under `claude_agent_sdk/_bundled/claude`

Option 3 is the reason for `--collect-all` on the SDK: the bundled copy is package data that a plain
import collection would drop, and dropping it removes the fallback that makes a fresh install work at all.
It still needs a Node runtime present, so it is a fallback rather than a solution.

**Build-time verification is required.** A CI smoke test must assert that the bundled CLI path exists
inside the built binary and that `--version` runs. Both are cheap, and both fail in the specific way this
project has been bitten by before: a data file silently absent from a bundle, producing a runtime error on
a user's machine that no build-time test caught.

Two consequences worth stating rather than discovering:

- **Version skew is a supported state, not an error.** A user's `PATH` CLI can be newer or older than the SDK's `__cli_version__` pin. Startup records both and warns on mismatch; it does not refuse to run. Refusing would make our release cadence a gate on theirs
- **The binary's version string describes AC⚡DC only.** It says nothing about the engine, so bug reports need both — which is why `EngineHealth` carries `cli_path`, `cli_version`, `sdk_version`, and `sdk_cli_pin` together ([`../../specs-reference/3-engine/session.md`](../../specs-reference/3-engine/session.md) § `EngineHealth`)

### The `mcp` Version Floor Is a Build Constraint

`claude-agent-sdk` requires `mcp` ≥ 1.29.0. The pre-conversion lockfile pinned 1.14.1, and `doc_convert`
depends on that package too — so the upgrade is a deliberate step with `doc_convert` re-tested, not a
side effect of installing the SDK. The lockfile refresh and the doc-convert regression run belong in the
same commit, because splitting them produces a green build with a broken feature.

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
- The binary does **not** contain a Node runtime and does not pretend to. A missing engine degrades to a working editor with a health banner, never a failed launch ([startup.md](startup.md#engine-health-in-the-overlay))
- CI asserts the SDK's bundled CLI path is present in the built binary before the release is published
- Hidden imports cover every module — no "module not found" at runtime
- SPA fallback ensures client-side routing works when users bookmark deep links
- Webapp location priority always tried in order; first hit wins
- Broken-pipe errors during client disconnect never surface as tracebacks