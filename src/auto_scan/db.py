"""The scan's SQLite database: its schema and the few helpers every module shares.

``scan.db`` is the store of record. It is kept at ``Config.state_url`` between
runs and a copy is uploaded to the Datasette. The tables are the contract between
the collector, the extractors, the checks, the findings engine and the dashboard:
change one here and in DESIGN.md together.
"""

import json
import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# When an event was simulated: its earliest manifest (the origin time only when no
# run records one). This, not the origin time, orders events wherever the question
# is "which code ran": an old earthquake run through the workflow later (a backlog
# event) is old by origin but new by code. For a query whose events table is `e`.
SIMULATED_AT = "coalesce((SELECT min(r.written) FROM runs r WHERE r.event_id = e.event_id), e.origin_time)"
# The same for one run, falling back to the event's when there is no such run
# (an event-wide metric, resolution ''). Binds :resolution.
RUN_AT = f"coalesce((SELECT r.written FROM runs r WHERE r.event_id = e.event_id AND r.resolution = :resolution), {SIMULATED_AT})"

SCHEMA = """
-- One row a simulated event (a Cylc cycle).
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,          -- GeoNet public id, e.g. 2026p866048
    cycle INTEGER,                      -- the workflow's cycle point
    event_dir TEXT NOT NULL,            -- <cycle>-<event_id> under results_url
    origin_time TEXT,                   -- ISO 8601, UTC
    magnitude REAL,                     -- the CMT's Mw, as simulated
    latitude REAL, longitude REAL, depth_km REAL,
    location TEXT                       -- "15 km south-east of Arthur's Pass"
);

-- One row a run: an event at one resolution, as its manifest.json describes it.
CREATE TABLE IF NOT EXISTS runs (
    event_id TEXT NOT NULL REFERENCES events,
    resolution TEXT NOT NULL,           -- 0_25hz, 0_5hz or 1hz
    status TEXT,                        -- the manifest's: succeeded, failed:<task>, ...
    written TEXT,                       -- when the manifest was written, ISO 8601 UTC
    defaults_version TEXT,
    manifest_sha256 TEXT,               -- of manifest.json: a changed one is rescanned
    scanned_at TEXT,                    -- when the collector last ingested it
    PRIMARY KEY (event_id, resolution)
);

-- The commit of every source a run was built from (the manifest's `sources`).
CREATE TABLE IF NOT EXISTS run_sources (
    event_id TEXT NOT NULL,
    resolution TEXT NOT NULL,
    source TEXT NOT NULL,               -- the manifest's key: workflow, sw4, qcore-utils, ...
    url TEXT, ref TEXT,
    commit_sha TEXT,                    -- a commit, or a content hash for data sources
    PRIMARY KEY (event_id, resolution, source)
);

-- Scalars about a run, in long form: what every check reads.
-- `resolution` is '' for a metric of the event as a whole.
CREATE TABLE IF NOT EXISTS metrics (
    event_id TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL,               -- dotted name: science.residual.mean.PGA, perf.mpi_fraction
    value REAL,
    unit TEXT,
    detail TEXT,                        -- JSON, anything that explains the value
    PRIMARY KEY (event_id, resolution, metric)
);

-- Observed against simulated at a real station, one row an intensity measure.
CREATE TABLE IF NOT EXISTS station_residuals (
    event_id TEXT NOT NULL,
    resolution TEXT NOT NULL,
    station TEXT NOT NULL,
    im TEXT NOT NULL,                   -- PGA, PGV, pSA_0.5, ...
    observed REAL, simulated REAL,
    gmm REAL,                           -- an empirical model's median, when known
    residual REAL,                      -- ln(observed / simulated)
    rrup_km REAL, vs30 REAL,
    quality_ok INTEGER,                 -- 1 when the observed record passes the quality filter
    PRIMARY KEY (event_id, resolution, station, im)
);

-- SW4's timer regions (sw4_timers.csv), one row a region of a run.
CREATE TABLE IF NOT EXISTS sw4_timers (
    event_id TEXT NOT NULL,
    resolution TEXT NOT NULL,
    region TEXT NOT NULL,               -- solve/time_stepping/predictor/evalRHS, MPI_Barrier, ...
    depth INTEGER,
    calls_total INTEGER, ranks INTEGER,
    min_s REAL, avg_s REAL, max_s REAL, wall_avg_s REAL,
    mpi_min_s REAL, mpi_avg_s REAL, mpi_max_s REAL,
    omp_wait_frac REAL, omp_imb_avg REAL, omp_imb_max REAL,
    PRIMARY KEY (event_id, resolution, region)
);

-- The Cylc monitor's `jobs` view for the scanned workflow, copied whole each run.
CREATE TABLE IF NOT EXISTS jobs (
    workflow TEXT NOT NULL, cycle TEXT NOT NULL, name TEXT NOT NULL, submit_num INTEGER NOT NULL,
    try_num INTEGER, job_id TEXT, platform TEXT, runner TEXT, state TEXT,
    time_submit TEXT, time_run TEXT, time_run_exit TEXT, queue_s REAL, run_s REAL,
    cores INTEGER, nodes INTEGER, chunks INTEGER, mem_gb REAL, time_limit_min REAL,
    account TEXT, partition TEXT, core_hours REAL, rsu REAL, max_rss_gb REAL,
    mem_allocated_gb REAL, cpu_hours REAL, cpu_efficiency REAL, job_err TEXT,
    PRIMARY KEY (workflow, cycle, name, submit_num)
);

-- A problem a check found: one row from first sighting to verified fix.
-- (check_id, subject) names the problem; at most one finding per pair is not `verified`.
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    check_id TEXT NOT NULL,             -- e.g. perf.cost_model_overestimate
    subject TEXT NOT NULL DEFAULT '',   -- what it is about: a resolution, a station, a task
    kind TEXT NOT NULL,                 -- science, perf or ops
    severity TEXT NOT NULL,             -- critical, high, medium or low
    status TEXT NOT NULL,               -- open, filed, fix_pending, verified, regressed, dismissed
    title TEXT NOT NULL,
    summary TEXT,                       -- the latest failing evaluation's
    evidence TEXT,                      -- JSON, the latest failing evaluation's
    suspect_sources TEXT,               -- JSON list of run_sources.source names
    first_event TEXT, last_event TEXT,  -- the first and the latest event it failed on
    n_failing INTEGER DEFAULT 0,
    opened_at TEXT, updated_at TEXT,
    issue_url TEXT, issue_repo TEXT,    -- set by `auto-scan link`
    issue_closed_at TEXT,
    verified_event TEXT, verified_at TEXT
);
CREATE INDEX IF NOT EXISTS findings_key ON findings (check_id, subject, status);

-- Every evaluation of a check on a run, passing or failing: a finding's history,
-- and what the verification step reads.
CREATE TABLE IF NOT EXISTS evaluations (
    check_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '',
    failing INTEGER NOT NULL,
    value REAL, threshold REAL,
    summary TEXT,
    evaluated_at TEXT,
    PRIMARY KEY (check_id, subject, event_id, resolution)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open ``scan.db``, creating its tables if they are missing.

    Parameters
    ----------
    path : Path or str
        The database file, or ``":memory:"``.

    Returns
    -------
    sqlite3.Connection
        A connection whose rows are ``sqlite3.Row``.
    """
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    connection.commit()
    return connection


def set_metric(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    metric: str,
    value: float | None,
    unit: str | None = None,
    detail: Any = None,
) -> None:
    """Record one metric of a run, replacing an earlier value.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.
    resolution : str
        The run's resolution, or ``""`` for a metric of the whole event.
    metric : str
        The metric's dotted name.
    value : float or None
        Its value. NaN is stored as NULL.
    unit : str, optional
        Its unit.
    detail : Any, optional
        Anything JSON-serialisable that explains the value.
    """
    if value is not None and math.isnan(value):
        value = None
    db.execute(
        "INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, resolution, metric, value, unit, None if detail is None else json.dumps(detail)),
    )


def get_metric(db: sqlite3.Connection, event_id: str, resolution: str, metric: str) -> float | None:
    """One metric of a run.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.
    resolution : str
        The run's resolution, or ``""``.
    metric : str
        The metric's dotted name.

    Returns
    -------
    float or None
        The value, or None if it was never recorded or is NULL.
    """
    row = db.execute(
        "SELECT value FROM metrics WHERE event_id = ? AND resolution = ? AND metric = ?",
        (event_id, resolution, metric),
    ).fetchone()
    return None if row is None else row["value"]


def metric_history(
    db: sqlite3.Connection, metric: str, resolution: str, before_event: str | None = None
) -> list[tuple[str, float]]:
    """A metric's values on earlier runs, oldest first.

    "Earlier" is by when the run's manifest was written, not by the earthquake's
    origin time: the history of a backlog event is the runs made before it, on
    the code of the time.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    metric : str
        The metric's dotted name.
    resolution : str
        The resolution, or ``""``.
    before_event : str, optional
        Only runs written before this event's run at ``resolution``.

    Returns
    -------
    list of (str, float)
        ``(event_id, value)`` for every event with a non-NULL value, by when the
        run was written.
    """
    sql = (
        f"SELECT m.event_id, m.value, {RUN_AT} AS at FROM metrics m JOIN events e USING (event_id)"
        " WHERE m.metric = :metric AND m.resolution = :resolution AND m.value IS NOT NULL"
    )
    params: dict[str, Any] = {"metric": metric, "resolution": resolution}
    if before_event is not None:
        sql += f" AND at < (SELECT {RUN_AT} FROM events e WHERE e.event_id = :before)"
        params["before"] = before_event
    sql += " ORDER BY at, m.event_id"
    return [(row["event_id"], row["value"]) for row in db.execute(sql, params)]


def simulated_at(db: sqlite3.Connection, event_id: str) -> str | None:
    """When an event was simulated: what orders events as runs of the code.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Returns
    -------
    str or None
        When its earliest manifest was written (ISO 8601, UTC), else its origin
        time, else None.
    """
    row = db.execute(f"SELECT {SIMULATED_AT} AS at FROM events e WHERE e.event_id = ?", (event_id,)).fetchone()
    return None if row is None else row["at"]


def is_backlog(db: sqlite3.Connection, event_id: str) -> bool:
    """Whether an event was simulated out of order: after a later earthquake's simulation.

    Such an event was not taken as it happened (it was run by hand, or caught up
    after an outage), so the time from its origin to its simulation says nothing
    about how fast the workflow reacts.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Returns
    -------
    bool
        True when an earthquake with a later origin time was simulated before this one.
    """
    row = db.execute(
        f"SELECT 1 FROM events e, (SELECT origin_time, {SIMULATED_AT} AS at FROM events e WHERE e.event_id = ?) own"
        f" WHERE e.origin_time > own.origin_time AND {SIMULATED_AT} < own.at LIMIT 1",
        (event_id,),
    ).fetchone()
    return row is not None


def run_resolutions(db: sqlite3.Connection, event_id: str) -> list[str]:
    """The resolutions an event has runs at.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Returns
    -------
    list of str
        The resolutions, coarsest first.
    """
    order = {"0_25hz": 0, "0_5hz": 1, "1hz": 2}
    rows: Iterable[sqlite3.Row] = db.execute("SELECT resolution FROM runs WHERE event_id = ?", (event_id,))
    return sorted((row["resolution"] for row in rows), key=lambda r: order.get(r, 99))
