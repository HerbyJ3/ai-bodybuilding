"""CSV import with a column-mapping config (BUILD_SPEC M6).

Mapping JSON:
{
  "date_format": "%Y-%m-%d",
  "streams": {
    "<event_type>": {
      "file": "weigh_ins.csv",
      "date_column": "Date",
      "fields": {
        "<payload_field>": {"column": "CSV header", "default": <value>, "split": ";"}
                         | {"value": <constant>}
      },
      "derive_sessions": true   # set_logged only: emit session_completed per session
    }
  }
}
A payload field named `date` is filled from `date_column` when not mapped.
Empty cells fall back to `default`, or are omitted. Rows that fail payload
validation are rejected and reported, never silently dropped.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from schemas.events import PAYLOAD_MODELS, Event, Source, make_event

IMPORTABLE = {"set_logged", "stimulus_rated", "soreness_rated", "joint_pain_reported",
              "weigh_in", "intake_logged", "weekly_checkin", "cardio_logged",
              "nutrition_targets_set"}
# nutrition_targets_set is imported one row per (date, day_type); rows with the same
# date become one event. Its mapping uses these row fields instead of payload fields.
TARGET_ROW_FIELDS = {"day_type", "protein_g", "carb_g", "fat_g", "note"}


class MappingError(ValueError):
    pass


@dataclass
class StreamReport:
    rows_read: int = 0
    events: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    first_date: date | None = None
    last_date: date | None = None


@dataclass
class ImportResult:
    events: list[Event]
    streams: dict[str, StreamReport]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self.events:
            out[e.type] += 1
        return dict(sorted(out.items()))

    def summary(self) -> dict[str, Any]:
        return {t: {"rows_read": r.rows_read, "events": r.events, "rejected": r.rejected,
                    "first_date": r.first_date and r.first_date.isoformat(),
                    "last_date": r.last_date and r.last_date.isoformat()}
                for t, r in self.streams.items()}


def load_mapping(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        mapping = json.load(f)
    for etype, spec in mapping.get("streams", {}).items():
        if etype not in IMPORTABLE:
            raise MappingError(f"stream '{etype}' is not importable")
        if "file" not in spec or "date_column" not in spec:
            raise MappingError(f"stream '{etype}' needs 'file' and 'date_column'")
        model_fields = (TARGET_ROW_FIELDS if etype == "nutrition_targets_set"
                        else set(PAYLOAD_MODELS[etype].model_fields))
        unknown = set(spec.get("fields", {})) - model_fields
        if unknown:
            raise MappingError(f"stream '{etype}': unknown payload fields {sorted(unknown)}")
    return mapping


def _cell(row: dict[str, str], fspec: dict[str, Any]) -> Any:
    if "value" in fspec:
        return fspec["value"]
    col = fspec["column"]
    if col not in row:
        raise MappingError(f"column '{col}' not in CSV")
    raw = (row[col] or "").strip()
    if raw == "":
        return fspec.get("default", None)
    if "split" in fspec:
        return [x.strip() for x in raw.split(fspec["split"]) if x.strip()]
    return raw


def _import_targets(client_id: str, path: Path, spec: dict[str, Any], fmt: str,
                    rep: StreamReport, recorded_at: datetime) -> list[Event]:
    by_date: dict[date, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            rep.rows_read += 1
            try:
                d = datetime.strptime(row[spec["date_column"]].strip(), fmt).date()
                vals = {name: _cell(row, fspec) for name, fspec in spec.get("fields", {}).items()}
                day_type = vals.get("day_type")
                grp = by_date.setdefault(d, {"macros": {}, "notes": [], "rows": []})
                grp["macros"][day_type] = {k: vals.get(k) for k in ("protein_g", "carb_g", "fat_g")}
                if vals.get("note"):
                    grp["notes"].append(str(vals["note"]))
                grp["rows"].append(i)
            except (ValueError, KeyError) as exc:
                rep.rejected.append({"row": i, "error": str(exc)})
    out = []
    for d, grp in sorted(by_date.items()):
        try:
            ev = make_event(client_id, "nutrition_targets_set",
                            datetime.combine(d, time(0), tzinfo=timezone.utc),
                            {"macros_by_day_type": grp["macros"], "note": "; ".join(grp["notes"])},
                            Source.import_, recorded_at)
        except ValidationError as exc:
            rep.rejected.append({"row": grp["rows"], "error": exc.errors()[0]["msg"]})
            continue
        out.append(ev)
        rep.events += 1
        rep.first_date = min(rep.first_date or d, d)
        rep.last_date = max(rep.last_date or d, d)
    return out


def import_csvs(client_id: str, csv_dir: str | Path, mapping: dict[str, Any],
                recorded_at: datetime | None = None) -> ImportResult:
    csv_dir = Path(csv_dir)
    fmt = mapping.get("date_format", "%Y-%m-%d")
    recorded_at = recorded_at or datetime.now(timezone.utc)
    events: list[Event] = []
    reports: dict[str, StreamReport] = {}
    for etype, spec in mapping["streams"].items():
        rep = reports[etype] = StreamReport()
        path = csv_dir / spec["file"]
        if not path.exists():
            rep.rejected.append({"row": None, "error": f"file not found: {spec['file']}"})
            continue
        if etype == "nutrition_targets_set":
            events += _import_targets(client_id, path, spec, fmt, rep, recorded_at)
            continue
        model_fields = PAYLOAD_MODELS[etype].model_fields
        sessions: dict[str, dict[str, Any]] = {}
        with path.open(newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f), start=2):  # header is line 1
                rep.rows_read += 1
                try:
                    d = datetime.strptime(row[spec["date_column"]].strip(), fmt).date()
                    payload = {}
                    for name, fspec in spec.get("fields", {}).items():
                        v = _cell(row, fspec)
                        if v is not None:
                            payload[name] = v
                    if "date" in model_fields and "date" not in payload:
                        payload["date"] = d
                    ts = datetime.combine(d, time(12, 0), tzinfo=timezone.utc)
                    ev = make_event(client_id, etype, ts, payload, Source.import_, recorded_at)
                except (ValidationError, ValueError, KeyError) as exc:
                    msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                    rep.rejected.append({"row": i, "error": msg})
                    continue
                events.append(ev)
                rep.events += 1
                rep.first_date = min(rep.first_date or d, d)
                rep.last_date = max(rep.last_date or d, d)
                if etype == "set_logged" and spec.get("derive_sessions"):
                    s = sessions.setdefault(ev.payload.session_id, {"date": d, "muscles": []})
                    if ev.payload.muscle not in s["muscles"]:
                        s["muscles"].append(ev.payload.muscle)
        for sid, s in sessions.items():
            ts = datetime.combine(s["date"], time(23, 0), tzinfo=timezone.utc)
            events.append(make_event(client_id, "session_completed", ts,
                                     {"session_id": sid, "date": s["date"],
                                      "muscles_trained": s["muscles"]},
                                     Source.import_, recorded_at))
    events.sort(key=lambda e: (e.timestamp, e.event_id))
    return ImportResult(events, reports)
