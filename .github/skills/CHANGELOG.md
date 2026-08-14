# Agent Skills Changelog

Append-only decision log for this directory. One entry per skill admission,
rejection, or repair, newest last. Do not rewrite or delete past entries — add a
correcting entry instead. Rejected skills stay recorded so they are not
rediscovered and re-admitted later.

Every entry states: what changed, why it was admitted or rejected, and what
evidence backs the decision.

---

## 2026-08-14 — `premium-frontend-ui` admitted without a relevance gate

- **Source**: `github-awesome-copilot`, installed via `packageChild` of the bulk
  `skills` package (`premium-frontend-ui/.skill-meta.json`, `installedAt`
  `2026-08-14T09:40:12Z`).
- **Decision**: admitted by the installer. No per-skill review ran.
- **Assessment (retroactive)**: out of scope. This repository is a headless edge
  service — CLAUDE.md states it is "not a scheduler, UI, or business-logic
  service" — and the skill targets GitHub Copilot by name while prescribing
  GSAP, Framer Motion, Lenis and React Three Fiber. Nothing in
  `src/printer_agent/` is a web frontend.
- **Status**: left in place, marked out of scope in README.md, pending removal.
- **Rule going forward**: bulk package installs must be expanded to per-skill
  admissions. A skill is admitted only if its activation scenario intersects
  work that actually happens in this repository.

## 2026-08-14 — Repair: generator overwrote a locked surface

- **What happened**: the `agent-ninja` markers in `AGENTS.md` wrapped the entire
  file. A regeneration replaced the whole span, deleting the 15-line
  "Working Rules For This Repository" section. The same run replaced the
  hand-written skill-routing list in this directory's README.md with a generated
  table.
- **Guidance lost**: most rules were duplicated in CLAUDE.md, but four were not —
  one stable printer identity per location; `msg_id` must not be the only dedupe
  mechanism for printer state; single active owner (lease/claim) when multiple
  agents observe one printer; protocol-agnostic dedupe keys outside adapters.
  The pointer to `docs/data-storage-and-dedup.md` was also lost. Any agent
  reading only `AGENTS.md` had zero repository rules.
- **Detected**: the changes were still uncommitted, so `git diff` recovered them.
  That was luck, not a review gate.
- **Repair**: restored the working-rules section verbatim from
  `git show HEAD:AGENTS.md`, moved `agent-ninja-END` up so the generated span
  covers only the "Agent Skills" pointer, and added guard comments to both files.
- **Rule going forward**: `AGENTS.md` working rules and this directory's routing
  notes are a locked surface. Generated spans stay scoped to their own section.
  A diff that removes lines from either requires human approval before commit.

## 2026-08-14 — Generator output was malformed

- The README table cell spliced two truncated fields joined by an escaped pipe
  (`...experiences wit... \| As an AI engineering assistant...`), and
  `whenToUse` in `.skill-meta.json` is cut mid-word (`This skil...`).
- The cell was rewritten by hand; it will regress on the next regeneration.
- **Rule going forward**: generated output needs a structure check — one
  description field per row, no truncation mid-word, no unescaped or
  double-escaped table delimiters.
