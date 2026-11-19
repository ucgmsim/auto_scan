"""Tests of the database helpers that order events by when they were simulated."""

import sqlite3

from auto_scan.db import is_backlog, metric_history, set_metric, simulated_at


def add(db: sqlite3.Connection, event_id: str, origin_day: int, written: dict[str, int | None]) -> None:
    """An event of October with runs written on the given days of November (None: no time recorded)."""
    db.execute(
        "INSERT INTO events (event_id, event_dir, origin_time) VALUES (?, ?, ?)",
        (event_id, f"1-{event_id}", f"2026-10-{origin_day:02d}T00:00:00+00:00"),
    )
    for resolution, day in written.items():
        db.execute(
            "INSERT INTO runs (event_id, resolution, written) VALUES (?, ?, ?)",
            (event_id, resolution, None if day is None else f"2026-11-{day:02d}T00:00:00+00:00"),
        )
        set_metric(db, event_id, resolution, "perf.x", float(origin_day))
    set_metric(db, event_id, "", "ops.y", float(origin_day))


def test_history_is_in_simulation_order(db: sqlite3.Connection) -> None:
    add(db, "new", 20, {"0_5hz": 1, "1hz": 5})
    add(db, "backlog", 2, {"0_5hz": 3, "1hz": 4})  # an older earthquake, simulated in between
    add(db, "untimed", 10, {"0_5hz": None})  # no manifest time: its origin time stands in

    assert metric_history(db, "perf.x", "0_5hz") == [("untimed", 10.0), ("new", 20.0), ("backlog", 2.0)]
    # Per run: the backlog event's 1hz run was written before the new event's.
    assert metric_history(db, "perf.x", "1hz") == [("backlog", 2.0), ("new", 20.0)]
    assert metric_history(db, "perf.x", "0_5hz", before_event="backlog") == [("untimed", 10.0), ("new", 20.0)]
    assert metric_history(db, "perf.x", "1hz", before_event="backlog") == []
    assert metric_history(db, "perf.x", "0_5hz", before_event="untimed") == []
    # An event-wide metric goes by the event's earliest run.
    assert metric_history(db, "ops.y", "", before_event="backlog") == [("untimed", 10.0), ("new", 20.0)]


def test_simulated_at_and_backlog(db: sqlite3.Connection) -> None:
    add(db, "new", 20, {"0_5hz": 1, "1hz": 5})
    add(db, "backlog", 2, {"0_5hz": 3})
    add(db, "untimed", 10, {"0_5hz": None})
    assert simulated_at(db, "new") == "2026-11-01T00:00:00+00:00"
    assert simulated_at(db, "untimed") == "2026-10-10T00:00:00+00:00"
    assert simulated_at(db, "missing") is None
    assert is_backlog(db, "backlog")
    assert not is_backlog(db, "new") and not is_backlog(db, "untimed") and not is_backlog(db, "missing")
