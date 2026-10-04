from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "llm-settings.json"


@lru_cache(maxsize=1)
def llm_settings(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)
