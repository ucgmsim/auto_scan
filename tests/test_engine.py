"""Tests of the checks engine and the findings lifecycle, with synthetic checks."""

import json
import sqlite3
from collections.abc import Iterator

import pytest

from auto_scan import engine, registry
from auto_scan.registry import Evaluation

CHECK = "perf.synthetic"


def add_events(db: sqlite3.Connection, n: int) -> list[str]:
    """Events e01..eNN on consecutive days, each with a 1hz run built from commit cNN."""
    ids = []
    for i in range(1, n + 1):
        event_id = f"2026p{i:06d}"
        db.execute(
            "INSERT INTO events (event_id, cycle, event_dir, origin_time) VALUES (?, ?, ?, ?)",
            (event_id, i, f"{i}-{event_id}", f"2026-10-{i:02d}T00:00:00+00:00"),
        )
        for resolution in ("0_5hz", "1hz"):
            db.execute("INSERT INTO runs (event_id, resolution) VALUES (?, ?)", (event_id, resolution))
            db.execute(
                "INSERT INTO run_sources VALUES (?, ?, 'sw4', 'https://github.com/ucgmsim/sw4.git', 'main', ?)",
                (event_id, resolution, f"c{i:02d}"),
            )
        ids.append(event_id)
    db.commit()
    return ids


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bool]]:
    """A synthetic check failing on the events mapped to True; it yields nothing for unmapped ones."""
    failing: dict[str, bool] = {}

    def synthetic(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
        if event_id not in failing:
            return
        yield Evaluation(
            check_id=CHECK,
            event_id=event_id,
            failing=failing[event_id],
            title="Synthetic problem",
            summary=f"value on {event_id}",
            kind="perf",
            severity="high",
            resolution="1hz",
            subject="1hz",
            value=1.0 if failing[event_id] else 0.0,
            threshold=0.5,
            evidence={"event": event_id},
            suspect_sources=["sw4", "realtime"],
        )

    monkeypatch.setattr(registry, "CHECKS", [synthetic])
    monkeypatch.setattr(registry, "load_plugins", lambda: None)
    yield failing


def findings(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM findings ORDER BY id").fetchall()


def snapshot(db: sqlite3.Connection) -> dict:
    return {
        t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3")] for t in ("findings", "evaluations")
    }


def test_opens_on_latest_failure(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 4)
    outcomes.update({ids[0]: False, ids[1]: False, ids[2]: False, ids[3]: True})
    engine.run_checks(db)
    [finding] = findings(db)
    assert (finding["status"], finding["first_event"], finding["last_event"], finding["n_failing"]) == (
        "open",
        ids[3],
        ids[3],
        1,
    )
    assert finding["title"] == "Synthetic problem" and finding["kind"] == "perf" and finding["severity"] == "high"
    assert json.loads(finding["evidence"]) == {"event": ids[3]}
    assert json.loads(finding["suspect_sources"]) == ["sw4", "realtime"]
    assert db.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 4


def test_old_one_off_does_not_open(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 6)
    outcomes.update(dict.fromkeys(ids, False))
    outcomes[ids[1]] = True
    engine.run_checks(db)
    assert findings(db) == []


def test_opens_on_two_of_last_five(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 7)
    outcomes.update(dict.fromkeys(ids, False))
    outcomes[ids[0]] = True  # outside the window
    outcomes[ids[3]] = True
    outcomes[ids[4]] = True
    engine.run_checks(db)
    [finding] = findings(db)
    assert (finding["first_event"], finding["last_event"], finding["n_failing"]) == (ids[3], ids[4], 2)
    assert finding["status"] == "open"


def test_two_failures_followed_by_three_passes_open_nothing(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 5)
    outcomes.update({ids[0]: True, ids[1]: True, ids[2]: False, ids[3]: False, ids[4]: False})
    engine.run_checks(db)
    assert findings(db) == []


def test_further_failures_update_then_self_close(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 7)
    outcomes[ids[0]] = True
    engine.run_checks(db)
    outcomes[ids[1]] = False
    outcomes[ids[2]] = True
    engine.run_checks(db)
    [finding] = findings(db)
    assert (finding["status"], finding["last_event"], finding["n_failing"]) == ("open", ids[2], 2)
    assert finding["summary"] == f"value on {ids[2]}"

    outcomes.update({ids[3]: False, ids[4]: False})
    engine.run_checks(db)
    assert findings(db)[0]["status"] == "open"
    outcomes[ids[5]] = False
    engine.run_checks(db)
    [finding] = findings(db)
    assert finding["status"] == "verified" and finding["verified_event"] == ids[5]
    assert finding["issue_url"] is None

    # A new failure after a self-closed one opens a fresh finding, counting only from then.
    outcomes[ids[6]] = True
    engine.run_checks(db)
    new = findings(db)[1]
    assert (new["status"], new["first_event"], new["n_failing"]) == ("open", ids[6], 1)


def test_filed_finding_is_updated_but_not_self_closed(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 5)
    outcomes[ids[0]] = True
    engine.run_checks(db)
    db.execute("UPDATE findings SET status = 'filed', issue_url = 'https://github.com/x/y/issues/1'")
    outcomes.update({ids[1]: True, ids[2]: False, ids[3]: False, ids[4]: False})
    engine.run_checks(db)
    [finding] = findings(db)
    assert (finding["status"], finding["last_event"], finding["n_failing"]) == ("filed", ids[1], 2)


def test_fix_pending_is_left_to_verify(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 2)
    outcomes[ids[0]] = True
    engine.run_checks(db)
    db.execute("UPDATE findings SET status = 'fix_pending'")
    before = tuple(findings(db)[0])
    outcomes[ids[1]] = True
    engine.run_checks(db)
    assert tuple(findings(db)[0]) == before


def test_dismissal_holds_for_90_days(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 2)
    outcomes[ids[0]] = True
    engine.run_checks(db)
    db.execute("UPDATE findings SET status = 'dismissed', updated_at = ?", (engine._now(),))
    outcomes[ids[1]] = True
    engine.run_checks(db)
    assert [f["status"] for f in findings(db)] == ["dismissed"]

    db.execute("UPDATE findings SET updated_at = '2000-01-01T00:00:00+00:00'")
    engine.run_checks(db)
    statuses = [(f["status"], f["first_event"]) for f in findings(db)]
    # Only failures after the dismissed finding's last event count towards the new one.
    assert statuses == [("dismissed", ids[0]), ("open", ids[1])]


def test_idempotent(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 6)
    outcomes.update({ids[0]: False, ids[1]: True, ids[2]: True, ids[3]: False, ids[4]: True, ids[5]: True})
    engine.run_checks(db)
    first = snapshot(db)
    engine.run_checks(db)
    assert snapshot(db) == first
    engine.run_checks(db, event=ids[2])
    assert snapshot(db) == first


def test_single_event_run_uses_full_history(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 3)
    outcomes.update({ids[0]: True, ids[1]: False, ids[2]: True})
    engine.run_checks(db, event=ids[0])
    assert findings(db)[0]["first_event"] == ids[0]  # the only (so latest) event evaluated
    engine.run_checks(db, event=ids[2])
    [finding] = findings(db)
    assert (finding["last_event"], finding["n_failing"]) == (ids[2], 2)


def test_broken_check_does_not_stop_others(
    db: sqlite3.Connection, outcomes: dict[str, bool], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ids = add_events(db, 1)
    outcomes[ids[0]] = True

    def broken(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
        raise RuntimeError("check bug")
        yield  # pragma: no cover

    monkeypatch.setattr(registry, "CHECKS", [broken, *registry.CHECKS])
    engine.run_checks(db)
    assert len(findings(db)) == 1
    assert "check bug" in capsys.readouterr().err


def test_helpers_and_suspect_ranges(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 4)
    outcomes.update({ids[0]: False, ids[1]: False, ids[2]: True, ids[3]: True})
    engine.run_checks(db)
    finding = engine.live_finding(db, CHECK, "1hz")
    assert finding is not None and finding["first_event"] == ids[2]
    assert engine.live_finding(db, CHECK, "0_5hz") is None
    assert [r["event_id"] for r in engine.evaluations_for(db, CHECK, "1hz")] == ids

    ranges = {r["source"]: r for r in engine.suspect_ranges(db, finding["id"])}
    assert ranges["sw4"] == {
        "source": "sw4",
        "url": "https://github.com/ucgmsim/sw4.git",
        "good_commit": "c02",
        "bad_commit": "c03",
        "good_event": ids[1],
        "bad_event": ids[2],
        "compare_url": "https://github.com/ucgmsim/sw4/compare/c02...c03",
    }
    realtime = ranges["realtime"]
    assert (realtime["good_commit"], realtime["bad_commit"], realtime["compare_url"]) == (None, None, None)
    assert (realtime["good_event"], realtime["bad_event"]) == (ids[1], ids[2])
    assert engine.suspect_ranges(db, 999) == []


def test_suspect_ranges_without_a_pass(db: sqlite3.Connection, outcomes: dict[str, bool]) -> None:
    ids = add_events(db, 2)
    outcomes.update({ids[0]: True, ids[1]: True})
    engine.run_checks(db)
    [sw4, _] = engine.suspect_ranges(db, findings(db)[0]["id"])
    assert (sw4["good_event"], sw4["good_commit"], sw4["bad_event"], sw4["bad_commit"]) == (None, None, ids[0], "c01")
    assert sw4["compare_url"] is None
