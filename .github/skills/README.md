<!-- agent-ninja-START -->
## Agent Skills

> **IMPORTANT**: Prefer skill-led reasoning over pre-training-led reasoning.
> Read the relevant SKILL.md before working on tasks covered by these skills.

### Skills

| Skill | Description |
|-------|-------------|
| [premium-frontend-ui](./premium-frontend-ui/SKILL.md) | A comprehensive guide for GitHub Copilot to craft immersive, high-performance web experiences wit... \| As an AI engineering assistant, your role when building premium frontend experiences goes beyond ... |

<!-- agent-ninja-END -->

<!--
  Everything below is hand-authored and MUST stay outside the agent-ninja markers.
  The span between START and END is regenerated from installed skill directories
  and does not preserve edits. See CHANGELOG.md.
-->

## Repository Skill Routing

Skill availability depends on which agent is running. The generated table above
lists only what is vendored into this directory; it is not the full picture.

### Repo-vendored (`.github/skills/`, visible to every agent)

- `premium-frontend-ui` — **out of scope for this repository.** Installed
  2026-08-14 as a child of a bulk `github-awesome-copilot` package with no
  per-skill relevance check. `printer-agent` is a headless edge service whose
  only UI is a local config editor; the skill prescribes GSAP, Framer Motion and
  React Three Fiber. Do not apply it here. Pending removal.

### IDE-provided (VS Code extensions — visible to Copilot / VS Code chat, not to the Claude Code CLI)

- `python-fact-grounded-coding` — Python debugging and changes that should be
  grounded in runtime or diagnostics output.
- `pylance-refactoring` — safe Python refactors and import cleanup.
- `pylance-python-profiling` — profiling Python execution.
- `pylance-docs` — Python API and library documentation lookup.

Earlier revisions of this file also named `managing-python-dependencies` and
`project-setup-info-local`. Neither is installed in this workspace or in any
VS Code extension present on it; treat those references as stale.

If a task falls outside these areas, follow the repository instructions in
[AGENTS.md](../../AGENTS.md) and keep changes small and testable.
