"""Dashboard journal loader: incremental tail parsing and per-view analytics cache."""
from __future__ import annotations

import importlib
import json
import os
import time

import pytest


@pytest.fixture(scope="module")
def server():
    """dashboard.server loads .env on import (35 keys such as CRYPTO_WINDOWS_MIN=5); importing it at
    collection time leaked those into every other test module. Import lazily and drop what it added."""
    before = set(os.environ)
    mod = importlib.import_module("dashboard.server")
    for key in set(os.environ) - before:
        os.environ.pop(key, None)
    return mod


def _write(path, rows, mode="a"):
    with path.open(mode) as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_journal_load_is_incremental_and_tolerates_torn_tail(server, tmp_path):
    p = tmp_path / "journal.jsonl"
    _write(p, [{"event": "cycle", "n": i} for i in range(3)])
    j = server.Journal(p)
    assert [e["n"] for e in j.load()] == [0, 1, 2]
    first_offset = j.offset
    # append two complete lines and one torn line (writer mid-append)
    _write(p, [{"event": "cycle", "n": 3}, {"event": "cycle", "n": 4}])
    with p.open("a") as fh:
        fh.write('{"event": "cycle", "n": 5')
    ev = j.load()
    assert [e["n"] for e in ev] == [0, 1, 2, 3, 4]
    assert j.offset > first_offset and j.size != p.stat().st_size  # torn tail not consumed
    # the writer finishes the line -> only the tail is parsed, nothing duplicated
    with p.open("a") as fh:
        fh.write("}\n")
    ev = j.load()
    assert [e["n"] for e in ev] == [0, 1, 2, 3, 4, 5]
    assert j.size == p.stat().st_size
    # unchanged file -> same list object, no re-read
    assert j.load() is ev
    # rotation / truncation -> full re-parse
    time.sleep(0.01)
    _write(p, [{"event": "cycle", "n": 9}], mode="w")
    assert [e["n"] for e in j.load()] == [9]


def test_build_state_caches_per_view_and_never_blocks_on_a_busy_lock(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_compute_state", lambda now: calls.append(server.is_live()) or {"mode": "live" if server.is_live() else "paper", "t": now})
    monkeypatch.setattr(server, "STATE_TTL_S", 60.0)
    server._STATE_CACHE.clear()
    server.set_view("paper")
    a = server.build_state()
    assert a["mode"] == "paper" and server.build_state() is a  # cached
    server.set_view("live")
    b = server.build_state()
    assert b["mode"] == "live" and calls == [False, True]
    server.set_view("paper")
    assert server.build_state() is a and calls == [False, True]  # switching views did not evict
    # another thread holds the lock: a view with a snapshot gets the snapshot back at once
    monkeypatch.setattr(server, "STATE_TTL_S", 0.0)
    assert server._STATE_LOCK.acquire(timeout=1)
    try:
        t0 = time.time()
        assert server.build_state() is a
        assert time.time() - t0 < 2.0 and calls == [False, True]
    finally:
        server._STATE_LOCK.release()
    server._STATE_CACHE.clear()
