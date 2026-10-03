# Open Items

## Decisions needed
- [ ] LLM provider
- [ ] Initial maintenance formula (BMR × activity) before history exists. `maintenance.initial_estimate` raises until decided
- [ ] Persona name / branding
- [ ] DB encryption mechanism for production
- [ ] Auto-approval policy (post-v1)
- [ ] Tech stack: built with the BUILD_SPEC §4 defaults (Python 3.11, Pydantic v2, SQLite, pytest, Typer). Confirm

## Deferred
- [ ] Cold-start approach (BUILD_SPEC §13.5). Owner (2026-10-03): not needed soon; onboarding is mainly for reviewing and improving on existing history. Current code stays as a placeholder (gate applies to onboarded clients only, imported history counts, recovery held + flagged). Revisit before onboarding live clients

## Provisional (not from source books)
Values live in `config/engine-settings.json` (`_provisional: true`). Keys:
- [ ] `adherence_threshold_pct`: adherence that blocks calorie changes (85)
- [ ] `data_quality`: BUILD_SPEC §7.5 thresholds (intake 5/7 days, gap > 7 days, ±3 %BW/day, RIR 0–10)
- [ ] `confidence`: flag-count scoring (lookback 21 days; 1 flag → medium, 2+ or critical → low)
- [ ] `training`: cut set-addition cap for non-beginners (1), "multiple muscles" for a deload (2), linear RIR interpolation between week 1 and the final week
- [ ] `phase_transition`: hunger ≥ 4 or mean performance ≥ 2 counts as "fatigue" past the recommended cut length
- [ ] `maintenance_confidence`: intake days/weigh-ins per week for high/medium calibration confidence
- [ ] `cardio`: kcal/min by intensity when est_kcal is missing (BUILD_SPEC §7.4)
- [ ] `energy`: Atwater kcal/g (4/4/9)
- [ ] Cardio rules (BUILD_SPEC §7.4)
- [ ] Recomp phase guidance: the engine emits `flag` only
- [ ] Mini-cut rate ("slightly faster than a standard cut"): the engine emits `flag` only

## Interpretation choices (review)
- [ ] Performance derivation (§6.3) per set vs. the same set last week: load drop or fewer reps → 3; RIR below target → 2; +2 reps → 0; else 1
- [ ] Week-1 performance "1–2": the more conservative matrix cell is used
- [ ] Gain-phase *decreases* and cut-phase *increases* use fat→carbs (decrease) and carbs-first (increase). The books only state the cut-decrease and gain-increase orders
- [ ] Maintenance `stable_band_pct_bw` (1.25) is read as max drift over the assess window
- [ ] Cut→maintenance: only the first step (midpoint jump) is automated. The ~20 % step-ups every 3–4 weeks are left to the coach (base of "20 %" is ambiguous)
- [ ] Weeks are trailing 7-day windows ending at `as_of` (data quality, trend); meso/phase weeks count from their start event

## Not yet extracted
- [ ] RD2 Table 10.1 (maintenance calories, image in PDF)
- [ ] RD2 ch13–17, SPHT ch8
- [ ] Per-muscle starting volume estimates (not in SPHT; optional cold-start fallback)
