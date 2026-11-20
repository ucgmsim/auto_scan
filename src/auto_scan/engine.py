"""Run the checks and keep the findings' lifecycle.

``run_checks`` evaluates every registered check on every event (or one), stores
each ``Evaluation`` in ``evaluations``, and then, for each ``(check_id,
subject)`` it touched, applies the parts of DESIGN.md's findings lifecycle that
need no GitHub access:

- **open**: no live finding exists, no dismissal in the last 90 days, and the
  check fails on the latest event evaluated, or on at least 2 of the last 5.
  Only events after the latest closed (verified or expired-dismissed) finding of
  the pair count, and a failure already followed by 3 passes opens nothing.
- **update**: an ``open``, ``filed`` or ``regressed`` finding takes the latest
  failure's ``last_event``, ``n_failing``, ``summary`` and ``evidence``.
- **self-close**: an ``open`` finding whose check passed on 3 consecutive later
  events becomes ``verified`` with no issue.

An event fails a pair when any of its resolutions does. ``fix_pending``,
``verified``-after-issue and ``regressed`` are set by ``issues.verify``.

Everything is computed from the stored history, and rows are written only when
they change, so running twice changes nothing the second time.
"""

import datetime as dt
import json
import math
import sqlite3
import sys
import traceback
from typing import Any

from auto_scan import registry
from auto_scan.db import SIMULATED_AT, run_resolutions
from auto_scan.registry import Evaluation

LIVE_STATUSES = ("open", "filed", "fix_pending", "regressed")
UPDATED_STATUSES = ("open", "filed", "regressed")
# DESIGN.md: a new finding needs a failure on the latest event or on 2 of the last 5.
OPEN_WINDOW = 5
OPEN_MIN_FAILURES = 2
# DESIGN.md: an open finding passing on 3 consecutive later events was transient.
SELF_CLOSE_PASSES = 3
# DESIGN.md: a dismissed (check_id, subject) is not reopened for 90 days.
DISMISS_DAYS = 90
# The Cylc workflow itself has no run_sources row; its repository is fixed.
REALTIME_URL = "https://github.com/ucgmsim/realtime_simulation_workflow"


def _now() -> str:
    """The current time, UTC ISO 8601 to the second."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _log(message: str) -> None:
    """One line on stderr."""
    print(f"auto-scan: {message}", file=sys.stderr, flush=True)


def _clean(value: float | None) -> float | None:
    """NaN as None, so it compares equal to the stored NULL."""
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else value


def live_finding(db: sqlite3.Connection, check_id: str, subject: str) -> sqlite3.Row | None:
    """The live finding of a ``(check_id, subject)``.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    check_id : str
        The check.
    subject : str
        The subject.

    Returns
    -------
    sqlite3.Row or None
        The latest finding whose status is ``open``, ``filed``, ``fix_pending`` or
        ``regressed``, or None.
    """
    return db.execute(
        f"SELECT * FROM findings WHERE check_id = ? AND subject = ? AND status IN ({', '.join('?' * len(LIVE_STATUSES))})"
        " ORDER BY id DESC LIMIT 1",
        (check_id, subject, *LIVE_STATUSES),
    ).fetchone()


def evaluations_for(db: sqlite3.Connection, check_id: str, subject: str) -> list[sqlite3.Row]:
    """Every stored evaluation of a ``(check_id, subject)``, by when its event was simulated.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    check_id : str
        The check.
    subject : str
        The subject.

    Returns
    -------
    list of sqlite3.Row
        The ``evaluations`` columns plus ``origin_time`` and ``simulated_at``
        (``db.SIMULATED_AT``), ordered by ``simulated_at``, then event id, then
        resolution. An event can have several rows, one per resolution. The
        order is of the runs, not of the earthquakes: a backlog event comes last.
    """
    return db.execute(
        f"SELECT v.*, e.origin_time, {SIMULATED_AT} AS simulated_at"
        " FROM evaluations v LEFT JOIN events e USING (event_id)"
        " WHERE v.check_id = ? AND v.subject = ?"
        " ORDER BY simulated_at IS NULL, simulated_at, v.event_id, v.resolution",
        (check_id, subject),
    ).fetchall()


def _event_history(db: sqlite3.Connection, check_id: str, subject: str) -> list[dict[str, Any]]:
    """Per event, in order: ``event_id``, ``simulated_at``, ``failing`` (any resolution) and its rows."""
    history: list[dict[str, Any]] = []
    for row in evaluations_for(db, check_id, subject):
        if not history or history[-1]["event_id"] != row["event_id"]:
            history.append(
                {"event_id": row["event_id"], "simulated_at": row["simulated_at"], "failing": False, "rows": []}
            )
        history[-1]["failing"] = history[-1]["failing"] or bool(row["failing"])
        history[-1]["rows"].append(row)
    return history


def _store(db: sqlite3.Connection, evaluation: Evaluation, now: str) -> None:
    """Insert or replace an evaluation, leaving an unchanged row (and its ``evaluated_at``) alone."""
    db.execute(
        "INSERT INTO evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (check_id, subject, event_id, resolution) DO UPDATE SET"
        " failing = excluded.failing, value = excluded.value, threshold = excluded.threshold,"
        " summary = excluded.summary, evaluated_at = excluded.evaluated_at"
        " WHERE failing IS NOT excluded.failing OR value IS NOT excluded.value"
        " OR threshold IS NOT excluded.threshold OR summary IS NOT excluded.summary",
        (
            evaluation.check_id,
            evaluation.subject,
            evaluation.event_id,
            evaluation.resolution,
            int(bool(evaluation.failing)),
            _clean(evaluation.value),
            _clean(evaluation.threshold),
            evaluation.summary,
            now,
        ),
    )


def _closed_boundary(db: sqlite3.Connection, check_id: str, subject: str) -> str | None:
    """The simulation time after which failures belong to a new finding: the latest closed one's end."""
    row = db.execute(
        f"SELECT max({SIMULATED_AT}) AS boundary FROM findings f"
        " JOIN events e ON e.event_id = coalesce(f.verified_event, f.last_event)"
        " WHERE f.check_id = ? AND f.subject = ? AND f.status IN ('verified', 'dismissed')",
        (check_id, subject),
    ).fetchone()
    return row["boundary"]


def _recently_dismissed(db: sqlite3.Connection, check_id: str, subject: str, now: str) -> bool:
    """Whether the pair was dismissed in the last 90 days (by the finding's ``updated_at``)."""
    cutoff = (dt.datetime.fromisoformat(now) - dt.timedelta(days=DISMISS_DAYS)).isoformat(timespec="seconds")
    row = db.execute(
        "SELECT 1 FROM findings WHERE check_id = ? AND subject = ? AND status = 'dismissed'"
        " AND coalesce(updated_at, opened_at) >= ? LIMIT 1",
        (check_id, subject, cutoff),
    ).fetchone()
    return row is not None


def _update(db: sqlite3.Connection, finding: sqlite3.Row, changes: dict[str, Any], now: str) -> None:
    """Write the columns of ``changes`` that differ, bumping ``updated_at`` only if any did."""
    changed = {key: value for key, value in changes.items() if finding[key] != value}
    if not changed:
        return
    changed["updated_at"] = now
    db.execute(
        f"UPDATE findings SET {', '.join(f'{key} = ?' for key in changed)} WHERE id = ?",
        (*changed.values(), finding["id"]),
    )


def _from_evaluation(evaluation: Evaluation) -> dict[str, Any]:
    """The finding columns an evaluation supplies."""
    return {
        "summary": evaluation.summary,
        "evidence": json.dumps(evaluation.evidence, sort_keys=True, default=str),
        "suspect_sources": json.dumps(list(evaluation.suspect_sources)),
    }


def _apply_lifecycle(
    db: sqlite3.Connection, check_id: str, subject: str, failures: dict[str, Evaluation], now: str
) -> None:
    """Open, update or self-close the finding of one pair from its stored history.

    ``failures`` maps event id to this run's failing evaluation there (the source
    of a finding's title, summary and evidence).
    """
    history = _event_history(db, check_id, subject)
    if not history:
        return
    finding = live_finding(db, check_id, subject)
    if finding is None:
        if _recently_dismissed(db, check_id, subject, now):
            return
        boundary = _closed_boundary(db, check_id, subject)
        recent = [h for h in history if boundary is None or (h["simulated_at"] or "") > boundary]
        window = recent[-OPEN_WINDOW:]
        if not window:
            return
        failing = [h for h in window if h["failing"]]
        if not (window[-1]["failing"] or len(failing) >= OPEN_MIN_FAILURES):
            return
        last_index = max(i for i, h in enumerate(recent) if h["failing"])
        if len(recent) - 1 - last_index >= SELF_CLOSE_PASSES:
            return
        source = next((failures[h["event_id"]] for h in reversed(failing) if h["event_id"] in failures), None)
        if source is None:
            # Not evaluated in this run: no title or evidence to open it with.
            return
        first = failing[0]["event_id"]
        first_index = next(i for i, h in enumerate(recent) if h["event_id"] == first)
        db.execute(
            "INSERT INTO findings (check_id, subject, kind, severity, status, title, summary, evidence,"
            " suspect_sources, first_event, last_event, n_failing, opened_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                check_id,
                subject,
                source.kind,
                source.severity,
                source.title,
                *_from_evaluation(source).values(),
                first,
                recent[last_index]["event_id"],
                sum(h["failing"] for h in recent[first_index:]),
                now,
                now,
            ),
        )
        _log(f"opened finding {check_id} [{subject}]: {source.title}")
        return
    if finding["status"] not in UPDATED_STATUSES:
        return
    ids = [h["event_id"] for h in history]
    start = ids.index(finding["first_event"]) if finding["first_event"] in ids else 0
    # The history's order can change under a finding: an event is simulated again and moves
    # to the end, or (once) the order itself went from origin time to simulation time. The
    # finding then spans from whichever of its two ends now comes first.
    if finding["last_event"] in ids:
        start = min(start, ids.index(finding["last_event"]))
    since = history[start:]
    failing = [h for h in since if h["failing"]]
    changes: dict[str, Any] = {}
    if failing:
        last = failing[-1]["event_id"]
        changes = {"first_event": failing[0]["event_id"], "last_event": last, "n_failing": len(failing)}
        if last in failures:
            changes |= _from_evaluation(failures[last])
            changes["severity"] = failures[last].severity
    if finding["status"] == "open":
        last_event = changes.get("last_event", finding["last_event"])
        after = ids.index(last_event) + 1 if last_event in ids else start
        passes = history[after:]
        if len(passes) >= SELF_CLOSE_PASSES and not any(h["failing"] for h in passes):
            changes |= {
                "status": "verified",
                "verified_event": passes[SELF_CLOSE_PASSES - 1]["event_id"],
                "verified_at": now,
            }
            _log(f"finding {finding['id']} {check_id} [{subject}] passed {SELF_CLOSE_PASSES} times: verified")
    if "verified_at" in changes and finding["verified_at"] is not None:
        changes.pop("verified_at")
    _update(db, finding, changes, now)


def run_checks(db: sqlite3.Connection, event: str | None = None, checks: list[registry.Check] | None = None) -> None:
    """Evaluate every check and update the findings.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database; committed at the end.
    event : str, optional
        Only this event id. The lifecycle still reads each pair's whole history.
    checks : list of Check, optional
        The checks to run, by default every simulation check (``registry.CHECKS``,
        after ``load_plugins``). The CMT review passes ``registry.CMT_CHECKS``.
    """
    if checks is None:
        registry.load_plugins()
        checks = registry.CHECKS
    now = _now()
    if event is None:
        events = [
            row["event_id"]
            for row in db.execute(
                f"SELECT event_id, {SIMULATED_AT} AS at FROM events e ORDER BY at IS NULL, at, event_id"
            )
        ]
    else:
        events = [row["event_id"] for row in db.execute("SELECT event_id FROM events WHERE event_id = ?", (event,))]
        if not events:
            _log(f"no event {event} in the database")
    # Per pair, this run's latest failing evaluation of each event.
    touched: dict[tuple[str, str], dict[str, Evaluation]] = {}
    for event_id in events:
        for check in checks:
            try:
                evaluations = list(check(db, event_id))
            except Exception:  # noqa: BLE001 - one check's bug must not stop the others
                _log(f"check {getattr(check, '__qualname__', check)} failed on {event_id}:\n{traceback.format_exc()}")
                continue
            for evaluation in evaluations:
                _store(db, evaluation, now)
                failures = touched.setdefault((evaluation.check_id, evaluation.subject), {})
                if evaluation.failing:
                    failures[evaluation.event_id] = evaluation
    for (check_id, subject), failures in touched.items():
        try:
            _apply_lifecycle(db, check_id, subject, failures, now)
        except Exception:  # noqa: BLE001
            _log(f"findings lifecycle failed for {check_id} [{subject}]:\n{traceback.format_exc()}")
    db.commit()


def _commit_at(db: sqlite3.Connection, event_id: str, source: str, resolution: str) -> tuple[str | None, str | None]:
    """The (commit, url) a source had on an event, at ``resolution`` or else the finest run."""
    candidates = ([resolution] if resolution else []) + list(reversed(run_resolutions(db, event_id)))
    for candidate in candidates:
        row = db.execute(
            "SELECT commit_sha, url FROM run_sources WHERE event_id = ? AND resolution = ? AND source = ?",
            (event_id, candidate, source),
        ).fetchone()
        if row is not None:
            return row["commit_sha"], row["url"]
    return None, None


def _compare_url(url: str | None, good: str | None, bad: str | None) -> str | None:
    """``<repo>/compare/<good>...<bad>`` for a GitHub repository whose commit changed."""
    if not url or not good or not bad or good == bad or not url.startswith("https://github.com/"):
        return None
    return f"{url.removesuffix('.git').rstrip('/')}/compare/{good}...{bad}"


def suspect_ranges(db: sqlite3.Connection, finding_id: int) -> list[dict[str, Any]]:
    """The commit range of each suspect source of a finding.

    The bad event is the finding's first failing event; the good event is the
    last event simulated before it where the check passed. For each suspect source the
    commit comes from ``run_sources`` at the resolution the evaluation ran on (or
    the event's finest run for an event-wide check).

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    finding_id : int
        The finding.

    Returns
    -------
    list of dict
        One per suspect source, with keys ``source``, ``url``, ``good_commit``,
        ``bad_commit``, ``good_event``, ``bad_event`` and ``compare_url``.
        ``good_event`` and ``good_commit`` are None when the check never passed
        before (failing since the first event scanned). ``compare_url`` is None
        then, when the commit did not change, or for a non-GitHub source. For
        ``realtime`` (no ``run_sources`` row) both commits are None. An unknown
        finding gives ``[]``.
    """
    finding = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if finding is None:
        return []
    sources = json.loads(finding["suspect_sources"] or "[]")
    history = _event_history(db, finding["check_id"], finding["subject"])
    ids = [h["event_id"] for h in history]
    if finding["first_event"] in ids:
        bad_index = ids.index(finding["first_event"])
    else:
        bad_index = next((i for i, h in enumerate(history) if h["failing"]), None)
    if bad_index is None:
        bad = None
        good = None
    else:
        good_index = next((i for i in range(bad_index - 1, -1, -1) if not history[i]["failing"]), None)
        good = None if good_index is None else history[good_index]
        if good_index is not None:
            bad_index = next(i for i in range(good_index + 1, len(history)) if history[i]["failing"])
        bad = history[bad_index]

    def resolution_of(entry: dict[str, Any] | None, failing: bool) -> str:
        """The resolution of the entry's evaluation that passed or failed.

        Parameters
        ----------
        entry : dict or None
            One event's evaluations.
        failing : bool
            Whether to look for the failing evaluation or the passing one.

        Returns
        -------
        str
            The resolution, or '' if the evaluation is event-wide or there is none.
        """
        if entry is None:
            return ""
        return next((row["resolution"] for row in entry["rows"] if bool(row["failing"]) == failing), "")

    ranges = []
    for source in sources:
        entry: dict[str, Any] = {
            "source": source,
            "url": None,
            "good_commit": None,
            "bad_commit": None,
            "good_event": None if good is None else good["event_id"],
            "bad_event": None if bad is None else bad["event_id"],
            "compare_url": None,
        }
        if source == "realtime":
            entry["url"] = REALTIME_URL
        else:
            if good is not None:
                entry["good_commit"], entry["url"] = _commit_at(
                    db, good["event_id"], source, resolution_of(good, False)
                )
            if bad is not None:
                commit, url = _commit_at(db, bad["event_id"], source, resolution_of(bad, True))
                entry["bad_commit"] = commit
                entry["url"] = url or entry["url"]
            entry["compare_url"] = _compare_url(entry["url"], entry["good_commit"], entry["bad_commit"])
        ranges.append(entry)
    return ranges
