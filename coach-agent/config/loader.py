"""Loads knowledge/*.json and config/engine-settings.json.

Values are never hardcoded in the engine: rules call `cfg.training(...)`,
`cfg.nutrition(...)` or `cfg.setting(...)`, which raise ConfigError on a
missing key instead of falling back to an invented number.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
SETTINGS_PATH = Path(__file__).resolve().parent / "engine-settings.json"


class ConfigError(KeyError):
    pass


def _walk(doc: dict[str, Any], dotted: str, label: str) -> Any:
    node: Any = doc
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise ConfigError(f"{label}: missing key '{dotted}'")
    return node


@dataclass(frozen=True)
class Config:
    training_doc: dict[str, Any]
    nutrition_doc: dict[str, Any]
    settings_doc: dict[str, Any] = field(default_factory=dict)

    def training(self, dotted: str) -> Any:
        return _walk(self.training_doc, dotted, "training-defaults.json")

    def nutrition(self, dotted: str) -> Any:
        return _walk(self.nutrition_doc, dotted, "nutrition-defaults.json")

    def setting(self, dotted: str) -> Any:
        """`dotted` is '<entry>' or '<entry>.<path inside value>'."""
        head, _, rest = dotted.partition(".")
        entry = _walk(self.settings_doc, head, "engine-settings.json")
        if not isinstance(entry, dict) or "value" not in entry:
            raise ConfigError(f"engine-settings.json: '{head}' has no 'value'")
        if not rest:
            return entry["value"]
        return _walk(entry["value"], rest, f"engine-settings.json:{head}")

    def is_provisional(self, entry: str) -> bool:
        return bool(_walk(self.settings_doc, entry, "engine-settings.json").get("_provisional"))

    def provisional_entries(self) -> list[str]:
        return [k for k, v in self.settings_doc.items()
                if isinstance(v, dict) and v.get("_provisional")]


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_config(knowledge_dir: Path | None = None, settings_path: Path | None = None) -> Config:
    kd = knowledge_dir or KNOWLEDGE_DIR
    return Config(
        training_doc=_read(kd / "training-defaults.json"),
        nutrition_doc=_read(kd / "nutrition-defaults.json"),
        settings_doc=_read(settings_path or SETTINGS_PATH),
    )


@lru_cache(maxsize=1)
def default_config() -> Config:
    return load_config()
