---
name: consult-agy
description: Take a design question to Google Antigravity over several rounds until the positions converge, rather than accepting the first answer. Use when consulting the second opinion tool on an architecture choice, a diagnosis, a risky diff, or any decision worth attacking before committing to it — and whenever a single second opinion has already come back and the question is still open.
---

# Consulting Antigravity to convergence

`mcp__aic-dc-antigravity__second_opinion` reaches Google Antigravity on the
account holder's paid subscription and renders as a live tab in the
AIC⚡DC UI. **One call is one round.** A single round produces a plausible
essay; several rounds produce a decision. Treat the first answer as the
opening of a discussion, not as the deliverable.

This matters because the failure mode is invisible: a one-round answer is
fluent, confident, and frequently wrong in ways that only show up when
someone checks. Rounds are what convert an opinion into a finding.

## The protocol

**1. Put the current design, not a remembered one.** A consultation given
a stale description will converge confidently on a regression and sound
certain doing it. Read the code or the plan of record first, and describe
what is actually there.

**2. State your own position and ask it to attack that.** "Here is what I
believe and why — attack it" produces a sharper round than "what should I
do". Say which parts are measured and which are assumed.

**3. End every round by asking what it needs.** Invite follow-up questions
explicitly and offer to run probes on its behalf. It asks for specific,
checkable things — binary strings, flag behaviour, timing measurements.

**4. Run the probes and bring the measurements back.** This is the step
that makes the next round better rather than merely longer. Prefer running
them in a scratch directory or an isolated configuration so nothing of the
user's is touched.

**5. Check what it says.** It is confidently wrong often enough to matter:
in one session it recommended a CLI flag that does not exist, claimed to
have read documentation files that are not shipped, and called a failure
mode "most severe" that measurement then refuted. Wrong-but-checkable is
still useful. Wrong-and-adopted is not.

**6. Carry the thread as third-party evidence.** Each call is a cold start
with no memory of the last, so the prior exchange must be supplied in
`context` — and it should be attributed to "an earlier reviewer" rather
than to the model itself. A model shown its own prior words defends them;
the same content presented as somebody else's is evaluated on its merits.

**7. Stop when it converges.** Ask plainly whether it is done, and take
"we are done, no further probes are needed" at face value rather than
manufacturing another round. If the positions do not converge, state the
disagreement precisely enough to act on.

## What not to do

- **Do not resume a conversation to continue a consultation.** The one-shot
  cold start is a decision, not a limitation — see `specs5/plan-ag/decisions.md`
  AG-16. A reviewer that remembers agreeing with you is no longer
  independent, and `agy` silently compacts long conversations, so its memory
  of an early round is a summary nobody can audit.
- **Do not treat convergence as proof.** Two models agreeing is weaker
  evidence than one measurement. Where a claim is checkable, check it.
- **Do not let it write.** The consultant runs with a gate that permits no
  tools, so it answers from what you supply. That is deliberate.

## Recording the outcome

Findings belong in `specs5/plan-ag/` — decisions as `AG-n`, risks as
`AG-R-n`, the narrative in `delivery.md` — and anything left specified but
unwritten belongs in the unbuilt list in `specs5/plan-ag/README.md`, so the
work is discoverable and not only the reasoning.
