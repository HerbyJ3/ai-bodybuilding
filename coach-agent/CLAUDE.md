# CLAUDE.md

AI hypertrophy & physique coach agent. **Read `docs/BUILD_SPEC.md` before any work.** It is the implementation spec and milestone plan.

## Non-negotiables
- Rules engine is deterministic and pure; the LLM never computes training/nutrition numbers.
- All numbers come from `knowledge/training-defaults.json` and `knowledge/nutrition-defaults.json`. Never hardcode or invent them.
- Missing values: mark `"_provisional": true` and log them in `docs/OPEN_ITEMS.md`.
- Never commit client data, databases, or source PDFs (see `.gitignore`).
- Don't edit `persona/` or `knowledge/` content without owner approval.
- Items marked **[DECIDE]** in the spec: ask the owner first.

## Workflow
- Build milestones in order (BUILD_SPEC §10). Tests pass before moving on.
- Every set-progression matrix cell and every nutrition rule has a unit test.
- Synthetic test data only.
