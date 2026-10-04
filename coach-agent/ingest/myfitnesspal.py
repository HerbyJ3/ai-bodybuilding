"""MyFitnessPal import (no public API exists; this reads MyFitnessPal's own reports).

Accepted inputs (recognised by column headers, not file names):
- Premium "Download Your Data" ZIP, or the CSVs inside it:
    nutrition (meal-level: Date, Meal, Calories, Fat, Carbohydrates, Protein, ...)
        -> one intake_logged per day (meals summed)
    progress / measurements (Date, Weight[, Body Fat ...])     -> weigh_in
    exercise (Date, Exercise, Exercise Minutes, Exercise Calories, Sets ...)
        -> cardio_logged for entries with minutes and no sets/reps (strength rows skipped)
- Printable diary report saved as HTML (free accounts): daily TOTAL rows -> intake_logged
- PDF: not supported (reported, nothing imported)

Everything imported has source=import. Re-importing is safe: identical rows are ignored and
a newer total for the same day replaces the older one in the client's state.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from schemas.events import Event, Source, make_event

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y", "%A, %B %d, %Y",
                "%b %d, %Y", "%a, %b %d, %Y")
DATE_IN_TEXT = re.compile(
    r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+)?"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}")


@dataclass
class MFPResult:
    events: list[Event] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)  # per file: name, kind, imported, notes

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for e in self.events:
            counts[e.type] += 1
        return {"files": self.files, "events": dict(counts)}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", h.lower()).strip()


def _find(headers: dict[str, str], *candidates: str, exclude: tuple[str, ...] = ()) -> str | None:
    """First header whose normalised name starts with a candidate (and contains no excluded word)."""
    for cand in candidates:
        for norm, orig in headers.items():
            if (norm == cand or norm.startswith(cand + " ")) and not any(x in norm for x in exclude):
                return orig
    return None


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def parse_date(v: str) -> date:
    v = v.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {v!r}")


def _at(d: date, hour: int) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=timezone.utc)


# --- CSV kinds ---------------------------------------------------------------------------

def _nutrition(rows: list[dict[str, str]], h: dict[str, str], cid: str, rec: datetime,
               info: dict[str, Any]) -> list[Event]:
    col = {"date": _find(h, "date"), "kcal": _find(h, "calories", exclude=("exercise", "burn")),
           "p": _find(h, "protein"), "c": _find(h, "carbohydrates", "carbs", "carbohydrate"),
           "f": _find(h, "fat", "total fat", exclude=("saturated", "trans", "poly", "mono"))}
    days: dict[date, dict[str, float]] = defaultdict(lambda: {"calories": 0.0, "protein_g": 0.0,
                                                              "carb_g": 0.0, "fat_g": 0.0})
    for i, r in enumerate(rows, start=2):
        try:
            d = parse_date(r[col["date"]])
        except (ValueError, KeyError):
            info["notes"].append(f"row {i}: bad date")
            continue
        for key, c in (("calories", "kcal"), ("protein_g", "p"), ("carb_g", "c"), ("fat_g", "f")):
            days[d][key] += _num(r.get(col[c]) if col[c] else None) or 0.0
    out = []
    for d, t in sorted(days.items()):
        payload = {"date": d, **{k: round(v, 1) for k, v in t.items()}}
        out.append(make_event(cid, "intake_logged", _at(d, 22), payload, Source.import_, rec))
    if not col["p"] or not col["c"] or not col["f"]:
        info["notes"].append("some macro columns missing; imported as 0 g")
    return out


def _progress(rows: list[dict[str, str]], h: dict[str, str], cid: str, rec: datetime,
              info: dict[str, Any], unit: str) -> list[Event]:
    dcol, wcol = _find(h, "date"), _find(h, "weight")
    norm_w = _norm(wcol or "")
    file_unit = "kg" if "kg" in norm_w else "lb" if ("lb" in norm_w or "lbs" in norm_w) else unit
    out = []
    for i, r in enumerate(rows, start=2):
        w = _num(r.get(wcol))
        if not w:
            continue  # rows that only carry other measurements
        try:
            out.append(make_event(cid, "weigh_in", _at(parse_date(r[dcol]), 7),
                                  {"weight": w, "unit": file_unit, "conditions": "MyFitnessPal"},
                                  Source.import_, rec))
        except (ValueError, KeyError, ValidationError) as exc:
            info["notes"].append(f"row {i}: {exc.__class__.__name__}")
    others = [o for n, o in h.items() if n not in ("date",) and not n.startswith("weight")]
    if others:
        info["notes"].append(f"not imported (no place for them yet): {', '.join(others)}")
    return out


def _exercise(rows: list[dict[str, str]], h: dict[str, str], cid: str, rec: datetime,
              info: dict[str, Any]) -> list[Event]:
    dcol, ncol = _find(h, "date"), _find(h, "exercise", exclude=("calories", "minutes"))
    mcol, kcol = _find(h, "exercise minutes", "minutes"), _find(h, "exercise calories", "calories")
    scol, rcol = _find(h, "sets"), _find(h, "reps per set", "reps")
    out, strength = [], 0
    for i, r in enumerate(rows, start=2):
        minutes = _num(r.get(mcol)) if mcol else None
        if (scol and _num(r.get(scol))) or (rcol and _num(r.get(rcol))) or not minutes:
            strength += 1
            continue
        try:
            d = parse_date(r[dcol])
            out.append(make_event(cid, "cardio_logged", _at(d, 20), {
                "date": d, "modality": (r.get(ncol) or "cardio").strip() or "cardio",
                "minutes": minutes, "intensity": "mod",
                "est_kcal": _num(r.get(kcol)) if kcol else None}, Source.import_, rec))
        except (ValueError, KeyError, ValidationError) as exc:
            info["notes"].append(f"row {i}: {exc.__class__.__name__}")
    if strength:
        info["notes"].append(f"{strength} strength/step rows skipped (no per-set detail in the export)")
    if out:
        info["notes"].append("cardio intensity isn't in the export; recorded as moderate")
    return out


def _csv(name: str, text: str, cid: str, rec: datetime, unit: str) -> tuple[list[Event], dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    headers = {_norm(k): k for k in (rows[0].keys() if rows else
                                     next(csv.reader(io.StringIO(text.lstrip("﻿"))), []))}
    info: dict[str, Any] = {"name": name, "kind": "unrecognised", "imported": 0, "notes": []}
    has = lambda *c: _find(headers, *c) is not None  # noqa: E731
    if not has("date"):
        info["notes"].append("no Date column")
        return [], info
    if has("exercise") and (has("exercise minutes", "minutes") or has("exercise calories")):
        info["kind"], events = "exercise", _exercise(rows, headers, cid, rec, info)
    elif has("calories") and (has("protein") or has("carbohydrates", "carbs") or has("meal")):
        info["kind"], events = "nutrition", _nutrition(rows, headers, cid, rec, info)
    elif has("weight"):
        info["kind"], events = "progress", _progress(rows, headers, cid, rec, info, unit)
    else:
        info["notes"].append("columns not recognised as a MyFitnessPal report")
        return [], info
    info["imported"] = len(events)
    return events, info


# --- printable diary (HTML) --------------------------------------------------------------

class _DiaryParser(HTMLParser):
    """Collects (date, header cells, TOTAL row cells) from MyFitnessPal's printable diary."""

    def __init__(self) -> None:
        super().__init__()
        self.current_date: date | None = None
        self.headers: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.totals: list[tuple[date, list[str], list[str]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            cells, self.row = self.row, None
            if not cells:
                return
            low = [c.lower() for c in cells]
            if "calories" in low and any(x in low for x in ("protein", "carbs", "fat")):
                self.headers = cells
            elif low[0].startswith("total") and self.current_date and self.headers:
                self.totals.append((self.current_date, self.headers, cells))

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif (m := DATE_IN_TEXT.search(data)):
            try:
                self.current_date = parse_date(m.group(0))
            except ValueError:
                pass


def _html(name: str, text: str, cid: str, rec: datetime) -> tuple[list[Event], dict]:
    info: dict[str, Any] = {"name": name, "kind": "printable diary", "imported": 0, "notes": []}
    p = _DiaryParser()
    p.feed(text)
    by_day: dict[date, dict[str, float]] = {}
    for d, headers, cells in p.totals:
        idx = {h.lower(): i for i, h in enumerate(headers)}
        def val(*keys: str) -> float:
            for k in keys:
                if k in idx and idx[k] < len(cells):
                    return _num(cells[idx[k]]) or 0.0
            return 0.0
        if d in by_day:  # exercise tables also end in TOTAL; keep the food (first) total
            continue
        by_day[d] = {"calories": val("calories"), "protein_g": val("protein"),
                     "carb_g": val("carbs", "carbohydrates"), "fat_g": val("fat")}
    events = [make_event(cid, "intake_logged", _at(d, 22), {"date": d, **t}, Source.import_, rec)
              for d, t in sorted(by_day.items())]
    if not events:
        info["kind"] = "unrecognised"
        info["notes"].append("no daily TOTAL rows found; is this the Printable Diary report?")
    info["imported"] = len(events)
    return events, info


# --- entry point ------------------------------------------------------------------------

def parse_files(client_id: str, files: dict[str, bytes], unit: str = "lb",
                recorded_at: datetime | None = None) -> MFPResult:
    rec = recorded_at or datetime.now(timezone.utc)
    res = MFPResult()

    def handle(name: str, data: bytes) -> None:
        low = name.lower()
        if low.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for inner in z.namelist():
                        if not inner.endswith("/") and not Path(inner).name.startswith("."):
                            handle(Path(inner).name, z.read(inner))
            except zipfile.BadZipFile:
                res.files.append({"name": name, "kind": "unrecognised", "imported": 0,
                                  "notes": ["not a valid ZIP file"]})
            return
        if low.endswith(".pdf"):
            res.files.append({"name": name, "kind": "pdf", "imported": 0, "notes": [
                "PDF reports aren't supported: open the Printable Diary in a browser and use "
                "'Save page as' (HTML), or use the Premium CSV export"]})
            return
        text = data.decode("utf-8-sig", errors="replace")
        if low.endswith((".html", ".htm")):
            events, info = _html(name, text, client_id, rec)
        elif low.endswith(".csv"):
            events, info = _csv(name, text, client_id, rec, unit)
        else:
            events, info = [], {"name": name, "kind": "unrecognised", "imported": 0,
                                "notes": ["unsupported file type"]}
        res.events += events
        res.files.append(info)

    for name, data in files.items():
        handle(Path(name).name, data)
    res.events.sort(key=lambda e: (e.timestamp, e.event_id))
    return res


def import_paths(client_id: str, paths: list[Path], unit: str = "lb") -> MFPResult:
    return parse_files(client_id, {p.name: p.read_bytes() for p in paths}, unit)
