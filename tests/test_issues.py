"""Tests of issues.py: the brief, link, dismiss and every branch of verify, with a fake gh."""

import json
import sqlite3
import subprocess
import sys
import types
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from auto_scan import issues
from auto_scan.config import Config
from auto_scan.db import connect

ISSUE = "https://github.com/ucgmsim/sw4/issues/42"


@pytest.fixture
def scan() -> Iterator[sqlite3.Connection]:
    """A scan database with three events, their runs and sources, and evaluations of one check."""
    db = connect(":memory:")
    events = [
        ("2026p100", 1, "2026-11-01T00:00:00Z", 5.1, "10 km north of Ōtautahi"),
        ("2026p200", 2, "2026-11-05T00:00:00Z", 4.6, "20 km east of Kaikōura"),
        ("2026p300", 3, "2026-11-10T00:00:00Z", 5.8, "near Arthur's Pass"),
        ("2026p400", 4, "2026-11-15T00:00:00Z", 4.9, "off Wellington"),
    ]
    for event_id, cycle, origin, magnitude, location in events:
        db.execute(
            "INSERT INTO events (event_id, cycle, event_dir, origin_time, magnitude, location) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, cycle, f"{cycle}-{event_id}", origin, magnitude, location),
        )
        written = origin.replace("T00", "T06")
        for resolution in ("0_25hz", "1hz"):
            db.execute(
                "INSERT INTO runs (event_id, resolution, status, written) VALUES (?, ?, 'succeeded', ?)",
                (event_id, resolution, written),
            )
            db.execute(
                "INSERT INTO run_sources VALUES (?, ?, 'sw4', 'https://github.com/ucgmsim/sw4.git', 'main', ?)",
                (event_id, resolution, f"sha{cycle}"),
            )
    db.commit()
    yield db
    db.close()


def add_finding(db: sqlite3.Connection, **fields: Any) -> int:
    row = {
        "check_id": "perf.sw4_slow",
        "subject": "1hz",
        "kind": "perf",
        "severity": "high",
        "status": "open",
        "title": "SW4 updates per core-second dropped",
        "summary": "1.2e6 against 2.0e6 before",
        "evidence": json.dumps({"metric": "perf.updates_per_core_s", "value": 1.2e6}),
        "suspect_sources": json.dumps(["sw4"]),
        "first_event": "2026p200",
        "last_event": "2026p200",
        "n_failing": 1,
        "opened_at": "2026-11-05T07:00:00+00:00",
        "updated_at": "2026-11-05T07:00:00+00:00",
    } | fields
    cursor = db.execute(
        f"INSERT INTO findings ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())
    )
    return int(cursor.lastrowid or 0)


def evaluate(db: sqlite3.Connection, event_id: str, failing: bool, check_id: str = "perf.sw4_slow", **kw: Any) -> None:
    db.execute(
        "INSERT OR REPLACE INTO evaluations (check_id, subject, event_id, resolution, failing, value, threshold, summary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            check_id,
            kw.get("subject", "1hz"),
            event_id,
            kw.get("resolution", "1hz"),
            int(failing),
            kw.get("value", 1.0 if failing else 2.0),
            1.5,
            "slow" if failing else "fine",
        ),
    )


def status(db: sqlite3.Connection, finding_id: int) -> sqlite3.Row:
    return db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()


class FakeGh:
    """gh with canned issue states; records every call."""

    def __init__(self, issues_: dict[str, dict[str, Any] | None], fail: set[str] | None = None) -> None:
        self.issues = issues_
        self.fail = fail or set()
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        assert args[0] == "gh" and args[1] == "issue"
        verb, url = args[2], args[3]
        if verb in self.fail:
            return subprocess.CompletedProcess(args, 1, "", f"{verb} failed")
        if verb == "view":
            issue = self.issues.get(url)
            if issue is None:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 404: Not Found")
            return subprocess.CompletedProcess(args, 0, json.dumps(issue), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def verbs(self, url: str = ISSUE) -> list[str]:
        return [call[2] for call in self.calls if call[3] == url]


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("AUTO_SCAN_DATASETTE_URL", "https://datasette.test")
    monkeypatch.setenv("AUTO_SCAN_RESULTS_URL", "remote:results")
    return Config()


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Stand in for engine.py: suspect_ranges returns one sw4 range."""
    asked: list[int] = []

    def suspect_ranges(db: sqlite3.Connection, finding_id: int) -> list[dict[str, Any]]:
        asked.append(finding_id)
        return [
            {
                "source": "sw4",
                "url": "https://github.com/ucgmsim/sw4.git",
                "good_commit": "sha1",
                "bad_commit": "sha2",
                "good_event": "2026p100",
                "bad_event": "2026p200",
                "compare_url": "https://github.com/ucgmsim/sw4/compare/sha1...sha2",
            }
        ]

    module = types.ModuleType("auto_scan.engine")
    setattr(module, "suspect_ranges", suspect_ranges)  # noqa: B010 - a stand-in module
    monkeypatch.setitem(sys.modules, "auto_scan.engine", module)
    import auto_scan

    monkeypatch.setattr(auto_scan, "engine", module, raising=False)
    return asked


# --- github_repo ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "source", "expected"),
    [
        ("https://github.com/ucgmsim/sw4.git", "sw4", "ucgmsim/sw4"),
        ("https://github.com/ucgmsim/qcore", "qcore", "ucgmsim/qcore"),
        ("git@github.com:ucgmsim/IM_calculation.git", "im", "ucgmsim/IM_calculation"),
        ("ssh://git@github.com/ucgmsim/workflow.git", "workflow", "ucgmsim/workflow"),
        (None, "realtime", "ucgmsim/realtime_simulation_workflow"),
        ("https://gitlab.com/x/y.git", "y", None),
        (None, "velocity-model", None),
    ],
)
def test_github_repo(url: str | None, source: str, expected: str | None) -> None:
    assert issues.github_repo(url, source) == expected


# --- brief ----------------------------------------------------------------------


def test_brief_empty_when_nothing_open(scan: sqlite3.Connection, config: Config) -> None:
    add_finding(scan, status="filed", issue_url=ISSUE)
    assert issues.brief(scan, config=config) == ""


def test_brief_content(scan: sqlite3.Connection, config: Config, fake_engine: list[int]) -> None:
    finding = add_finding(scan)
    evaluate(scan, "2026p100", False)
    evaluate(scan, "2026p200", True, value=1.2e6)
    text = issues.brief(scan, config=config)
    assert fake_engine == [finding]
    assert f"## Finding {finding}: SW4 updates per core-second dropped" in text
    assert "`perf.sw4_slow`" in text and "severity: high" in text
    assert "cycle 2" in text and "Mw 4.6" in text and "20 km east of Kaikōura" in text
    assert "| 2026p200 | 2026-11-05T00:00:00Z | 1hz | 1.2e+06 | 1.5 | FAIL |" in text
    assert "| 2026p100 |" in text and "| pass |" in text
    assert '"metric": "perf.updates_per_core_s"' in text
    assert "ucgmsim/sw4" in text and "https://github.com/ucgmsim/sw4/compare/sha1...sha2" in text
    assert "`remote:results/2-2026p200/1hz/`" in text
    assert "https://datasette.test/-/dashboards/scan" in text
    assert f"https://datasette.test/scan/findings/{finding}" in text


def test_brief_order_and_limit(scan: sqlite3.Connection, config: Config, fake_engine: list[int]) -> None:
    low = add_finding(scan, check_id="a.low", severity="low", title="Low one", opened_at="2026-01-01")
    ops = add_finding(scan, check_id="a.ops", kind="ops", title="Ops high", opened_at="2026-01-01")
    sci_new = add_finding(scan, check_id="a.sci2", kind="science", title="Science new", opened_at="2026-03-01")
    sci_old = add_finding(scan, check_id="a.sci1", kind="science", title="Science old", opened_at="2026-02-01")
    crit = add_finding(scan, check_id="a.crit", severity="critical", title="Critical", opened_at="2026-04-01")
    add_finding(scan, check_id="a.filed", status="filed", issue_url=ISSUE, title="Filed already")
    add_finding(scan, check_id="a.dismissed", status="dismissed", title="Dismissed already")
    text = issues.brief(scan, max_findings=4, config=config)
    order = [text.index(f"## Finding {i}:") for i in (crit, sci_old, sci_new, ops)]
    assert order == sorted(order)
    assert f"## Finding {low}:" not in text
    assert "Filed already" not in text and "Dismissed already" not in text
    assert "1 more wait" in text


def test_brief_survives_missing_engine(
    scan: sqlite3.Connection, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = types.ModuleType("auto_scan.engine")

    def broken(db: sqlite3.Connection, finding_id: int) -> list[dict[str, Any]]:
        raise RuntimeError("not yet")

    setattr(module, "suspect_ranges", broken)  # noqa: B010 - a stand-in module
    monkeypatch.setitem(sys.modules, "auto_scan.engine", module)
    import auto_scan

    monkeypatch.setattr(auto_scan, "engine", module, raising=False)
    add_finding(scan)
    text = issues.brief(scan, config=config)
    assert "commit ranges unavailable" in text
    assert "`sw4`: repo ucgmsim/sw4" in text


# --- link and dismiss -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/ucgmsim/sw4/pull/42",
        "https://gitlab.com/ucgmsim/sw4/issues/42",
        "github.com/ucgmsim/sw4/issues/42",
        "https://github.com/ucgmsim/sw4/issues/42#issuecomment-1",
        "https://github.com/ucgmsim/issues/42",
        "",
    ],
)
def test_link_rejects_non_issue_urls(scan: sqlite3.Connection, url: str) -> None:
    finding = add_finding(scan)
    with pytest.raises(ValueError):
        issues.link(scan, finding, url)
    assert status(scan, finding)["status"] == "open"


def test_link(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan)
    issues.link(scan, finding, ISSUE + "\n")
    row = status(scan, finding)
    assert (row["status"], row["issue_url"], row["issue_repo"]) == ("filed", ISSUE, "ucgmsim/sw4")


def test_link_unknown_or_closed_finding(scan: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="no finding"):
        issues.link(scan, 999, ISSUE)
    finding = add_finding(scan, status="verified")
    with pytest.raises(ValueError, match="verified"):
        issues.link(scan, finding, ISSUE)


def test_dismiss(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan)
    issues.dismiss(scan, finding, "ordinary misfit for a small event")
    row = status(scan, finding)
    assert row["status"] == "dismissed"
    assert row["summary"].startswith("1.2e6 against 2.0e6 before")
    assert "ordinary misfit for a small event" in row["summary"]
    assert row["updated_at"] != "2026-11-05T07:00:00+00:00"


def test_dismiss_validation(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan)
    with pytest.raises(ValueError, match="reason"):
        issues.dismiss(scan, finding, "  ")
    with pytest.raises(ValueError, match="no finding"):
        issues.dismiss(scan, 999, "why")


# --- verify ------------------------------------------------------------------------


def closed(at: str, reason: str = "COMPLETED") -> dict[str, Any]:
    return {"state": "CLOSED", "closedAt": at, "stateReason": reason}


def test_verify_open_issue_does_nothing(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE)
    gh = FakeGh({ISSUE: {"state": "OPEN", "closedAt": None, "stateReason": ""}})
    assert issues.verify(scan, runner=gh) == {finding: "issue open"}
    assert status(scan, finding)["status"] == "filed"
    assert gh.verbs() == ["view"]


def test_verify_reopened_by_hand(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="fix_pending", issue_url=ISSUE, issue_closed_at="2026-11-06T00:00:00Z")
    issues.verify(scan, runner=FakeGh({ISSUE: {"state": "OPEN", "closedAt": None, "stateReason": "REOPENED"}}))
    row = status(scan, finding)
    assert row["status"] == "filed" and row["issue_closed_at"] is None


@pytest.mark.parametrize("reason", ["NOT_PLANNED", "DUPLICATE"])
def test_verify_not_planned_dismisses(scan: sqlite3.Connection, reason: str) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE)
    issues.verify(scan, runner=FakeGh({ISSUE: closed("2026-11-06T00:00:00Z", reason)}))
    row = status(scan, finding)
    assert row["status"] == "dismissed"
    assert "Dismissed" in row["summary"]


def test_verify_closed_waits_for_later_runs(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE)
    evaluate(scan, "2026p200", True)
    # Closed after every run was written: nothing to judge the fix on yet.
    gh = FakeGh({ISSUE: closed("2026-11-20T00:00:00Z")})
    outcome = issues.verify(scan, runner=gh)
    row = status(scan, finding)
    assert row["status"] == "fix_pending"
    assert row["issue_closed_at"] == "2026-11-20T00:00:00Z"
    assert "0 of 1" in outcome[finding]
    assert gh.verbs() == ["view"]


def test_verify_passing_after_close_verifies(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE)
    evaluate(scan, "2026p200", True)
    evaluate(scan, "2026p300", False)  # written 2026-11-10T06:00, after the close
    gh = FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")})
    issues.verify(scan, runner=gh)
    row = status(scan, finding)
    assert row["status"] == "verified"
    assert row["verified_event"] == "2026p300" and row["verified_at"]
    assert gh.verbs() == ["view", "comment"]
    body = gh.calls[-1][gh.calls[-1].index("--body") + 1]
    assert "2026p300" in body and "| pass |" in body


def test_verify_science_needs_two_events(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE, kind="science", check_id="science.bias")
    evaluate(scan, "2026p300", False, check_id="science.bias")
    evaluate(scan, "2026p300", False, check_id="science.bias", resolution="0_25hz")
    gh = FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")})
    issues.verify(scan, runner=gh)
    assert status(scan, finding)["status"] == "fix_pending"  # two runs, but one event
    evaluate(scan, "2026p400", False, check_id="science.bias")
    issues.verify(scan, runner=gh)
    row = status(scan, finding)
    assert row["status"] == "verified" and row["verified_event"] == "2026p400"


def test_verify_failing_after_close_reopens(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="fix_pending", issue_url=ISSUE, issue_closed_at="2026-11-08T00:00:00Z")
    evaluate(scan, "2026p200", True)  # before the close: ignored
    evaluate(scan, "2026p300", False)
    evaluate(scan, "2026p400", True, value=0.9)
    gh = FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")})
    issues.verify(scan, runner=gh)
    row = status(scan, finding)
    assert row["status"] == "filed"
    assert row["issue_closed_at"] is None
    assert row["n_failing"] == 2
    assert row["last_event"] == "2026p400"
    assert row["summary"].startswith("Regressed after the fix")
    evidence = json.loads(row["evidence"])
    assert evidence["regressions"][0]["failing"][0]["event_id"] == "2026p400"
    assert evidence["metric"] == "perf.updates_per_core_s"
    assert gh.verbs() == ["view", "reopen", "comment"]
    assert "2026p400" in gh.calls[-1][gh.calls[-1].index("--body") + 1]
    assert "regressed: 1" in issues.summary(scan)


def test_verify_event_level_evaluation_needs_every_run_after_close(scan: sqlite3.Connection) -> None:
    scan.execute(
        "UPDATE runs SET written = '2026-11-07T00:00:00Z' WHERE event_id = '2026p300' AND resolution = '0_25hz'"
    )
    finding = add_finding(scan, status="filed", issue_url=ISSUE, subject="")
    evaluate(scan, "2026p300", False, subject="", resolution="")
    gh = FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")})
    issues.verify(scan, runner=gh)
    assert status(scan, finding)["status"] == "fix_pending"  # one of its runs predates the close


def test_verify_gh_failure_isolated(scan: sqlite3.Connection, capsys: pytest.CaptureFixture[str]) -> None:
    other = "https://github.com/ucgmsim/qcore/issues/7"
    missing = add_finding(scan, status="filed", issue_url=ISSUE)
    fine = add_finding(scan, status="filed", issue_url=other, check_id="perf.other")
    evaluate(scan, "2026p300", False, check_id="perf.other")
    gh = FakeGh({ISSUE: None, other: closed("2026-11-08T00:00:00Z")})
    outcome = issues.verify(scan, runner=gh)
    assert outcome[missing].startswith("error")
    assert status(scan, missing)["status"] == "filed"
    assert status(scan, fine)["status"] == "verified"
    assert "404" in capsys.readouterr().err


def test_verify_failed_reopen_keeps_fix_pending(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE)
    evaluate(scan, "2026p400", True)
    gh = FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")}, fail={"reopen"})
    outcome = issues.verify(scan, runner=gh)
    assert outcome[finding].startswith("error")
    # Rolled back to before this finding: retried next run from the same state.
    assert status(scan, finding)["status"] == "filed"
    gh = FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")})
    issues.verify(scan, runner=gh)
    assert status(scan, finding)["status"] == "filed"
    assert gh.verbs() == ["view", "reopen", "comment"]


def test_verify_comment_failure_still_verifies(scan: sqlite3.Connection) -> None:
    finding = add_finding(scan, status="filed", issue_url=ISSUE)
    evaluate(scan, "2026p400", False)
    issues.verify(scan, runner=FakeGh({ISSUE: closed("2026-11-08T00:00:00Z")}, fail={"comment"}))
    assert status(scan, finding)["status"] == "verified"


def test_verify_keeps_callers_pending_work(scan: sqlite3.Connection) -> None:
    add_finding(scan, status="filed", issue_url=ISSUE)
    scan.execute("INSERT INTO events (event_id, event_dir) VALUES ('pending', '9-pending')")
    issues.verify(scan, runner=FakeGh({ISSUE: None}))
    assert scan.execute("SELECT 1 FROM events WHERE event_id = 'pending'").fetchone()


def test_summary(scan: sqlite3.Connection) -> None:
    add_finding(scan, opened_at="2026-11-19T01:00:00+00:00", title="Brand new")
    add_finding(scan, check_id="x", opened_at="2026-11-01T01:00:00+00:00")
    scan.execute("UPDATE runs SET scanned_at = '2026-11-19T01:00:00+00:00' WHERE event_id = '2026p400'")
    text = issues.summary(scan, since="2026-11-19T00:00:00+00:00")
    assert "runs scanned: 2 (1 event(s))" in text
    assert "findings opened: 1" in text
    assert "open and not yet filed: 2" in text
    assert "Brand new" in text
