import json

import pytest

from config.loader import ConfigError, load_config


def test_loads_both_knowledge_files(cfg):
    assert cfg.training("mesocycle.accumulation_weeks_min") == 4
    assert cfg.nutrition("tracking.kcal_per_lb_tissue") == 3500


def test_missing_key_raises(cfg):
    with pytest.raises(ConfigError):
        cfg.training("mesocycle.no_such_key")
    with pytest.raises(ConfigError):
        cfg.setting("no_such_setting")


def test_settings_entries_declare_source_and_provisional(cfg):
    for key, entry in cfg.settings_doc.items():
        if key.startswith("_"):
            continue
        assert "_source" in entry and "_provisional" in entry and "value" in entry, key


def test_every_provisional_setting_listed_in_open_items(cfg):
    from config.loader import ROOT
    open_items = (ROOT / "docs" / "OPEN_ITEMS.md").read_text()
    for key in cfg.provisional_entries():
        assert f"`{key}`" in open_items, f"{key} missing from OPEN_ITEMS.md"


def test_custom_settings_path(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"x": {"value": 1, "_provisional": True, "_source": "t"}}))
    cfg = load_config(settings_path=p)
    assert cfg.setting("x") == 1 and cfg.is_provisional("x")
