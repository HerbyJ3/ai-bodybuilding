# Mr. J — hypertrophy & physique coach agent

A deterministic rules engine (training + nutrition) with coach approval and a Claude-powered coach voice ("Mr. J"). Spec: `docs/BUILD_SPEC.md`. Open questions: `docs/OPEN_ITEMS.md`.

## Setup
```bash
cd coach-agent
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest                                    # run the tests
```
For the Mr. J commands (`checkin`, `chat`), set an Anthropic API key: `export ANTHROPIC_API_KEY=...`.

## Try it with the synthetic sample client
```bash
coach onboard samples/mid_cut_client/onboarding.json --report data/report.json
coach review SYN-CUT-01 2026-09-28            # what progressed, what stalled
coach queue refresh SYN-CUT-01 2026-09-28     # engine proposals -> approval queue
coach queue list SYN-CUT-01
coach queue show <id>                          # inputs, confidence, rationale
coach queue approve <id> --note "start Monday" # or: reject --note / modify --value '{...}' --note
coach prompt SYN-CUT-01 2026-09-28            # what Mr. J sees (no API call)
coach checkin SYN-CUT-01 2026-09-28           # Mr. J writes the check-in (needs API key)
coach chat SYN-CUT-01 2026-09-28              # ask Mr. J questions (needs API key)
```
Data lives in `data/coach.db` (gitignored). Never commit client data.

## A real client
1. Export their history to CSV (weigh-ins, food, sets, check-ins, optional cardio/ratings).
2. Copy `samples/mid_cut_client/mapping.json` and match your column names.
3. Copy `samples/mid_cut_client/onboarding.json`; fill in client id, date, consent, phase/week, meso week, last deload, optional past mesos/phases.
4. `coach onboard your-onboarding.json`, then `coach review`, `coach queue refresh`, approve, `coach checkin`.

Mr. J only ever presents changes you approved, and every number comes from the engine.
