# Known issues

The raw inbox — defects as they are noticed, unsorted. Triaged entries are carried into
[`next.md`](next.md) § C and leave here when they are fixed.

*Was empty as of 2026-08-29.* The prior entry — `specs-reference/5-webapp/viewers-hud.md` § *Cost
rendering* describing the pre-phase-6 HUD — was fixed the same day, and fixing it found two adjacent
sections stale from the same cause and one specified surface with nothing building it. See
[`next.md`](next.md) § B6.

---

## The token-refresh ladder cannot tell "did not need refreshing" from "could not refresh"

Reported 2026-09-10 from a live session: the health banner showed *"The Claude subscription access
token is expired or expiring and could not be refreshed automatically. Run `claude` in a terminal to
renew it, then restart the engine."* — the failure detail from
`aic_dc.claude_code.token_refresh.ensure_fresh`. The user ran `claude` in a terminal, typed `hi`, and
the engine worked again.

`ensure_fresh` decides a rung worked by re-reading `claudeAiOauth.expiresAt` and requiring it to have
moved forward. That test is only meaningful when the CLI itself considers a refresh due, and measurement
says it does not consider it due merely because we do:

- `claude auth status --json` exits 0 and prints account metadata (`loggedIn`, `authMethod`, `email`,
  `subscriptionType`). It carries no expiry field and did not rotate the token.
- `claude -p hi --max-turns 1` exits 0 in ~3s against a healthy token (7.9 h of life left) and left
  `expiresAt` byte-identical.

So with a *healthy* token both rungs "succeed" and neither moves the expiry. Our `REFRESH_MARGIN_SECONDS`
is 15 minutes; the CLI's own refresh threshold is unknown and narrower on the evidence above. Every
connect that lands in the gap between the two margins runs both rungs, sees no movement, and reports a
refresh failure for a token that is fine. The banner is indistinguishable from the real failure — a
lapsed `refreshTokenExpiresAt`, which needs an interactive re-login and which no rung can fix.

Two consequences worth separating when this is fixed:

1. **The success test is wrong.** "Did `expiresAt` move" should not be the criterion. A token that is
   still ahead of `now` after the ladder ran has not failed; only a token past its own `expiresAt`, or a
   `refreshTokenExpiresAt` in the past, is a real degradation. As written the banner cries wolf on
   ordinary restarts, which is the fastest way to teach a user to ignore it.
2. **The remedy is a terminal instruction.** For the one case that genuinely needs a human — an expired
   refresh token — the CLI has `claude auth login` (`--claudeai`, `--console`, `--email`, `--sso`), and
   the engine already spawns the CLI for cheaper reasons. Surfacing that flow's URL in the banner would
   replace "go open a terminal and restart the engine" with a click. `claude setup-token` (long-lived
   token, subscription only) and `apiKeyHelper` are the two standing alternatives that sidestep the
   ~8-hour cycle entirely.

Not an SDK gap, and not fixable by a version bump: `claude-agent-sdk` exposes no auth surface at all,
and `_internal/session_resume.py::_write_redacted_credentials` strips `refreshToken` from the temp
`CLAUDE_CONFIG_DIR` on purpose, so the CLI child can never renew itself. Confirmed against the
changelog for 0.2.137 → 0.2.152 (latest): nothing between them touches OAuth, credentials, or refresh.
