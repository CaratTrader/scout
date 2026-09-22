"""Every test runs against throw-away copies of the bot's data files.

On 2026-09-12, 09-13 and 09-18 the suite overwrote the real paper ledger (data/ledger.json) with a
{"cash": 30.0, ...} fixture: agent/ledger code paths call save_ledger() with no path, which resolves
to the module-level LEDGER_PATH (or LIVE_LEDGER_PATH when the ledger says mode=live). This fixture
points every module-level data path at tmp_path for the duration of each test, so a forgotten
monkeypatch can no longer touch data/ or learning/.
"""
from __future__ import annotations

import importlib

import pytest

_REDIRECT = {
    "scout.config": ["LEDGER_PATH", "LIVE_LEDGER_PATH", "SCAN_PATH", "JOURNAL_PATH", "GROK_CACHE_PATH", "GROK_API_LOCK"],
    "scout.ledger": ["LEDGER_PATH", "LIVE_LEDGER_PATH"],
    "scout.agent": ["JOURNAL_PATH", "SCAN_PATH"],
    "scout.run": ["JOURNAL_PATH", "SCAN_PATH"],
    "scout.lessons": ["LESSONS_PATH", "POSTMORTEM_PATH", "POSTMORTEMS_PATH"],
    "scout.weather_lock": ["LEDGER", "JOURNAL"],
}


@pytest.fixture(autouse=True)
def _no_real_data_files(tmp_path, monkeypatch):
    for module_name, names in _REDIRECT.items():
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, tmp_path / f"{module_name}.{name}.tmp")
    yield
