"""Ingest new or changed runs: download, record, extract.

For each event directory under ``results_url`` (the highest cycle when an event
was simulated twice):

1. list its files (one ``rclone lsjson -R``) and download every run's
   ``manifest.json`` (one ``rclone copy``);
2. if no run's manifest hash differs from ``runs.manifest_sha256`` (and not
   ``force``), stop there;
3. otherwise download the event's realisation files and *every* run's small
   files and SimDB outputs (one ``rclone copy``), so the event extractors always
   see all resolutions, and rescan every run of the event;
4. download SeismicNow's flatfiles into ``observed/`` and SimDB's station IMs
   into ``simdb_station_ims.json``;
5. fill ``events``, ``runs`` and ``run_sources``, run every extractor on every
   run and every event extractor on the event, and commit once.

The monitor's ``jobs`` view is copied whole once per call.

Late observations
-----------------
SeismicNow publishes flatfiles and SimDB ingests an event some time after the
simulation finishes. The meta row ``observed:<event_id>`` holds JSON
``{"flatfiles": bool, "station_ims": bool, "checked_at": ISO time}`` from the
last attempt. On every call, an event from the last ``OBSERVED_RETRY_DAYS``
days whose row says either is missing is probed again (one request each); when
something new has appeared, the whole event is rescanned as if forced, because
per-run extractors (station residuals) need the observations too.
"""

import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from auto_scan import registry
from auto_scan.config import Config
from auto_scan.sources import monitor, results, seismicnow, simdb

# SeismicNow's flatfiles usually appear within a day; two weeks covers a slow
# reprocessing without probing every old event forever.
OBSERVED_RETRY_DAYS = 14

JOB_COLUMNS = (
    "workflow", "cycle", "name", "submit_num", "try_num", "job_id", "platform", "runner", "state",
    "time_submit", "time_run", "time_run_exit", "queue_s", "run_s", "cores", "nodes", "chunks", "mem_gb",
    "time_limit_min", "account", "partition", "core_hours", "rsu", "max_rss_gb", "mem_allocated_gb",
    "cpu_hours", "cpu_efficiency", "job_err",
)  # fmt: skip


def _now() -> str:
    """The current time, UTC ISO 8601 to the second."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _log(message: str) -> None:
    """One line on stderr."""
    print(f"auto-scan: {message}", file=sys.stderr, flush=True)


def to_utc(timestamp: str | None) -> str | None:
    """An ISO 8601 timestamp converted to UTC.

    Parameters
    ----------
    timestamp : str or None
        Any ISO 8601 time; one with no offset is taken as UTC.

    Returns
    -------
    str or None
        The same instant in UTC (``+00:00``), or None if unparseable.
    """
    if not timestamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat()


def _read_json(path: Path) -> Any:
    """A JSON file, or None if it is missing or malformed."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        _log(f"unreadable {path}: {error}")
        return None


def _sha256(path: Path) -> str:
    """The SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def realisation_magnitude(realisation: dict[str, Any]) -> float | None:
    """The simulated moment magnitude: the moment sum of every segment's.

    Parameters
    ----------
    realisation : dict
        A ``realisation.json``.

    Returns
    -------
    float or None
        Mw, or None when the file has no magnitudes.
    """
    magnitudes = (realisation.get("magnitudes") or {}).get("magnitudes") or {}
    values = [float(value) for value in magnitudes.values() if value is not None]
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    moment = sum(10 ** (1.5 * mw + 9.1) for mw in values)
    return (math.log10(moment) - 9.1) / 1.5


def realisation_hypocentre(realisation: dict[str, Any]) -> tuple[float, float, float] | None:
    """The simulated hypocentre: (latitude, longitude, depth in km).

    The hypocentre is ``(s, d)`` along strike and down dip of the initial
    segment, the root of the rupture causality tree. Its corners run along the
    top edge, then back along the bottom, so the point is a bilinear blend.

    Parameters
    ----------
    realisation : dict
        A ``realisation.json``.

    Returns
    -------
    tuple of float or None
        The hypocentre, or None when the geometry is missing or not a planar fault.
    """
    try:
        propagation = realisation["rupture_propagation"]
        tree = propagation["rupture_causality_tree"]
        roots = [name for name, parent in tree.items() if parent is None]
        corners = realisation["sources"]["source_geometries"][roots[0]]["corners"]
        s = float(propagation["hypocentre"]["s"])
        d = float(propagation["hypocentre"]["d"])
        if len(corners) != 4:
            return None
        top_start, top_end, bottom_end, bottom_start = corners
        point = []
        for key in ("latitude", "longitude", "depth"):
            top = top_start[key] + s * (top_end[key] - top_start[key])
            bottom = bottom_start[key] + s * (bottom_end[key] - bottom_start[key])
            point.append(top + d * (bottom - top))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    return point[0], point[1], point[2] / 1000.0


def _observed_origin_time(event_dir: Path, event_id: str) -> str | None:
    """The origin time from SeismicNow's ``earthquake_source_table.csv``."""
    path = event_dir / "observed" / "earthquake_source_table.csv"
    try:
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("evid") == event_id:
                    return to_utc(row.get("datetime"))
    except (OSError, csv.Error):
        return None
    return None


def event_row(
    config: Config, event: results.EventDir, event_dir: Path, resolutions: list[str], summary_fetcher: Any = None
) -> dict[str, Any]:
    """The ``events`` row of an event, from its downloaded files.

    Origin time and location come from ``<res>/shake_movie.json``, magnitude and
    hypocentre from ``realisation.json`` (as simulated); SimDB's event summary and
    SeismicNow's source table fill whatever is still missing.

    Parameters
    ----------
    config : Config
        The scan's settings.
    event : results.EventDir
        The event's directory.
    event_dir : Path
        Its cache directory.
    resolutions : list of str
        The runs present.
    summary_fetcher : callable, optional
        ``f(config, event_id)`` returning SimDB's summary; by default ``simdb.event_summary``.

    Returns
    -------
    dict
        The row, keyed by column.
    """
    row: dict[str, Any] = {
        "event_id": event.event_id,
        "cycle": event.cycle,
        "event_dir": event.name,
        "origin_time": None,
        "magnitude": None,
        "latitude": None,
        "longitude": None,
        "depth_km": None,
        "location": None,
    }
    for resolution in reversed(resolutions):
        movie = _read_json(event_dir / resolution / "shake_movie.json")
        if isinstance(movie, dict):
            row["origin_time"] = row["origin_time"] or to_utc(movie.get("time"))
            row["location"] = row["location"] or movie.get("location")
            row["magnitude"] = row["magnitude"] if row["magnitude"] is not None else movie.get("magnitude")
    candidates = [event_dir / "realisation.json"] + [event_dir / r / "realisation.json" for r in resolutions]
    for path in candidates:
        realisation = _read_json(path)
        if not isinstance(realisation, dict):
            continue
        magnitude = realisation_magnitude(realisation)
        if magnitude is not None:
            row["magnitude"] = magnitude  # the simulated Mw wins over the movie's rounded one
        hypocentre = realisation_hypocentre(realisation)
        if hypocentre is not None:
            row["latitude"], row["longitude"], row["depth_km"] = hypocentre
        if magnitude is not None and hypocentre is not None:
            break
    if row["origin_time"] is None:
        row["origin_time"] = _observed_origin_time(event_dir, event.event_id)
    if any(row[key] is None for key in ("magnitude", "latitude", "longitude", "depth_km")):
        try:
            summary = (summary_fetcher or simdb.event_summary)(config, event.event_id) or {}
        except Exception as error:  # noqa: BLE001 - a fallback must not stop the scan
            _log(f"{event.name}: SimDB summary failed: {error}")
            summary = {}
        for column, key in (("magnitude", "magnitude"), ("latitude", "hypo_lat"), ("longitude", "hypo_lon")):
            if row[column] is None:
                row[column] = summary.get(key)
        if row["depth_km"] is None:
            row["depth_km"] = summary.get("hypo_depth")
    return row


def source_rows(manifest: dict[str, Any]) -> list[tuple[str, str | None, str | None, str | None]]:
    """The ``run_sources`` rows of a manifest: ``(source, url, ref, commit_sha)``.

    Parameters
    ----------
    manifest : dict
        A run's ``manifest.json``.

    Returns
    -------
    list of tuple
        One per source. A data source with no commit gives its content hash
        (``sha256``, else ``models_sha256``) as ``commit_sha``.
    """
    rows = []
    for name, source in (manifest.get("sources") or {}).items():
        if not isinstance(source, dict):
            continue
        commit = source.get("commit") or source.get("sha256") or source.get("models_sha256")
        rows.append((name, source.get("url"), source.get("ref"), commit))
    return rows


def _record_run(db: sqlite3.Connection, event_id: str, resolution: str, manifest: dict[str, Any], sha: str) -> None:
    """Write a run's ``runs`` and ``run_sources`` rows."""
    db.execute(
        "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            resolution,
            manifest.get("status"),
            to_utc(manifest.get("written")),
            manifest.get("defaults_version"),
            sha,
            _now(),
        ),
    )
    db.execute("DELETE FROM run_sources WHERE event_id = ? AND resolution = ?", (event_id, resolution))
    db.executemany(
        "INSERT INTO run_sources VALUES (?, ?, ?, ?, ?, ?)",
        [(event_id, resolution, *row) for row in source_rows(manifest)],
    )


def _upsert_event(db: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert or update an ``events`` row without disturbing its runs."""
    columns = list(row)
    db.execute(
        f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
        f" ON CONFLICT (event_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in columns[1:])}",
        [row[c] for c in columns],
    )


def fetch_observed(config: Config, event_id: str, event_dir: Path) -> dict[str, Any]:
    """Refresh an event's observed flatfiles and SimDB station IMs in its cache.

    Parameters
    ----------
    config : Config
        The scan's settings.
    event_id : str
        The event.
    event_dir : Path
        Its cache directory.

    Returns
    -------
    dict
        ``{"flatfiles": bool, "station_ims": bool, "checked_at": str}``: what is now present.
    """
    status = {"flatfiles": False, "station_ims": False, "checked_at": _now()}
    event_dir.mkdir(parents=True, exist_ok=True)
    try:
        status["flatfiles"] = seismicnow.download_flatfiles(config, event_id, event_dir / "observed")
    except Exception as error:  # noqa: BLE001 - a missing observation must not stop the scan
        _log(f"{event_id}: SeismicNow flatfiles failed: {error}")
    if not status["flatfiles"]:
        # A stale observed/ from an earlier download is kept; report what is on disk.
        status["flatfiles"] = (event_dir / "observed").is_dir()
    try:
        ims = simdb.station_ims(config, event_id)
    except Exception as error:  # noqa: BLE001
        _log(f"{event_id}: SimDB station-ims failed: {error}")
        ims = None
    target = event_dir / "simdb_station_ims.json"
    if ims is not None:
        target.write_text(json.dumps(ims))
    status["station_ims"] = target.is_file()
    return status


def _observed_meta(db: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    """The last ``observed:<event_id>`` record."""
    row = db.execute("SELECT value FROM meta WHERE key = ?", (f"observed:{event_id}",)).fetchone()
    return None if row is None else json.loads(row["value"])


def _set_observed_meta(db: sqlite3.Connection, event_id: str, status: dict[str, Any]) -> None:
    """Record what observed data an event had at its last check."""
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"observed:{event_id}", json.dumps(status)))


def _name(function: object) -> str:
    """A plug-in's dotted name, for log lines."""
    return f"{getattr(function, '__module__', '?')}.{getattr(function, '__qualname__', repr(function))}"


def _run_extractors(db: sqlite3.Connection, event_id: str, event_dir: Path, resolutions: list[str]) -> list[str]:
    """Run every extractor on every run, then every event extractor; return the failures."""
    failures = []
    for resolution in resolutions:
        for function in registry.EXTRACTORS:
            try:
                function(db, event_id, resolution, event_dir / resolution)
            except Exception:  # noqa: BLE001 - one extractor's bug must not stop the others
                failures.append(f"{_name(function)}[{resolution}]")
                _log(f"extractor {failures[-1]} failed on {event_id}:\n{traceback.format_exc()}")
    for function in registry.EVENT_EXTRACTORS:
        try:
            function(db, event_id, event_dir)
        except Exception:  # noqa: BLE001
            failures.append(_name(function))
            _log(f"event extractor {failures[-1]} failed on {event_id}:\n{traceback.format_exc()}")
    return failures


def _drop_large_files(event_dir: Path) -> None:
    """Delete the event's ``*.duckdb`` files unless ``AUTO_SCAN_KEEP_CACHE=1``."""
    if os.environ.get("AUTO_SCAN_KEEP_CACHE") == "1":
        return
    for path in event_dir.rglob("*.duckdb"):
        path.unlink(missing_ok=True)


def scan_event(config: Config, db: sqlite3.Connection, event: results.EventDir, force: bool = False) -> bool:
    """Ingest one event if any of its runs is new or changed.

    Parameters
    ----------
    config : Config
        The scan's settings.
    db : sqlite3.Connection
        The scan database; committed once when the event is ingested.
    event : results.EventDir
        The event's directory.
    force : bool, optional
        Rescan even when no manifest changed.

    Returns
    -------
    bool
        True when the event was (re)scanned.
    """
    files = results.list_files(config, event.name)
    paths = {entry["Path"] for entry in files}
    resolutions = [r for r in config.resolutions if f"{r}/manifest.json" in paths]
    if not resolutions:
        _log(f"{event.name}: no run has a manifest.json yet")
        return False
    event_dir = config.event_cache(event.name)
    # Manifests go to a scratch directory first: an unchanged event leaves the cache alone.
    with tempfile.TemporaryDirectory(prefix="manifests-") as scratch:
        scratch_config = _with_cache(config, Path(scratch))
        results.copy(scratch_config, event.name, results.manifest_includes(resolutions))
        hashes = {}
        for resolution in resolutions:
            path = scratch_config.event_cache(event.name) / resolution / "manifest.json"
            if path.is_file():
                hashes[resolution] = _sha256(path)
    resolutions = [r for r in resolutions if r in hashes]
    known = {
        row["resolution"]: row["manifest_sha256"]
        for row in db.execute(
            "SELECT r.resolution, r.manifest_sha256 FROM runs r JOIN events e USING (event_id)"
            " WHERE r.event_id = ? AND e.event_dir = ?",
            (event.event_id, event.name),
        )
    }
    if not force and all(known.get(r) == hashes[r] for r in resolutions):
        return False

    results.download_runs(config, event.name, resolutions)
    observed = fetch_observed(config, event.event_id, event_dir)
    manifests = {}
    for resolution in resolutions:
        manifest = _read_json(event_dir / resolution / "manifest.json")
        manifests[resolution] = manifest if isinstance(manifest, dict) else {}
    _upsert_event(db, event_row(config, event, event_dir, resolutions))
    # Runs no longer uploaded (e.g. a resolution dropped on a re-run) are forgotten with their sources.
    recorded = [row[0] for row in db.execute("SELECT resolution FROM runs WHERE event_id = ?", (event.event_id,))]
    stale = [r for r in recorded if r not in resolutions]
    for resolution in stale:
        db.execute("DELETE FROM runs WHERE event_id = ? AND resolution = ?", (event.event_id, resolution))
        db.execute("DELETE FROM run_sources WHERE event_id = ? AND resolution = ?", (event.event_id, resolution))
    for resolution in resolutions:
        sha = _sha256(event_dir / resolution / "manifest.json")
        _record_run(db, event.event_id, resolution, manifests[resolution], sha)
    _set_observed_meta(db, event.event_id, observed)
    failures = _run_extractors(db, event.event_id, event_dir, resolutions)
    db.commit()
    _drop_large_files(event_dir)
    for resolution in resolutions:
        changed = (
            "new"
            if resolution not in known
            else ("changed" if known[resolution] != hashes[resolution] else "rescanned")
        )
        _log(
            f"{event.name} {resolution}: {manifests[resolution].get('status', '?')}, {changed};"
            f" observed {'yes' if observed['flatfiles'] else 'no'}, simdb {'yes' if observed['station_ims'] else 'no'};"
            f" {len(failures)} extractor failure(s)"
        )
    return True


def _with_cache(config: Config, cache_dir: Path) -> Config:
    """The same settings with another cache directory."""
    return dataclasses.replace(config, cache_dir=cache_dir)


def copy_jobs(config: Config, db: sqlite3.Connection) -> int:
    """Replace the ``jobs`` rows of the workflow with the monitor's current view.

    Parameters
    ----------
    config : Config
        The scan's settings.
    db : sqlite3.Connection
        The scan database; committed.

    Returns
    -------
    int
        The rows copied. With no rows (no token, or the monitor unreachable) the
        old rows are kept rather than wiped.
    """
    try:
        rows = monitor.fetch_jobs(config)
    except Exception as error:  # noqa: BLE001 - the monitor being down must not stop the scan
        _log(f"monitor jobs failed: {error}")
        return 0
    if not rows:
        return 0
    db.execute("DELETE FROM jobs WHERE workflow = ?", (config.workflow,))
    db.executemany(
        f"INSERT OR REPLACE INTO jobs ({', '.join(JOB_COLUMNS)}) VALUES ({', '.join('?' * len(JOB_COLUMNS))})",
        [tuple(row.get(column) for column in JOB_COLUMNS) for row in rows],
    )
    db.commit()
    return len(rows)


def _retry_observed(config: Config, db: sqlite3.Connection, events: list[results.EventDir], scanned: set[str]) -> None:
    """Rescan recent events whose observations or SimDB IMs have appeared since."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=OBSERVED_RETRY_DAYS)).isoformat()
    by_id = {event.event_id: event for event in events}
    rows = db.execute(
        "SELECT event_id, event_dir FROM events WHERE origin_time >= ? ORDER BY origin_time", (cutoff,)
    ).fetchall()
    for row in rows:
        event = by_id.get(row["event_id"])
        if event is None or event.event_id in scanned or event.name != row["event_dir"]:
            continue
        before = _observed_meta(db, event.event_id) or {}
        if before.get("flatfiles") and before.get("station_ims"):
            continue
        status = fetch_observed(config, event.event_id, config.event_cache(event.name))
        gained = [key for key in ("flatfiles", "station_ims") if status[key] and not before.get(key)]
        _set_observed_meta(db, event.event_id, status)
        db.commit()
        if gained:
            _log(f"{event.name}: {' and '.join(gained)} now available; rescanning")
            scan_event(config, db, event, force=True)


def collect(config: Config, db: sqlite3.Connection, event: str | None = None, force: bool = False) -> None:
    """Ingest every new or changed run, and refresh the monitor's jobs.

    Parameters
    ----------
    config : Config
        The scan's settings.
    db : sqlite3.Connection
        The scan database; committed after each event.
    event : str, optional
        Only this GeoNet event id.
    force : bool, optional
        Rescan runs whose manifest is unchanged.
    """
    registry.load_plugins()
    copied = copy_jobs(config, db)
    _log(f"copied {copied} monitor job rows")
    events = results.list_events(config)
    if event is not None:
        events = [e for e in events if e.event_id == event]
        if not events:
            _log(f"no event directory for {event} under {config.results_url}")
            return
    scanned = set()
    for event_dir in events:
        try:
            if scan_event(config, db, event_dir, force=force):
                scanned.add(event_dir.event_id)
        except Exception:  # noqa: BLE001 - one event's bad upload must not stop the scan
            db.rollback()
            _log(f"{event_dir.name} failed:\n{traceback.format_exc()}")
    try:
        _retry_observed(config, db, events, scanned)
    except Exception:  # noqa: BLE001
        db.rollback()
        _log(f"retrying observed data failed:\n{traceback.format_exc()}")
