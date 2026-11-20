"""The open-ended review of each CMT: what a reviewer with no checklist notices.

The checks find what somebody already thought to test. To find the rest, each newly
ingested event is also read by a reviewer (Claude, ``prompts/cmt_explore.md``) who
is given the event's whole download, figures included, and a sheet of numbers
(``sheet``): this event's metrics and the recent events' for comparison. It is told
nothing of the checks, their thresholds or their findings. It writes down what
looks wrong as observations, each with a ``key`` naming the phenomenon so the same
thing seen on another event shares it.

The observations go in the ``observations`` table with status ``new``
(``import_observations``). The triage, which does know the checks and the issues,
then resolves each key (``resolve``):

- ``known``: a check, finding or issue already covers it (the resolution names it);
- ``filed``: it was new, and an issue was filed (the resolution is its URL), in
  ucgmsim/cmt_solutions for the problem itself or in ucgmsim/auto_scan for a check
  that would catch it from now on;
- ``dismissed``: not a problem (the resolution says why).

An event is explored once (meta ``explored:<event_id>``), and again after its CMT
is re-run.
"""

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from auto_scan.db import SIMULATED_AT

KEY = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
KINDS = {"science", "perf", "data"}
LEVELS = {"high", "medium", "low"}
RESOLVED = {"known", "filed", "dismissed"}
# Reviews of one event that may fail before it is left alone.
MAX_ATTEMPTS = 2
# The recent events shown beside the one reviewed.
SHEET_EVENTS = 20
# The selected run's numbers shown for each of them: (column, metric).
SHEET_COLUMNS = [
    ("stations", "cmt.n_stations"),
    ("VR", "cmt.vr"),
    ("Mw", "cmt.mw"),
    ("DC %", "cmt.dc_percent"),
    ("CLVD %", "cmt.clvd_percent"),
    ("ISO %", "cmt.iso_percent"),
    ("depth km", "cmt.centroid_depth_km"),
    ("red. chi2", "cmt.reduced_chi_square"),
    ("run total s", "cmt.timing.total_s"),
]


def _now() -> str:
    """The current time, UTC ISO 8601 to the second."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _number(value: Any) -> str:
    """A metric for a table cell."""
    if value is None:
        return ""
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def unexplored(db: sqlite3.Connection) -> list[str]:
    """The events with CMT runs that no reviewer has read yet, oldest first.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.

    Returns
    -------
    list of str
        Their event ids.
    """
    rows = db.execute(
        f"SELECT e.event_id, {SIMULATED_AT} AS at FROM events e"
        " WHERE EXISTS (SELECT 1 FROM runs r WHERE r.event_id = e.event_id)"
        " AND NOT EXISTS (SELECT 1 FROM meta m WHERE m.key = 'explored:' || e.event_id)"
        " ORDER BY at, e.event_id"
    ).fetchall()
    return [row["event_id"] for row in rows]


def pending(db: sqlite3.Connection) -> int:
    """How many observations await the triage.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.

    Returns
    -------
    int
        The observations with status ``new``.
    """
    return int(db.execute("SELECT count(*) FROM observations WHERE status = 'new'").fetchone()[0])


def sheet(db: sqlite3.Connection, event_id: str) -> str:
    """The numbers a reviewer gets with an event's download: its metrics, and recent events'.

    Deliberately without evaluations, thresholds or findings: the reviewer should
    not be told what the checks concluded.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event under review.

    Returns
    -------
    str
        Markdown.
    """
    event = db.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    lines = [f"# CMT review sheet: {event_id}", ""]
    if event is not None:
        lines += [
            (
                f"Origin {event['origin_time']}, {event['location'] or 'location unknown'}. SeismicNow now shows"
                f" M {_number(event['magnitude'])} at {_number(event['latitude'])}, {_number(event['longitude'])},"
                f" depth {_number(event['depth_km'])} km (the posted CMT's values once applied)."
            ),
            "",
        ]
    runs = [
        r["resolution"]
        for r in db.execute("SELECT resolution FROM runs WHERE event_id = ? ORDER BY resolution", (event_id,))
    ]
    metrics = db.execute(
        "SELECT resolution, metric, value, unit, detail FROM metrics WHERE event_id = ? ORDER BY metric, resolution",
        (event_id,),
    ).fetchall()
    by_metric: dict[str, dict[str, sqlite3.Row]] = {}
    for row in metrics:
        by_metric.setdefault(row["metric"], {})[row["resolution"]] = row
    lines += [
        "## This event's metrics",
        "",
        "Extracted from the download by auto_scan (`(event)`: of the whole event).",
        "",
    ]
    lines += [f"| metric | (event) | {' | '.join(runs)} | unit |", "|---|---|" + "---|" * (len(runs) + 1)]
    details = []
    for metric, values in by_metric.items():
        cells = [_number(values[r]["value"]) if r in values else "" for r in ["", *runs]]
        unit = next((v["unit"] for v in values.values() if v["unit"]), "")
        lines.append(f"| {metric} | {' | '.join(cells)} | {unit or ''} |")
        details += [
            f"- `{metric}` [{run or 'event'}]: {row['detail']}"
            for run, row in values.items()
            if row["detail"] and row["detail"] not in ("null", "[]", "{}")
        ]
    if details:
        lines += ["", "Details recorded with them:", "", *details]

    recent = db.execute(
        f"SELECT e.event_id, e.origin_time, e.location, {SIMULATED_AT} AS at FROM events e ORDER BY at DESC, e.event_id DESC LIMIT ?",
        (SHEET_EVENTS,),
    ).fetchall()
    lines += ["", f"## The last {len(recent)} events' posted solutions", ""]
    head = ["event", "origin", "computed", "GeoNet M", "Mw - GeoNet", "min to solution", "selected run"]
    head += [name for name, _ in SHEET_COLUMNS]
    lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for row in reversed(recent):
        own = {
            m["metric"]: m["value"]
            for m in db.execute(
                "SELECT metric, value FROM metrics WHERE event_id = ? AND resolution = ''", (row["event_id"],)
            )
        }
        selected = db.execute(
            "SELECT resolution FROM metrics WHERE event_id = ? AND metric = 'cmt.selected' AND value = 1",
            (row["event_id"],),
        ).fetchone()
        run = selected["resolution"] if selected else None
        of_run = (
            {
                m["metric"]: m["value"]
                for m in db.execute(
                    "SELECT metric, value FROM metrics WHERE event_id = ? AND resolution = ?", (row["event_id"], run)
                )
            }
            if run
            else {}
        )
        to_solution = own.get("cmt.time_to_solution_s")
        cells = [
            row["event_id"] + (" (this one)" if row["event_id"] == event_id else ""),
            str(row["origin_time"] or ""),
            str(row["at"] or ""),
            _number(own.get("cmt.geonet_magnitude")),
            _number(own.get("cmt.mw_minus_geonet")),
            _number(None if to_solution is None else to_solution / 60),
            run or "(none)",
            *[_number(of_run.get(metric)) for _, metric in SHEET_COLUMNS],
        ]
        lines.append("| " + " | ".join(cells) + " |")

    keys = db.execute(
        "SELECT key, min(title) AS title, count(DISTINCT event_id) AS n FROM observations GROUP BY key ORDER BY n DESC, key"
    ).fetchall()
    lines += ["", "## Names earlier reviews gave to what they saw", ""]
    if keys:
        lines += ["Use the same `key` when you see the same thing; make a new one otherwise.", ""]
        lines += [f"- `{row['key']}` ({row['n']} event(s)): {row['title']}" for row in keys]
    else:
        lines.append("(none yet)")
    return "\n".join(lines) + "\n"


def import_observations(db: sqlite3.Connection, event_id: str, path: Path) -> int:
    """Record a reviewer's observations of an event, and mark the event explored.

    The file is a JSON list of objects with ``key`` (lowercase words joined by
    hyphens), ``title``, ``body`` and, optionally, ``kind`` (science, perf or
    data), ``severity`` and ``confidence`` (high, medium or low). An entry that
    is malformed is skipped with nothing recorded for it. The event's earlier
    unresolved observations are replaced; a key already resolved for this event
    keeps its resolution and takes the new text.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database; not committed.
    event_id : str
        The event reviewed.
    path : Path
        The JSON file. When it is missing or not a JSON list, nothing is recorded
        and the event stays unexplored, to be reviewed again, until that has
        happened ``MAX_ATTEMPTS`` times.

    Returns
    -------
    int
        The observations recorded.
    """
    try:
        entries = json.loads(path.read_text()) if path.is_file() else None
    except ValueError:
        entries = None
    if not isinstance(entries, list):
        # The reviewer failed or wrote something else. Count it, and stop asking after
        # MAX_ATTEMPTS: an event no reviewer can get through must not be retried every run.
        row = db.execute("SELECT value FROM meta WHERE key = ?", (f"explore_attempts:{event_id}",)).fetchone()
        attempts = (int(row["value"]) if row else 0) + 1
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"explore_attempts:{event_id}", str(attempts)))
        if attempts >= MAX_ATTEMPTS:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"explored:{event_id}", f"gave up {_now()}"))
        return 0
    db.execute("DELETE FROM observations WHERE event_id = ? AND status = 'new'", (event_id,))
    now = _now()
    recorded = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key, title, body = entry.get("key"), entry.get("title"), entry.get("body")
        if not (isinstance(key, str) and KEY.fullmatch(key) and isinstance(title, str) and title.strip()):
            continue
        kind = entry.get("kind") if entry.get("kind") in KINDS else None
        severity = entry.get("severity") if entry.get("severity") in LEVELS else None
        confidence = entry.get("confidence") if entry.get("confidence") in LEVELS else None
        db.execute(
            "INSERT INTO observations (event_id, key, title, kind, severity, confidence, body, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)"
            " ON CONFLICT (event_id, key) DO UPDATE SET title = excluded.title, kind = excluded.kind,"
            " severity = excluded.severity, confidence = excluded.confidence, body = excluded.body",
            (event_id, key, title.strip(), kind, severity, confidence, body if isinstance(body, str) else None, now),
        )
        recorded += 1
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"explored:{event_id}", now))
    return recorded


def resolve(db: sqlite3.Connection, key: str, status: str, resolution: str) -> int:
    """Resolve every unresolved observation of a phenomenon.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database; not committed.
    key : str
        The phenomenon's key.
    status : {"known", "filed", "dismissed"}
        What became of it.
    resolution : str
        The check id or issue URL that covers it, or why it is no problem.

    Returns
    -------
    int
        The observations resolved.

    Raises
    ------
    ValueError
        If the status is not one of the three, the resolution is empty, or no
        unresolved observation has the key.
    """
    if status not in RESOLVED:
        raise ValueError(f"status must be one of {sorted(RESOLVED)}")
    if not resolution.strip():
        raise ValueError("a resolution needs a check, an issue URL or a reason")
    cursor = db.execute(
        "UPDATE observations SET status = ?, resolution = ?, resolved_at = ? WHERE key = ? AND status = 'new'",
        (status, resolution.strip(), _now(), key),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"no unresolved observation with key {key!r}")
    return cursor.rowcount


def brief_section(db: sqlite3.Connection) -> str:
    """The triage brief's section on the unresolved observations, by phenomenon.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.

    Returns
    -------
    str
        Markdown, or ``""`` when none is unresolved.
    """
    rows = db.execute("SELECT * FROM observations WHERE status = 'new' ORDER BY key, event_id").fetchall()
    if not rows:
        return ""
    by_key: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_key.setdefault(row["key"], []).append(row)
    order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(
        by_key.items(), key=lambda kv: (-len(kv[1]), min(order.get(r["severity"], 3) for r in kv[1]), kv[0])
    )
    lines = [
        "# Observations from the open-ended review",
        "",
        (
            f"{len(rows)} unresolved observation(s) of {len(by_key)} phenomenon(a), the most widely seen first."
            " The reviewer was not told what the checks test: sort each key into known, filed or dismissed."
        ),
        "",
    ]
    for key, group in ranked:
        lines += [f"## Observation `{key}`: {group[0]['title']}", ""]
        lines.append(f"- seen on {len(group)} event(s): {', '.join(f'`{r["event_id"]}`' for r in group)}")
        earlier = db.execute(
            "SELECT status, resolution, count(*) AS n FROM observations WHERE key = ? AND status != 'new'"
            " GROUP BY status, resolution",
            (key,),
        ).fetchall()
        for row in earlier:
            lines.append(f"- resolved before as **{row['status']}** on {row['n']} event(s): {row['resolution']}")
        lines.append("")
        for row in group:
            tags = ", ".join(f"{name} {row[name]}" for name in ("kind", "severity", "confidence") if row[name])
            lines += [
                f"### `{row['event_id']}`: {row['title']}" + (f" ({tags})" if tags else ""),
                "",
                row["body"] or "(no detail)",
                "",
            ]
    return "\n".join(lines)
