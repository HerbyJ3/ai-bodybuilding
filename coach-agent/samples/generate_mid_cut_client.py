"""Generates the synthetic mid-cut sample client (BUILD_SPEC §13.6).

SYNTHETIC DATA ONLY. Deterministic (seeded) so tests can compare the committed
CSVs with a fresh run:  python -m samples.generate_mid_cut_client

Story: SYN-CUT-01 moves from another app on 2026-09-28 (Monday). They are in
week 7 of a 12-week cut (target 0.75 %BW/wk, actually losing ~0.35 %/wk) and
week 3 of a 5-week meso that started after a deload ending 2026-09-13.
Built-in data problems the onboarding report must surface:
  - 2026-07-09..07-19 vacation: gap in every stream
  - 2026-06-24 weigh-in typo (129.4 for 192.4): implausible weight
  - week of 2026-08-03: one weigh-in only
  - week of 2026-08-24: intake logged on 3 days
  - the old app never collected soreness ratings
  - one check-in row with hunger 7 (rejected on import)
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "mid_cut_client"
AS_OF = date(2026, 9, 28)
HISTORY_START = date(2026, 6, 8)
CUT_START = date(2026, 8, 17)
VACATION = (date(2026, 7, 9), date(2026, 7, 19))
TYPO_DAY = date(2026, 6, 24)
SPARSE_WEIGH_WEEK = date(2026, 8, 3)
SPARSE_INTAKE_WEEK = date(2026, 8, 24)

# (start, accumulation weeks); each block is followed by a 1-week deload
MESOS = [(date(2026, 5, 18), 5), (date(2026, 6, 29), 4), (date(2026, 8, 3), 5),
         (date(2026, 9, 14), 5)]
# weekday -> (session name, [(muscle, exercise, base load, base reps)])
SESSIONS = {
    0: ("upper_a", [("chest", "bench_press", 205, 8), ("back", "chest_supported_row", 160, 10),
                    ("side_delts", "cable_lateral_raise", 20, 14), ("biceps", "incline_db_curl", 30, 11)]),
    1: ("lower_a", [("quads", "hack_squat", 270, 9), ("hamstrings", "seated_leg_curl", 120, 11)]),
    3: ("upper_b", [("chest", "incline_db_press", 70, 9), ("back", "lat_pulldown", 150, 10),
                    ("side_delts", "db_lateral_raise", 25, 13), ("biceps", "ez_bar_curl", 70, 10)]),
    4: ("lower_b", [("quads", "leg_extension", 150, 12), ("hamstrings", "romanian_deadlift", 225, 9)]),
}
RIR_BY_WEEK_5 = [4, 3, 2, 1, 0]
RIR_BY_WEEK_4 = [4, 3, 1, 0]


def on_vacation(d: date) -> bool:
    return VACATION[0] <= d <= VACATION[1]


def meso_position(d: date) -> tuple[int, int, bool] | None:
    """(meso index, 1-based week, is_deload)."""
    for i, (start, weeks) in enumerate(MESOS):
        wk = (d - start).days // 7 + 1
        if 1 <= wk <= weeks:
            return i, wk, False
        if wk == weeks + 1:
            return i, wk, True
    return None


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def generate() -> dict[str, list[list[str]]]:
    rng = random.Random(20261003)
    last_day = AS_OF - timedelta(days=1)
    files: dict[str, list[list[str]]] = {
        "weigh_ins.csv": [["Date", "Body Weight", "Conditions"]],
        "intake.csv": [["Date", "Calories", "Protein", "Carbs", "Fat"]],
        "sets.csv": [["Date", "Session", "Exercise", "Muscle Group", "Weight", "Reps", "RIR", "Set #"]],
        "stimulus.csv": [["Date", "Session", "Muscle Group", "Mind-Muscle", "Pump", "Disruption"]],
        "checkins.csv": [["Week Ending", "Adherence %", "Hunger", "Energy", "Sleep", "Notes"]],
        "cardio.csv": [["Date", "Activity", "Minutes", "Intensity", "Calories"]],
    }
    stimulus_w1 = {"chest": (2, 2, 1), "back": (2, 2, 1), "side_delts": (1, 1, 1),
                   "biceps": (2, 2, 1), "quads": (2, 2, 2), "hamstrings": (1, 2, 2)}

    for d in days(HISTORY_START, last_day):
        if on_vacation(d):
            continue
        wd = d.weekday()
        cutting = d >= CUT_START
        # weigh-ins Mon/Wed/Fri
        if wd in (0, 2, 4) and not (SPARSE_WEIGH_WEEK <= d < SPARSE_WEIGH_WEEK + timedelta(days=7) and wd != 0):
            weeks_cut = max(0, (d - CUT_START).days) / 7
            w = 192.0 * (1 - 0.0035) ** weeks_cut + rng.uniform(-0.6, 0.6)
            shown = "129.4" if d == TYPO_DAY else f"{w:.1f}"
            files["weigh_ins.csv"].append([d.isoformat(), shown, "fasted, post-bathroom"])
        # intake: Sundays unlogged; sparse week logs Mon-Wed only
        sparse = SPARSE_INTAKE_WEEK <= d < SPARSE_INTAKE_WEEK + timedelta(days=7)
        if wd != 6 and not (sparse and wd > 2):
            protein = 190 + rng.randint(-8, 8)
            fat = (62 if cutting else 75) + rng.randint(-6, 6)
            kcal = (2300 if cutting else 2700) + rng.randint(-80, 80)
            carbs = round((kcal - protein * 4 - fat * 9) / 4)
            files["intake.csv"].append([d.isoformat(), str(kcal), str(protein), str(carbs), str(fat)])
        # training
        pos = meso_position(d)
        if wd in SESSIONS and pos:
            mi, wk, deload = pos
            name, lifts = SESSIONS[wd]
            sid = f"{d.isoformat()}-{name}"
            weeks = MESOS[mi][1]
            rir = (RIR_BY_WEEK_5 if weeks == 5 else RIR_BY_WEEK_4)[min(wk, weeks) - 1]
            for muscle, ex, load, reps in lifts:
                n_sets = 2 if deload else 3 + (wk - 1) // 2
                for s in range(n_sets):
                    r = reps + (wk - 1) + (1 if rng.random() < 0.3 else 0) - s // 2
                    ld = load * (0.5 if deload and wd in (3, 4) else 1.0)
                    files["sets.csv"].append([d.isoformat(), sid, ex, muscle, f"{ld:g}",
                                              str(max(r, 1)), str(5 if deload else rir), str(s)])
                if mi == len(MESOS) - 1 and wk == 1:
                    mm, pump, dis = stimulus_w1[muscle]
                    files["stimulus.csv"].append([d.isoformat(), sid, muscle, str(mm), str(pump), str(dis)])
        # cardio Wed/Sat during the cut
        if cutting and wd in (2, 5):
            files["cardio.csv"].append([d.isoformat(), "incline walk", "30", "low", ""])
        # weekly check-in on Sundays
        if wd == 6:
            weeks_cut = max(0, (d - CUT_START).days // 7)
            hunger = min(5, 2 + weeks_cut // 2)
            hunger_cell = "7" if d == date(2026, 6, 28) else str(hunger)
            files["checkins.csv"].append([d.isoformat(), str(88 + rng.randint(0, 6)), hunger_cell,
                                          str(4 - weeks_cut // 4), "4", ""])
    return files


def write(out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in generate().items():
        with (out / name).open("w", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerows(rows)


if __name__ == "__main__":
    write()
