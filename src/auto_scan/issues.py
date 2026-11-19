"""Findings and their GitHub issues: the triage brief, linking, dismissing and verifying.

The findings engine (``engine.py``) opens findings and keeps their evidence up to
date. This module covers the rest of a finding's life (DESIGN.md, "Findings
lifecycle"):

- ``brief`` writes the markdown the triage reads: the open findings nobody has
  filed yet, with everything needed to investigate them.
- ``link`` records the issue the triage (or a person) filed: ``open`` -> ``filed``.
- ``dismiss`` closes a finding as not a problem: -> ``dismissed``.
- ``verify`` follows each filed issue with ``gh``. Closed as not planned (or as a
  duplicate) -> ``dismissed``. Closed as completed -> ``fix_pending``, and then,
  on runs whose manifest was written after the close: a failure reopens the
  issue (back to ``filed``, a regression), and enough passes verify it
  (``verified``, with a comment on the issue). A ``fix_pending`` finding whose
  issue was reopened by hand goes back to ``filed``.

``gh`` reads ``GH_TOKEN`` from the environment; it must be able to read, comment
on and reopen issues in every repository findings are filed in.
"""

import datetime
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any

from auto_scan.config import Config
from auto_scan.db import SIMULATED_AT

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
KIND_ORDER = {"science": 0, "perf": 1, "ops": 2}
# Passing events after the fix needed to verify it. One quiet event says little
# about a scientific misfit, which varies event to event; perf and ops problems
# tend to show on every run.
PASSES_TO_VERIFY = {"science": 2}
PASSES_TO_VERIFY_DEFAULT = 1
REALTIME_REPO = "ucgmsim/realtime_simulation_workflow"
ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([0-9]+)")
GITHUB_REPO = re.compile(
    r"(?:https?://|ssh://)?(?:[^@/]+@)?github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
LIVE = ("open", "filed", "fix_pending", "regressed")


def _now() -> str:
    """The current time, ISO 8601 UTC to the second."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing its output as text."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def _warn(message: str) -> None:
    """Print a one-line warning on stderr."""
    print(f"auto-scan: warning: {message}", file=sys.stderr)


def _parse_time(value: str | None) -> datetime.datetime | None:
    """An ISO 8601 time as an aware datetime (naive ones are taken as UTC), or None."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def github_repo(url: str | None, source: str | None = None) -> str | None:
    """The ``owner/repo`` a source's issues are filed in.

    Parameters
    ----------
    url : str or None
        The source's git URL (``run_sources.url``), e.g.
        ``https://github.com/ucgmsim/sw4.git`` or ``git@github.com:ucgmsim/sw4.git``.
    source : str, optional
        The source's name; ``realtime`` (the Cylc workflow itself) maps to
        ``ucgmsim/realtime_simulation_workflow`` whatever the URL.

    Returns
    -------
    str or None
        ``owner/repo``, or None if the URL is not a GitHub repository's.
    """
    if source == "realtime":
        return REALTIME_REPO
    if not url:
        return None
    match = GITHUB_REPO.match(url.strip())
    return f"{match[1]}/{match[2]}" if match else None


def _finding(db: sqlite3.Connection, finding_id: int) -> sqlite3.Row:
    """One finding's row.

    Raises
    ------
    ValueError
        If there is no such finding.
    """
    row = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise ValueError(f"no finding {finding_id}")
    return row


def _event(db: sqlite3.Connection, event_id: str | None) -> sqlite3.Row | None:
    """An event's row, or None."""
    if not event_id:
        return None
    return db.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()


def _describe_event(event: sqlite3.Row | None, event_id: str | None) -> str:
    """One line about an event: id, cycle, origin time, magnitude, location."""
    if event is None:
        return f"`{event_id}`" if event_id else "(none)"
    magnitude = f"Mw {event['magnitude']:.1f}" if event["magnitude"] is not None else "Mw ?"
    return (
        f"`{event['event_id']}` (cycle {event['cycle']}, {event['origin_time']}, {magnitude}, "
        f"{event['location'] or 'location unknown'}, dir `{event['event_dir']}`)"
    )


def _evaluations(db: sqlite3.Connection, check_id: str, subject: str) -> list[sqlite3.Row]:
    """Every evaluation of a (check, subject), earliest simulated event first, with when each run was written."""
    return db.execute(
        f"""
        SELECT v.event_id, v.resolution, v.failing, v.value, v.threshold, v.summary, v.evaluated_at,
               e.origin_time, e.cycle, {SIMULATED_AT} AS simulated_at,
               CASE WHEN v.resolution = ''
                    THEN (SELECT min(written) FROM runs r WHERE r.event_id = v.event_id)
                    ELSE (SELECT written FROM runs r WHERE r.event_id = v.event_id AND r.resolution = v.resolution)
               END AS written
        FROM evaluations v LEFT JOIN events e USING (event_id)
        WHERE v.check_id = ? AND v.subject = ?
        ORDER BY simulated_at IS NULL, simulated_at, v.event_id, v.resolution
        """,
        (check_id, subject),
    ).fetchall()


def _number(value: float | None) -> str:
    """A number for a table cell."""
    if value is None:
        return ""
    return f"{value:.4g}"


def _suspect_ranges(db: sqlite3.Connection, finding_id: int) -> list[dict[str, Any]] | None:
    """``engine.suspect_ranges``, or None if the engine can't give them."""
    try:
        from auto_scan import engine
    except ImportError as error:
        _warn(f"suspect ranges unavailable: {error}")
        return None
    try:
        return list(engine.suspect_ranges(db, finding_id))
    except Exception as error:  # noqa: BLE001 - the brief is still useful without the ranges
        _warn(f"suspect ranges for finding {finding_id} failed: {error}")
        return None


def _latest_sources(db: sqlite3.Connection, event_id: str | None) -> dict[str, sqlite3.Row]:
    """The sources of an event's runs, by name (the finest resolution's when they differ)."""
    if not event_id:
        return {}
    rows = db.execute(
        "SELECT * FROM run_sources WHERE event_id = ? ORDER BY resolution",
        (event_id,),
    ).fetchall()
    return {row["source"]: row for row in rows}


def _brief_one(db: sqlite3.Connection, config: Config, finding: sqlite3.Row) -> str:
    """The brief's section for one finding."""
    lines: list[str] = []
    title = finding["title"]
    lines.append(f"## Finding {finding['id']}: {title}")
    lines.append("")
    subject = finding["subject"] or "(whole event)"
    lines += [
        f"- check: `{finding['check_id']}`, subject: `{subject}`",
        f"- kind: {finding['kind']}, severity: {finding['severity']}",
        (
            f"- failing on {finding['n_failing']} evaluation(s); opened {finding['opened_at']}, "
            f"updated {finding['updated_at']}"
        ),
        f"- first failing event: {_describe_event(_event(db, finding['first_event']), finding['first_event'])}",
        f"- latest failing event: {_describe_event(_event(db, finding['last_event']), finding['last_event'])}",
        (
            f"- dashboard: {config.datasette_url}/-/dashboards/scan, "
            f"this finding: {config.datasette_url}/{config.scan_database}/findings/{finding['id']}"
        ),
        "",
        "### Summary",
        "",
        finding["summary"] or "(none)",
        "",
    ]

    evaluations = _evaluations(db, finding["check_id"], finding["subject"])
    lines += ["### Evaluation history", ""]
    if evaluations:
        lines += [
            "| event | origin time | resolution | value | threshold | result | run written |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in evaluations:
            result = "FAIL" if row["failing"] else "pass"
            lines.append(
                f"| {row['event_id']} | {row['origin_time'] or ''} | {row['resolution'] or '(event)'} | "
                f"{_number(row['value'])} | {_number(row['threshold'])} | {result} | {row['written'] or ''} |"
            )
    else:
        lines.append("(no evaluations recorded)")
    lines.append("")

    lines += ["### Evidence", "", "```json"]
    try:
        evidence = json.loads(finding["evidence"]) if finding["evidence"] else {}
        lines.append(json.dumps(evidence, indent=2, sort_keys=True))
    except ValueError:
        lines.append(str(finding["evidence"]))
    lines += ["```", ""]

    lines += ["### Suspect sources", ""]
    try:
        suspects = json.loads(finding["suspect_sources"]) if finding["suspect_sources"] else []
    except ValueError:
        suspects = []
    ranges = _suspect_ranges(db, finding["id"])
    by_source = {str(item["source"]): item for item in ranges or [] if item.get("source")}
    sources = _latest_sources(db, finding["last_event"])
    for name in list(dict.fromkeys([*suspects, *by_source])):
        item = by_source.get(name, {})
        url = item.get("url") or (sources[name]["url"] if name in sources else None)
        repo = github_repo(url, name)
        line = f"- `{name}`: repo {repo or 'unknown (not on GitHub?)'}"
        if item.get("compare_url"):
            line += (
                f"; passed at `{item.get('good_commit')}` (event {item.get('good_event')}), failed at "
                f"`{item.get('bad_commit')}` (event {item.get('bad_event')}); compare {item['compare_url']}"
            )
        elif item.get("bad_commit") or name in sources:
            bad = item.get("bad_commit") or sources[name]["commit_sha"]
            line += f"; no passing event before it: failing since the first event scanned, at `{bad}`"
        lines.append(line)
    if not suspects and not by_source:
        lines.append("(the check named none)")
    if ranges is None:
        lines.append("(commit ranges unavailable: engine.suspect_ranges failed)")
    lines.append("")

    lines += ["### Raw files", ""]
    event = _event(db, finding["last_event"])
    if event is not None:
        base = f"{config.results_url.rstrip('/')}/{event['event_dir']}"
        lines.append(f"- event: `{base}/` (realisation.json, realisation.toml, realisation.geojson)")
        resolutions = sorted({row["resolution"] for row in evaluations if row["event_id"] == event["event_id"]} - {""})
        if not resolutions:
            resolutions = [
                row["resolution"]
                for row in db.execute("SELECT resolution FROM runs WHERE event_id = ?", (event["event_id"],))
            ]
        for resolution in resolutions:
            lines.append(
                f"- {resolution}: `{base}/{resolution}/` (manifest.json, sw4_run.json, sw4_estimate.json, "
                "logs.tar.gz, simdb/)"
            )
    else:
        lines.append("(the latest failing event is not in the events table)")
    lines.append("")
    return "\n".join(lines)


def unfiled_open(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """The open findings with no issue yet, most urgent first.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.

    Returns
    -------
    list of sqlite3.Row
        Findings with status ``open`` and no ``issue_url``, by severity, then
        kind (science, perf, ops), then age (oldest first).
    """
    rows = db.execute(
        "SELECT * FROM findings WHERE status = 'open' AND coalesce(issue_url, '') = '' ORDER BY opened_at, id"
    ).fetchall()
    return sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), KIND_ORDER.get(r["kind"], 9)))


def brief(db: sqlite3.Connection, max_findings: int = 5, config: Config | None = None) -> str:
    """The triage brief: markdown about the open findings no issue covers yet.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    max_findings : int, optional
        The most findings included, by default 5; the rest wait for the next run.
    config : Config, optional
        The settings, for the results and dashboard URLs; by default from the
        environment.

    Returns
    -------
    str
        The brief, or ``""`` when nothing is open.
    """
    config = config or Config()
    findings = unfiled_open(db)
    if not findings:
        return ""
    chosen = findings[: max(max_findings, 0)]
    if not chosen:
        return ""
    head = [
        "# auto_scan triage brief",
        "",
        f"Generated {_now()}. {len(chosen)} of {len(findings)} open, unfiled finding(s), most urgent first.",
        f"Results store (rclone): `{config.results_url}`. Dashboard: {config.datasette_url}/-/dashboards/scan.",
        "",
    ]
    if len(findings) > len(chosen):
        head += [f"{len(findings) - len(chosen)} more wait for the next run; leave them alone.", ""]
    return "\n".join(head) + "\n" + "\n".join(_brief_one(db, config, finding) for finding in chosen)


def link(db: sqlite3.Connection, finding_id: int, url: str) -> None:
    """Record the GitHub issue filed for a finding: its status becomes ``filed``.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database (not committed here).
    finding_id : int
        The finding.
    url : str
        The issue's URL, ``https://github.com/<owner>/<repo>/issues/<n>``.

    Raises
    ------
    ValueError
        If the URL is not a GitHub issue's, the finding does not exist, or it is
        already ``verified`` or ``dismissed``.
    """
    url = url.strip()
    match = ISSUE_URL.fullmatch(url)
    if not match:
        raise ValueError(f"not a GitHub issue URL: {url!r} (want https://github.com/<owner>/<repo>/issues/<n>)")
    finding = _finding(db, finding_id)
    if finding["status"] not in LIVE:
        raise ValueError(f"finding {finding_id} is {finding['status']}; only a live finding can be linked")
    db.execute(
        "UPDATE findings SET issue_url = ?, issue_repo = ?, status = 'filed', issue_closed_at = NULL, updated_at = ?"
        " WHERE id = ?",
        (url, f"{match[1]}/{match[2]}", _now(), finding_id),
    )


def dismiss(db: sqlite3.Connection, finding_id: int, reason: str) -> None:
    """Close a finding as not a problem: its status becomes ``dismissed``.

    The engine does not reopen the same (check, subject) for 90 days.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database (not committed here).
    finding_id : int
        The finding.
    reason : str
        Why, appended to the finding's summary.

    Raises
    ------
    ValueError
        If the reason is empty or the finding does not exist.
    """
    reason = reason.strip()
    if not reason:
        raise ValueError("a dismissal needs a reason")
    finding = _finding(db, finding_id)
    summary = (finding["summary"] or "").rstrip()
    summary = f"{summary}\n\nDismissed {_now()}: {reason}".lstrip()
    db.execute(
        "UPDATE findings SET status = 'dismissed', summary = ?, updated_at = ? WHERE id = ?",
        (summary, _now(), finding_id),
    )


def _gh(runner: Runner, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``gh``; raise on failure.

    Raises
    ------
    RuntimeError
        If ``gh`` exits non-zero.
    """
    result = runner(["gh", *args])
    if result.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args[:2])} failed (exit {result.returncode}): {(result.stderr or '').strip()}"
        )
    return result


def _evidence(finding: sqlite3.Row) -> dict[str, Any]:
    """A finding's evidence as a dict."""
    try:
        evidence = json.loads(finding["evidence"]) if finding["evidence"] else {}
    except ValueError:
        evidence = {"raw": finding["evidence"]}
    return evidence if isinstance(evidence, dict) else {"evidence": evidence}


def _table(rows: list[sqlite3.Row]) -> str:
    """Evaluations as a markdown table."""
    lines = ["| event | resolution | value | threshold | result |", "|---|---|---|---|---|"]
    for row in rows:
        result = "FAIL" if row["failing"] else "pass"
        lines.append(
            f"| {row['event_id']} | {row['resolution'] or '(event)'} | {_number(row['value'])} | "
            f"{_number(row['threshold'])} | {result} |"
        )
    return "\n".join(lines)


def _after_close(finding: sqlite3.Row, db: sqlite3.Connection, closed: datetime.datetime) -> list[sqlite3.Row]:
    """The finding's evaluations on runs whose manifest was written after its issue closed."""
    rows = []
    for row in _evaluations(db, finding["check_id"], finding["subject"]):
        written = _parse_time(row["written"])
        if written is not None and written > closed:
            rows.append(row)
    return rows


def _regress(db: sqlite3.Connection, finding: sqlite3.Row, failing: list[sqlite3.Row], runner: Runner) -> None:
    """Reopen a fixed finding's issue with the new evidence; the finding goes back to ``filed``."""
    url = finding["issue_url"]
    events = sorted({row["event_id"] for row in failing})
    body = "\n".join(
        [
            (
                f"auto_scan: check `{finding['check_id']}` failed again after this issue was closed "
                f"({finding['issue_closed_at']}), on run(s) built after the close:"
            ),
            "",
            _table(failing),
            "",
            f"Latest summary: {failing[-1]['summary'] or finding['summary'] or ''}",
            "",
            f"Reopening. _auto_scan finding {finding['id']}, check: {finding['check_id']}._",
        ]
    )
    _gh(runner, ["issue", "reopen", url])
    _gh(runner, ["issue", "comment", url, "--body", body])
    evidence = _evidence(finding)
    evidence.setdefault("regressions", []).append(
        {
            "at": _now(),
            "issue_closed_at": finding["issue_closed_at"],
            "failing": [
                {
                    "event_id": r["event_id"],
                    "resolution": r["resolution"],
                    "value": r["value"],
                    "threshold": r["threshold"],
                }
                for r in failing
            ],
        }
    )
    summary = (
        f"Regressed after the fix (issue closed {finding['issue_closed_at']}): failing on {', '.join(events)}. "
        f"{failing[-1]['summary'] or ''}\n\n{finding['summary'] or ''}"
    ).strip()
    db.execute(
        "UPDATE findings SET status = 'filed', issue_closed_at = NULL, n_failing = coalesce(n_failing, 0) + ?,"
        " last_event = ?, summary = ?, evidence = ?, updated_at = ? WHERE id = ?",
        (len(failing), failing[-1]["event_id"], summary, json.dumps(evidence), _now(), finding["id"]),
    )


def _verify_fixed(db: sqlite3.Connection, finding: sqlite3.Row, passing: list[sqlite3.Row], runner: Runner) -> None:
    """Mark a finding verified and say so on its issue."""
    events = list(dict.fromkeys(row["event_id"] for row in passing))
    verified_event = events[-1]
    body = "\n".join(
        [
            (
                f"auto_scan: check `{finding['check_id']}` now passes on run(s) built after this issue was closed "
                f"({finding['issue_closed_at']}):"
            ),
            "",
            _table(passing),
            "",
            f"Verified on {', '.join(events)}. _auto_scan finding {finding['id']}, check: {finding['check_id']}._",
        ]
    )
    try:
        _gh(runner, ["issue", "comment", finding["issue_url"], "--body", body])
    except RuntimeError as error:
        # The comment is a courtesy: the fix is verified whether or not GitHub hears of it.
        _warn(f"finding {finding['id']}: {error}")
    db.execute(
        "UPDATE findings SET status = 'verified', verified_event = ?, verified_at = ?, updated_at = ? WHERE id = ?",
        (verified_event, _now(), _now(), finding["id"]),
    )


def _verify_one(db: sqlite3.Connection, finding: sqlite3.Row, runner: Runner) -> str:
    """Follow one filed finding's issue. Returns what happened, for the log."""
    result = _gh(runner, ["issue", "view", finding["issue_url"], "--json", "state,closedAt,stateReason"])
    issue = json.loads(result.stdout or "{}")
    state = str(issue.get("state", "")).upper()
    reason = str(issue.get("stateReason") or "").upper()
    if state == "OPEN":
        if finding["status"] == "fix_pending":
            # Reopened by hand (or a reopen whose database update was lost): follow it again.
            db.execute(
                "UPDATE findings SET status = 'filed', issue_closed_at = NULL, updated_at = ? WHERE id = ?",
                (_now(), finding["id"]),
            )
            return "issue reopened -> filed"
        return "issue open"
    if state != "CLOSED":
        raise RuntimeError(f"unexpected issue state {state!r}")
    if reason in ("NOT_PLANNED", "DUPLICATE"):
        why = "not planned" if reason == "NOT_PLANNED" else "a duplicate (link the original with `auto-scan link`)"
        summary = f"{(finding['summary'] or '').rstrip()}\n\nDismissed {_now()}: issue closed as {why}.".lstrip()
        db.execute(
            "UPDATE findings SET status = 'dismissed', summary = ?, issue_closed_at = ?, updated_at = ? WHERE id = ?",
            (summary, issue.get("closedAt"), _now(), finding["id"]),
        )
        return f"issue closed as {reason.lower()} -> dismissed"
    closed_at = issue.get("closedAt") or finding["issue_closed_at"]
    closed = _parse_time(closed_at)
    if closed is None:
        raise RuntimeError(f"closed issue without a usable closedAt: {closed_at!r}")
    if finding["status"] == "filed" or finding["issue_closed_at"] != closed_at:
        db.execute(
            "UPDATE findings SET status = 'fix_pending', issue_closed_at = ?, updated_at = ? WHERE id = ?",
            (closed_at, _now(), finding["id"]),
        )
        finding = _finding(db, finding["id"])
    after = _after_close(finding, db, closed)
    failing = [row for row in after if row["failing"]]
    if failing:
        _regress(db, finding, failing, runner)
        return f"failing on {len(failing)} run(s) after the fix -> issue reopened, filed"
    passing = [row for row in after if not row["failing"]]
    needed = PASSES_TO_VERIFY.get(finding["kind"], PASSES_TO_VERIFY_DEFAULT)
    if len({row["event_id"] for row in passing}) >= needed:
        _verify_fixed(db, finding, passing, runner)
        return "passing after the fix -> verified"
    return f"fix pending: {len({row['event_id'] for row in passing})} of {needed} passing event(s) so far"


def verify(db: sqlite3.Connection, runner: Runner | None = None) -> dict[int, str]:
    """Follow the issues of filed findings; verify fixes and reopen regressions.

    For each finding with status ``filed`` or ``fix_pending``, ``gh issue view``
    reads its issue:

    - open: nothing (a ``fix_pending`` finding whose issue was reopened goes back
      to ``filed``);
    - closed as not planned, or as a duplicate: ``dismissed``;
    - closed as completed: ``fix_pending`` (``issue_closed_at`` set). Then, among
      its evaluations on runs whose manifest was written after the close (for an
      event-wide evaluation, every run of the event), any failure reopens the
      issue with a comment and the new evidence, adds the failures to
      ``n_failing`` and returns the finding to ``filed``; otherwise passes on
      enough events (2 for ``science``, 1 otherwise) make it ``verified``, with a
      comment on the issue naming the events.

    A ``gh`` failure for one finding is a warning; the others still run. The
    database is committed after each finding, so an issue reopened on GitHub is
    never left recorded as fixed.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    runner : callable, optional
        Runs a command and returns its ``CompletedProcess`` (for tests); by
        default ``subprocess.run`` with captured text output.

    Returns
    -------
    dict of int to str
        What happened to each finding followed, by id.
    """
    runner = runner or _run
    outcomes: dict[int, str] = {}
    # A failed finding is rolled back alone, never the caller's earlier work (collect, check).
    db.commit()
    rows = db.execute(
        "SELECT * FROM findings WHERE status IN ('filed', 'fix_pending') AND coalesce(issue_url, '') != '' ORDER BY id"
    ).fetchall()
    for finding in rows:
        try:
            outcomes[finding["id"]] = _verify_one(db, finding, runner)
            db.commit()
        except (RuntimeError, ValueError) as error:
            db.rollback()
            outcomes[finding["id"]] = f"error: {error}"
            _warn(f"finding {finding['id']} ({finding['issue_url']}): {error}")
        print(f"auto-scan: finding {finding['id']}: {outcomes[finding['id']]}", file=sys.stderr)
    return outcomes


def summary(db: sqlite3.Connection, since: str | None = None) -> str:
    """A markdown summary of a scan run, for the GitHub Actions job summary.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    since : str, optional
        An ISO 8601 UTC time, the run's start: counts what changed after it.
        By default everything.

    Returns
    -------
    str
        Markdown: runs scanned, findings opened, verified, regressed and
        dismissed since then, and the findings now live by status.
    """
    since = since or ""
    scanned = db.execute(
        "SELECT count(*), count(DISTINCT event_id) FROM runs WHERE scanned_at >= ?", (since,)
    ).fetchone()
    opened = db.execute("SELECT count(*) FROM findings WHERE opened_at >= ?", (since,)).fetchone()[0]
    verified = db.execute("SELECT count(*) FROM findings WHERE verified_at >= ?", (since,)).fetchone()[0]
    dismissed = db.execute(
        "SELECT count(*) FROM findings WHERE status = 'dismissed' AND updated_at >= ?", (since,)
    ).fetchone()[0]
    regressed = 0
    for row in db.execute("SELECT evidence FROM findings WHERE evidence LIKE '%regressions%'"):
        try:
            regressions = json.loads(row["evidence"]).get("regressions", [])
        except (ValueError, AttributeError):
            continue
        regressed += any(str(item.get("at", "")) >= since for item in regressions)
    by_status = db.execute("SELECT status, count(*) AS n FROM findings GROUP BY status ORDER BY status").fetchall()
    lines = [
        "### auto_scan",
        "",
        f"- runs scanned: {scanned[0]} ({scanned[1]} event(s))",
        f"- findings opened: {opened}, verified: {verified}, regressed: {regressed}, dismissed: {dismissed}",
        f"- open and not yet filed: {len(unfiled_open(db))}",
        "",
        "| status | findings |",
        "|---|---|",
        *(f"| {row['status']} | {row['n']} |" for row in by_status),
        "",
    ]
    new = db.execute(
        "SELECT id, check_id, subject, severity, title FROM findings WHERE opened_at >= ? ORDER BY id", (since,)
    ).fetchall()
    if new:
        lines += ["New findings:", ""]
        lines += [f"- {r['id']}: [{r['severity']}] {r['title']} (`{r['check_id']}` {r['subject']})" for r in new]
        lines.append("")
    return "\n".join(lines)
