# CLAUDE.md (repo root)

The project lives in `coach-agent/`. Read `coach-agent/CLAUDE.md` (rules) and `coach-agent/docs/BUILD_SPEC.md` (spec) before any work.

## Team of sub-agents (`.claude/agents/`)
Roster: project-manager, graphic-artist, frontend-dev, backend-dev, engine-specialist, ai-engineer, qa-tester, code-reviewer, privacy-guardian, docs-writer. Shared context for all of them: `coach-agent/docs/TEAM_BRIEF.md`.

**When to use the team (owner's rules):**
- **Only when the owner says "team"** (or names a specific agent, e.g. "use the graphic artist"). Otherwise the main session does the work directly. "No team" / "just you" / "quick fix" always means no agents.
- **Exception — privacy-guardian runs on every PR**, team or not. Don't open a PR on a BLOCK.
- **graphic-artist only on request**; it preflights Higgsfield cost and asks before spending more than 5 credits per task.
- With the team: project-manager plans and writes task briefs; the main session dispatches agents (parallel where independent), then qa-tester → code-reviewer → privacy-guardian before the PR.
- Say at the start of a task whether the team is being used.
