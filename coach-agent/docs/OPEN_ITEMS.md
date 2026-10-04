# Open Items

## Decisions needed
- [x] LLM provider: **Claude** (Anthropic API), owner decision 2026-10-03
- [ ] Initial maintenance formula (BMR × activity) before history exists. `maintenance.initial_estimate` raises until decided
- [x] Persona name: **Mr. J** (2026-10-03). Branding still open
- [ ] DB encryption mechanism for production
- [ ] Auto-approval policy (post-v1)
- [ ] Tech stack: built with the BUILD_SPEC §4 defaults (Python 3.11, Pydantic v2, SQLite, pytest, Typer). Confirm

## Deferred
- [ ] Cold-start approach (BUILD_SPEC §13.5). Owner (2026-10-03): not needed soon; onboarding is mainly for reviewing and improving on existing history. Current code stays as a placeholder (gate applies to onboarded clients only, imported history counts, recovery held + flagged). Revisit before onboarding live clients

## Decided 2026-10-04 (from the first real-data test)
- [x] **Calorie step cap:** each adjustment is capped at 100 kcal/day (`calorie_step_cap`; lowered from 250 the same day to match the ~25 g carb steps that worked in practice); the full gap is kept in `inputs_used` and flagged `step_capped`
- [x] **Minimum data = weigh-ins + macro targets; everything else optional.** No check-in no longer holds: the adjustment is proposed with `no_recent_checkin` and confidence capped at medium. No macro targets → hold. (Replaces the same-day "no check-in → hold" decision.) The "recent" window is provisional (`adherence`)
- [x] **Day type per client:** the coach labels each client's days (e.g. training day = `light`) in the targets data. Onboarding warns and proposals flag `carbs_below_day_type_minimum:<day>` when a day's carbs are already below its book minimum
- [x] **Macro cut order stays the book default:** fat down to its floor first, then carbs, applied to every day type (not "carbs only, fat pinned" or training days only)
- [x] **Cardio as a lever** in a cut, before calories: frequency, then duration, each only if it clears the noise floor; same modality (limitation-safe); coach-approved. Per-client ceilings via onboarding `cardio_max_sessions_per_week` / `cardio_max_minutes_per_session`. Amends BUILD_SPEC §7.4

## Provisional (not from source books)
Values live in `config/engine-settings.json` (`_provisional: true`). Keys:
- [ ] `adherence_threshold_pct`: adherence that blocks calorie changes (85)
- [ ] `data_quality`: BUILD_SPEC §7.5 thresholds (intake 5/7 days, gap > 7 days, ±3 %BW/day, RIR 0–10)
- [ ] `confidence`: flag-count scoring (lookback 21 days; 1 flag → medium, 2+ or critical → low). Too few weigh-ins in the current week is critical, so the BUILD_SPEC §11 "one weigh-in → low, hold" holds regardless of other data
- [ ] `training`: cut set-addition cap for non-beginners (1), "multiple muscles" for a deload (2), linear RIR interpolation between week 1 and the final week
- [ ] `phase_transition`: hunger ≥ 4 or mean performance ≥ 2 counts as "fatigue" past the recommended cut length
- [ ] `maintenance_confidence`: intake days/weigh-ins per week for high/medium calibration confidence
- [ ] `cardio`: kcal/min by intensity when est_kcal is missing (BUILD_SPEC §7.4)
- [ ] `energy`: Atwater kcal/g (4/4/9)
- [ ] `adherence`: a check-in counts as recent for 14 days; daily "Macros hit?" logs give adherence % (hit days / logged days, last 7 days, needs ≥ 3 logged days) when the check-in has no adherence number
- [ ] Dashboard check-in mapping: hunger/energy low/mid/high stored as 1/3/5 on the engine's 1–5 scale (high hunger ≥ 4 counts as diet fatigue); sleep stored as hours
- [ ] `cardio_lever`: default ceilings (5 sessions/week, 45 min/session), +10 min duration step, 0.1 lb/week noise floor, 14-day lookback
- [ ] Cardio rules (BUILD_SPEC §7.4)
- [ ] Recomp phase guidance: the engine emits `flag` only
- [ ] Mini-cut rate ("slightly faster than a standard cut"): the engine emits `flag` only

## Interpretation choices (review)
- [ ] Performance derivation (§6.3) per set vs. the same set last week: load drop or fewer reps → 3; RIR below target → 2; +2 reps → 0; else 1
- [ ] Week-1 performance "1–2": the more conservative matrix cell is used
- [ ] Gain-phase *decreases* and cut-phase *increases* use fat→carbs (decrease) and carbs-first (increase). The books only state the cut-decrease and gain-increase orders
- [ ] Maintenance `stable_band_pct_bw` (1.25) is read as max drift over the assess window
- [ ] Cut→maintenance: only the first step (midpoint jump) is automated. The ~20 % step-ups every 3–4 weeks are left to the coach (base of "20 %" is ambiguous)
- [ ] History review (§14): rotation candidate = kept ≥ 2 consecutive complete mesos and flat/regressed in the latest; "best set" = heaviest then most reps (no e1RM); across-meso comparison at the latest common meso week (RIR may differ when meso lengths differ)
- [ ] Weeks are trailing 7-day windows ending at `as_of` (data quality, trend); meso/phase weeks count from their start event

## MyFitnessPal import
- [ ] Column names follow MyFitnessPal's documented Premium export and Printable Diary; verify against a real export the first time (headers are matched loosely, by meaning)
- [ ] Imported cardio has no intensity in the export; recorded as `mod` (only matters when est_kcal is missing)
- [ ] Body-fat and other measurements in the Progress file are reported but not stored (no event type yet)

## LLM layer (M8)
- [ ] Retrieval is BM25 keyword search, not embeddings (no Anthropic embeddings endpoint). Revisit if the knowledge corpus grows
- [ ] Not yet run against the live API from the build environment (no key there). Run `coach checkin` once with a real key and review the tone against the reference example
- [ ] Run `coach eval --llm` with a real key (6 scenarios ≈ 6–8 coach calls + 6 judge calls). The judge uses the same model as Mr. J; consider a human spot-check of verdicts the first time
- [ ] Effort `medium` for chat (model default). Raise to `high` if answers feel shallow

## Not yet extracted
- [ ] RD2 Table 10.1 (maintenance calories, image in PDF)
- [ ] RD2 ch13–17, SPHT ch8
- [ ] Per-muscle starting volume estimates (not in SPHT; optional cold-start fallback)
