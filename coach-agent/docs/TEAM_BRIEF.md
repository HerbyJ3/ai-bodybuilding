# Team Brief — read this first

One page for every sub-agent. It replaces exploring the repo: read this, then **only the files your task brief names** (plus what you must open to make the change safely).

## What this is
**Mr. J**: an AI hypertrophy & physique coach for one human coach (the owner). A deterministic rules engine proposes training/nutrition changes; the coach approves them; Mr. J (Claude) explains approved changes in a calm, supportive voice. Everything runs **locally** on the owner's computer.

## Map (all paths under `coach-agent/`)
| Area | Paths | Owner agent |
|---|---|---|
| Specs & decisions | `docs/BUILD_SPEC.md`, `docs/OPEN_ITEMS.md`, this file | project-manager |
| Source-of-truth numbers | `knowledge/*.json` (**owner approval to edit**), `config/engine-settings.json` (non-book values, each with `_source`/`_provisional`) | engine-specialist |
| Persona & voice | `persona/` (**owner approval to edit**), `prompts/system.md` | ai-engineer |
| Data model | `schemas/` (events, state, proposals, onboarding) | backend-dev |
| Event store | `store/event_store.py` (append-only SQLite) | backend-dev |
| Rules engine | `engine/` (state_builder, training/, nutrition/, cardio*, data_quality, history_review, proposals) | engine-specialist |
| Imports & CLI | `ingest/` (csv_import, onboarding, myfitnesspal, cli.py = `coach` command) | backend-dev |
| Approval queue | `approvals/queue.py` | backend-dev |
| Mr. J (LLM) | `llm/` (client.py = only Claude code, prompt_builder, retrieval, tools, attachments, coach) | ai-engineer |
| Dashboard | `dashboard/app.py` (routes) — backend-dev; `dashboard/templates/`, `dashboard/static/` — frontend-dev | |
| Client app (local prototype, BUILD_SPEC §15) | `clientapp/app.py` (routes) — backend-dev; `clientapp/templates/`, `clientapp/static/` — frontend-dev | |
| Graphics | `dashboard/static/assets.json` (+ local copies via `coach fetch-assets`) | graphic-artist |
| Evals | `evals/scenarios.yaml`, `evals/run.py` (`coach eval [--llm]`) | ai-engineer / qa-tester |
| Tests | `tests/` (pytest) | qa-tester (+ whoever changes code) |
| Synthetic sample client | `samples/` (generator + committed CSVs) | qa-tester |
| Client data | `data/` — **gitignored, never commit, never read unless the task requires it** | everyone |

## Non-negotiables
1. The engine is pure and deterministic. **The LLM never computes numbers**; it calls engine tools.
2. Every number comes from `knowledge/*.json` or `config/engine-settings.json`. New values: add with `_provisional: true` + `_source`, list in `OPEN_ITEMS.md`.
3. **Don't change an existing rule without the owner's OK.** Items marked [DECIDE] go to the owner.
4. Mr. J only presents **approved** proposals. Limitations in the profile are hard constraints.
5. Client data stays in `data/` on the owner's machine. Synthetic data only in tests/samples. No real names in code, tests, commits or PRs.
6. Every rule and feature ships with tests. Never skip, weaken or delete a test to get green.
7. Commits: no model identifiers in messages, code or PRs.

## Conventions
- Python 3.11, Pydantic v2, SQLite, Typer CLI, FastAPI + Jinja dashboard bound to 127.0.0.1.
- Events are append-only and content-addressed (`make_event`); state is rebuilt from events; "latest wins" per day for intake, check-ins and daily target logs.
- Owner-facing text is plain language, no jargon.
- Charts follow the dataviz method (validated palette, light/dark, tooltip + table view).

## Run & check
```bash
cd coach-agent
python -m pytest -q          # full suite (must stay green)
python -m evals.run          # engine eval scenarios
coach dashboard --data-dir <dir> --no-open --port 8765   # local UI
```
Browser checks: Playwright with Chromium at `/opt/pw-browsers/chromium` (cloud sessions).

## Current state (keep updated — project-manager)
- Milestones M0–M9 built; dashboard, MyFitnessPal import, check-ins, Daily Target, chat uploads merged (PRs #1–#5).
- Owner decisions: provider Claude; coach name Mr. J; calm/supportive voice; 100 kcal/day step cap; minimum data = weigh-ins + macro targets; cardio lever before calories; book macro-cut order; cold start deferred.
- Client web app (owner 2026-10-05): dashboard = coach/admin view; clients get their own app. **In progress: local client-view prototype** (BUILD_SPEC §15).
- Open: DB encryption; live API checks (`coach checkin`, `coach eval --llm`) with the owner's key; provisional values in `OPEN_ITEMS.md`.

## Hand-off format (what each agent returns)
1. **Done:** what changed (files) — or what was found, for reviewers.
2. **Verified:** tests/commands run and their results.
3. **Open:** anything blocked, assumed, or needing the owner.
Keep it short; the coordinator relays what matters to the owner.
