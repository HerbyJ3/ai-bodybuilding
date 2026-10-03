"""Parses the action strings in training-defaults.json into structured outcomes,
so the JSON text stays the single source of truth."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from config.loader import ConfigError

Kind = Literal["add_sets", "hold", "recovery", "reduce_sets", "defer"]
_ADD = re.compile(r"^add (\d+)-(\d+)(?: sets next week)?$")
_RANK = {"recovery": 0, "reduce_sets": 1, "hold": 2, "add_sets": 3}


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    add_min: int = 0
    add_max: int = 0

    def proposed_value(self) -> dict:
        if self.kind == "add_sets":
            return {"add_sets_min": self.add_min, "add_sets_max": self.add_max}
        if self.kind == "recovery":
            return {"session": "recovery", "volume": "MV"}
        if self.kind == "reduce_sets":
            return {"reduce_sets": "unspecified (coach sets amount)"}
        return {"add_sets": 0}


def parse(text: str) -> Outcome:
    t = text.strip().lower()
    if (m := _ADD.match(t)):
        return Outcome("add_sets", int(m.group(1)), int(m.group(2)))
    if t == "hold":
        return Outcome("hold")
    if t == "recovery":
        return Outcome("recovery")
    if t.startswith("progress normally"):
        return Outcome("defer")
    if t.startswith("reduce volume"):
        return Outcome("reduce_sets")
    raise ConfigError(f"unrecognised training action '{text}'")


def most_conservative(a: Outcome, b: Outcome) -> Outcome:
    if a.kind != b.kind:
        return a if _RANK[a.kind] < _RANK[b.kind] else b
    if a.kind == "add_sets":
        return a if a.add_max <= b.add_max else b
    return a
