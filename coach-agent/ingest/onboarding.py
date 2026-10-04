"""Mid-program onboarding (BUILD_SPEC §13).

1. Record consent (refuse to ingest without it).
2. Import the client's history from CSV (source=import).
3. Write backdated phase_started / meso_started / deload_completed events
   (source=import) so the phase week and RIR schedule resume where the client is.
4. Run data_quality over the imported history and report gaps.
5. Report cold-start gate status and the first proposals.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from config.loader import Config
from engine import cold_start, history_review, proposals
from engine.state_builder import build_state
from engine.training.mesocycle import rir_target, validate_meso_length
from engine.util import week_index
from ingest.csv_import import import_csvs, load_mapping
from schemas.events import Event, Source, make_event
from schemas.onboarding import OnboardingConfig
from store.event_store import EventStore


class OnboardingError(ValueError):
    pass


@dataclass
class OnboardingReport:
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2, default=str)


def _at(d: date, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=timezone.utc)


def _deload_days(cfg: Config) -> int:
    length = cfg.training("fatigue_management.deload.length")
    m = re.fullmatch(r"(\d+) weeks?", length.strip())
    if not m:
        raise OnboardingError(f"cannot parse deload length '{length}'")
    return int(m.group(1)) * 7


def _infer_meso_layout(sets: list[Event], meso_start: date, as_of: date
                       ) -> tuple[dict[str, list[str]], dict[str, int], str] | None:
    """Exercises and set counts per muscle from meso week 1; falls back to the
    most recent full week of logged sets."""
    def layout(lo: date, hi: date) -> tuple[dict[str, list[str]], dict[str, int]] | None:
        ex: dict[str, list[str]] = defaultdict(list)
        n: dict[str, int] = defaultdict(int)
        for e in sets:
            if lo <= e.day < hi:
                p = e.payload
                if p.exercise_id not in ex[p.muscle]:
                    ex[p.muscle].append(p.exercise_id)
                n[p.muscle] += 1
        return (dict(sorted(ex.items())), dict(sorted(n.items()))) if n else None

    wk1 = layout(meso_start, min(meso_start + timedelta(days=7), as_of + timedelta(days=1)))
    if wk1:
        return (*wk1, "meso_week_1")
    last_full_start = meso_start + timedelta(days=7 * ((as_of - meso_start).days // 7 - 1))
    if last_full_start >= meso_start:
        latest = layout(last_full_start, last_full_start + timedelta(days=7))
        if latest:
            return (*latest, f"week_of_{last_full_start.isoformat()}")
    return None


def _current_meso(oc: OnboardingConfig, imported: list[Event], cfg: Config,
                  recorded_at: datetime, warnings: list[str]
                  ) -> tuple[list[Event], dict[str, Any] | None, date | None]:
    m, cid, as_of, src = oc.meso, oc.client_id, oc.as_of, Source.import_
    if m is None:
        warnings.append("no mesocycle given: training rules (set progression, RIR, deload) "
                        "stay inactive until a meso is started")
        return [], None, None
    if m.current_week > m.weeks_planned + 1:
        raise OnboardingError(
            f"meso week {m.current_week} is beyond {m.weeks_planned} accumulation weeks + deload")
    warnings += validate_meso_length(m.weeks_planned, cfg)
    meso_start = as_of - timedelta(days=7 * (m.current_week - 1))

    events: list[Event] = []
    deload_info: dict[str, Any] = {"last_deload_date": m.last_deload_date}
    if m.last_deload_date is not None:
        if m.last_deload_date >= meso_start:
            raise OnboardingError(
                f"last deload ({m.last_deload_date}) is on/after the derived meso start "
                f"({meso_start}); check current_week")
        gap = (meso_start - m.last_deload_date).days - 1
        if gap > cfg.setting("data_quality.max_gap_days"):
            warnings.append(f"{gap} days between last deload and meso start: "
                            "check current_week / last_deload_date")
        start = m.last_deload_date - timedelta(days=_deload_days(cfg) - 1)
        events.append(make_event(cid, "deload_completed", _at(m.last_deload_date, 12),
                                 {"start_date": start, "end_date": m.last_deload_date},
                                 src, recorded_at))
        deload_info["weeks_since_deload"] = (as_of - m.last_deload_date).days // 7
    else:
        warnings.append("no last deload date: fatigue history before this meso is unknown")

    inferred: list[str] = []
    ex, sets_ = m.exercises_per_muscle, m.starting_sets_per_muscle
    if ex is None or sets_ is None:
        logged = [e for e in imported if e.type == "set_logged"]
        guess = _infer_meso_layout(logged, meso_start, as_of)
        if guess is None:
            raise OnboardingError("no set logs to infer exercises/sets; provide them in meso config")
        if ex is None:
            ex = guess[0]
            inferred.append(f"exercises_per_muscle from {guess[2]}")
        if sets_ is None:
            sets_ = guess[1]
            inferred.append(f"starting_sets_per_muscle from {guess[2]}")
    events.append(make_event(cid, "meso_started", _at(meso_start),
                             {"meso_id": m.meso_id, "weeks_planned": m.weeks_planned,
                              "exercises_per_muscle": ex, "starting_sets_per_muscle": sets_},
                             src, recorded_at))
    info = {"meso_id": m.meso_id, "start_date": meso_start, "current_week": m.current_week,
            "weeks_planned": m.weeks_planned,
            "rir_target_this_week": rir_target(m.current_week, m.weeks_planned, cfg),
            "rir_target_next_week": rir_target(m.current_week + 1, m.weeks_planned, cfg),
            "inferred": inferred, **deload_info}
    return events, info, meso_start


def build_onboarding_events(oc: OnboardingConfig, imported: list[Event], cfg: Config,
                            recorded_at: datetime) -> tuple[list[Event], dict[str, Any], list[str]]:
    cid, as_of, src = oc.client_id, oc.as_of, Source.import_
    warnings: list[str] = []
    ph = oc.phase
    phase_start = as_of - timedelta(days=7 * (ph.current_phase_week - 1))
    if ph.current_phase_week > ph.planned_weeks:
        warnings.append(f"phase week {ph.current_phase_week} > planned {ph.planned_weeks}")

    meso_events, meso_info, meso_start = _current_meso(oc, imported, cfg, recorded_at, warnings)
    events: list[Event] = []
    logged_sets = [e for e in imported if e.type == "set_logged"]
    prev_start: date | None = None
    for pm in sorted(oc.past_mesos, key=lambda x: x.start_date):
        if meso_start is not None and pm.start_date >= meso_start:
            raise OnboardingError(f"past meso {pm.meso_id} starts on/after the current meso")
        if oc.meso is not None and pm.meso_id == oc.meso.meso_id:
            raise OnboardingError(f"past meso id {pm.meso_id} duplicates the current meso id")
        if prev_start is not None and pm.start_date == prev_start:
            raise OnboardingError(f"two past mesos start on {pm.start_date}")
        prev_start = pm.start_date
        accum_end = pm.start_date + timedelta(days=7 * pm.weeks_planned - 1)
        layout = _infer_meso_layout(logged_sets, pm.start_date, accum_end)
        events.append(make_event(cid, "meso_started", _at(pm.start_date), {
            "meso_id": pm.meso_id, "weeks_planned": pm.weeks_planned,
            "exercises_per_muscle": layout[0] if layout else {},
            "starting_sets_per_muscle": layout[1] if layout else {}}, src, recorded_at))
        if layout is None:
            warnings.append(f"past meso {pm.meso_id}: no set logs in its first week")
    for pp in sorted(oc.past_phases, key=lambda x: x.start_date):
        if pp.start_date >= phase_start:
            raise OnboardingError(f"past {pp.phase} phase starts on/after the current phase")
        events.append(make_event(cid, "phase_started", _at(pp.start_date), {
            "phase": pp.phase, "target_rate_pct_bw": pp.target_rate_pct_bw,
            "planned_weeks": pp.planned_weeks}, src, recorded_at))

    events.append(make_event(cid, "phase_started", _at(phase_start),
                             {"phase": ph.phase, "target_rate_pct_bw": ph.target_rate_pct_bw,
                              "planned_weeks": ph.planned_weeks}, src, recorded_at))
    events += meso_events
    profile = {k: getattr(oc, k) for k in ("training_age", "cardio_max_sessions_per_week",
                                           "cardio_max_minutes_per_session") if getattr(oc, k) is not None}
    if profile:
        events.append(make_event(cid, "profile_updated", _at(as_of), profile, src, recorded_at))
    if oc.nutrition_targets:
        events.append(make_event(cid, "nutrition_targets_set", _at(as_of),
                                 {"macros_by_day_type": oc.nutrition_targets,
                                  "note": "current targets at onboarding"}, src, recorded_at))
    ids = [lim.limitation_id for lim in oc.limitations]
    if len(ids) != len(set(ids)):
        raise OnboardingError("duplicate limitation_id")
    for lim in oc.limitations:
        events.append(make_event(cid, "limitation_recorded", _at(lim.since or as_of), {
            "limitation_id": lim.limitation_id, "area": lim.area, "description": lim.description,
            "restrictions": lim.restrictions}, src, recorded_at))
    phase_info = {**ph.model_dump(), "start_date": phase_start}
    return events, {"meso": meso_info, "phase": phase_info,
                    "limitations": [lim.model_dump() for lim in oc.limitations]}, warnings


def onboard(store: EventStore, oc: OnboardingConfig, cfg: Config, base_dir: Path | None = None,
            recorded_at: datetime | None = None) -> OnboardingReport:
    recorded_at = recorded_at or datetime.now(timezone.utc)
    base = base_dir or Path(".")
    cid, as_of = oc.client_id, oc.as_of

    if not oc.consent.granted:
        raise OnboardingError("consent not granted; nothing ingested")
    consent = make_event(cid, "consent_recorded", _at(as_of), oc.consent.model_dump(),
                         Source.coach, recorded_at)

    mapping = load_mapping(base / oc.history.mapping)
    result = import_csvs(cid, base / oc.history.csv_dir, mapping, recorded_at)
    future = [e for e in result.events if e.day > as_of]
    imported = [e for e in result.events if e.day <= as_of]

    ob_events, info, warnings = build_onboarding_events(oc, imported, cfg, recorded_at)
    if future:
        warnings.append(f"{len(future)} imported rows dated after as_of were skipped")

    # Data quality over the imported history plus the backdated structure.
    pre_state = build_state([consent, *imported, *ob_events], as_of, cfg)
    dq_codes = sorted({f.code for f in pre_state.data_quality})
    counts: dict[str, int] = defaultdict(int)
    for e in imported:
        counts[e.type] += 1
    done = make_event(cid, "onboarding_completed", _at(as_of, 23, 59, 59), {
        "as_of": as_of, "meso_id": oc.meso.meso_id if oc.meso else None,
        "current_meso_week": oc.meso.current_week if oc.meso else None,
        "last_deload_date": oc.meso.last_deload_date if oc.meso else None, "phase": oc.phase.phase,
        "current_phase_week": oc.phase.current_phase_week,
        "imported_event_counts": dict(sorted(counts.items())), "data_quality_codes": dq_codes,
    }, Source.import_, recorded_at)

    inserted, dupes = store.append([consent, *imported, *ob_events, done])
    all_events = store.read(cid)
    state = build_state(all_events, as_of, cfg)
    props = proposals.generate(state, cfg)
    if state.weight.latest_avg_lb:
        from engine.nutrition.adjustment import carbs_below_minimum
        for flag in carbs_below_minimum(state, state.weight.latest_avg_lb, cfg):
            day_type = flag.split(":", 1)[1]
            warnings.append(f"current {day_type}-day carbs are below the book minimum for '{day_type}' "
                            "days: check which day type this client's day really is")
    review = history_review.review(all_events, state, cfg)

    flags = [f.model_dump() for f in state.data_quality]
    by_code: dict[str, int] = defaultdict(int)
    for f in state.data_quality:
        by_code[f.code] += 1
    nut = cold_start.nutrition_status(state, cfg)
    minimum = {"weigh_ins": {"ok": nut.ready, "weeks": nut.have_weeks, "needed": nut.need_weeks},
               "macro_targets": {"ok": bool(state.current_macros)}}
    if not minimum["macro_targets"]["ok"]:
        warnings.append("minimum data missing: no macro targets (add nutrition_targets or a targets CSV)")
    if not nut.ready:
        warnings.append(f"minimum data missing: {nut.have_weeks}/{nut.need_weeks} weeks of weigh-ins")
    optional_missing = sorted(label for t, label in history_review.COLLECTED_STREAMS.items()
                              if t != "weigh_in" and not any(e.type == t for e in all_events))
    train = cold_start.training_status(state, cfg)
    return OnboardingReport({
        "client_id": cid,
        "as_of": as_of,
        "events_written": inserted,
        "duplicates_ignored": dupes,
        "import": result.summary(),
        "onboarding_events": [{"type": e.type, "timestamp": e.timestamp, "source": e.source.value}
                              for e in [*ob_events, done]],
        **info,
        "warnings": warnings,
        "data_quality": {
            "counts_by_code": dict(sorted(by_code.items())),
            "gaps": [f for f in flags if f["code"] == "stream_gap"],
            "flags": flags,
        },
        "minimum_data": minimum,
        "optional_data_missing": optional_missing,
        "cold_start": {
            "nutrition": {"ready": nut.ready, "weeks_of_weigh_ins": nut.have_weeks,
                          "needed": nut.need_weeks},
            "training": {mu: {"ready": g.ready, "rated_weeks": g.have_weeks, "needed": g.need_weeks}
                         for mu, g in train.items()},
            "training_gate_applies": cold_start.training_applies(state, cfg),
        },
        "history_review": review,
        "proposals": [{"rule_id": p.rule_id, "target": p.target, "action": p.action,
                       "confidence": p.confidence, "rationale": p.rationale_short,
                       "flags": p.data_quality_flags} for p in props],
    })


def load_onboarding_config(path: str | Path) -> OnboardingConfig:
    with open(path, encoding="utf-8") as f:
        return OnboardingConfig.model_validate(json.load(f))
