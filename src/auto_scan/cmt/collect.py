"""Ingest the CMT inversions SeismicNow posts: download, record, extract.

Which events
------------
With event ids (``auto-scan --review cmt collect <id> ...``, or the workflow's
``events`` input), exactly those, whatever their age: this is how older events
are reviewed. Without, the new postings: SeismicNow's listing of earthquakes with
a CMT (the newest ``source.MAX_PAGES`` pages) is read, and an event is taken when

- it is not in the database and SeismicNow applied its CMT after the watermark
  (meta ``cmt_since``), or
- it is in the database and its ``cmt_applied_at`` changed (the CMT was re-run).

The first run with no watermark sets it to the current time and takes nothing,
so the automatic scan starts from the next posting and never works through the
backlog by itself. ``cmt_applied_at`` of each ingested event is kept in meta
``cmt_applied:<event_id>``.

One event
---------
The ``scope=all`` zip is unpacked into ``<cache_dir>/cmt/<event_id>/`` with
``listing.json`` beside it (SeismicNow's listing row, plus ``computed_at``, the
zip's time of ``cmt_solution.csv``). Then, in one transaction: an event seen
before loses every row derived from its old CMT (runs, metrics, evaluations; its
findings stay, so a re-run that passes can verify a fix); its ``events`` row and
one ``runs`` row a run of ``cmt_selection.csv`` are written; every CMT extractor
runs. ``runs.written`` is ``computed_at`` (else ``cmt_applied_at``, else now): it
orders events as runs of the code. An event whose download has no
``cmt_selection.csv`` gets no runs; its ``cmt_applied`` meta row is still written,
so it isn't downloaded again until SeismicNow re-applies it.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

from auto_scan import registry
from auto_scan.cmt import source
from auto_scan.cmt.extract import first_row, number, read_rows
from auto_scan.config import Config

WATERMARK = "cmt_since"


def _now() -> str:
    """The current time, UTC ISO 8601 to the second."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _log(message: str) -> None:
    """One line on stderr."""
    print(f"auto-scan: cmt: {message}", file=sys.stderr, flush=True)


def _meta(db: sqlite3.Connection, key: str) -> str | None:
    """A meta value."""
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def _set_meta(db: sqlite3.Connection, key: str, value: str | None) -> None:
    """Set a meta value (None deletes it)."""
    if value is None:
        db.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def event_cache(config: Config, event_id: str) -> Path:
    """Where an event's CMT download is unpacked.

    Parameters
    ----------
    config : Config
        The settings.
    event_id : str
        The event.

    Returns
    -------
    Path
        ``<cache_dir>/cmt/<event_id>``.
    """
    return config.cache_dir / "cmt" / event_id


def _forget(db: sqlite3.Connection, event_id: str) -> None:
    """Delete every row derived from an event's CMT, keeping its findings."""
    for table in ("runs", "run_sources", "metrics", "evaluations"):
        db.execute(f"DELETE FROM {table} WHERE event_id = ?", (event_id,))


def _event_row(event_id: str, listed: source.CmtEvent | None, event_dir: Path) -> dict[str, Any]:
    """The ``events`` row: SeismicNow's active values, else the download's own."""
    origin = first_row(read_rows(event_dir / "event.csv"))
    solution = first_row(read_rows(event_dir / "cmt_solution.csv"))
    row: dict[str, Any] = {
        "event_id": event_id,
        "cycle": None,
        "event_dir": event_id,
        "origin_time": (listed.origin_time if listed else None) or source.http_date_to_iso(origin.get("datetime")),
        "magnitude": (listed.magnitude if listed else None) or number(solution.get("Mw")),
        "latitude": (listed.latitude if listed else None) or number(solution.get("Latitude")),
        "longitude": (listed.longitude if listed else None) or number(solution.get("Longitude")),
        "depth_km": (listed.depth_km if listed else None) or number(solution.get("CD")),
        "location": listed.locality if listed else None,
    }
    return row


def _sha256(path: Path) -> str | None:
    """A file's sha256, or None if it is missing."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def ingest(config: Config, db: sqlite3.Connection, event_id: str, listed: source.CmtEvent | None) -> bool:
    """Download, record and extract one event's CMT.

    Parameters
    ----------
    config : Config
        The settings.
    db : sqlite3.Connection
        The CMT database; committed once.
    event_id : str
        The event.
    listed : source.CmtEvent or None
        Its row in SeismicNow's listing, if known.

    Returns
    -------
    bool
        True when runs were recorded; False when SeismicNow has no CMT results
        for it, or they have no ``cmt_selection.csv``.
    """
    event_dir = event_cache(config, event_id)
    times = source.download_results(config, event_id, event_dir)
    applied = listed.applied_at if listed else None
    if times is None:
        _log(f"{event_id}: SeismicNow has no CMT results")
        return False
    computed = times.get("cmt_solution.csv") or times.get("cmt_selection.csv")
    computed_at = f"{computed}+00:00" if computed else None
    listing = {**(dataclasses.asdict(listed) if listed else {"event_id": event_id}), "computed_at": computed_at}
    (event_dir / "listing.json").write_text(json.dumps(listing, indent=2))

    _forget(db, event_id)
    _set_meta(db, f"cmt_applied:{event_id}", applied or "")
    selection = read_rows(event_dir / "cmt_selection.csv")
    if not selection:
        db.commit()
        _log(f"{event_id}: the download has no cmt_selection.csv; nothing to review")
        return False
    row = _event_row(event_id, listed, event_dir)
    columns = list(row)
    db.execute(
        f"INSERT INTO events ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
        f" ON CONFLICT (event_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in columns[1:])}",
        [row[c] for c in columns],
    )
    written = computed_at or applied or _now()
    sha = _sha256(event_dir / "cmt_selection.csv")
    now = _now()
    for run in selection:
        if run.get("run"):
            db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, run["run"], run.get("status") or None, written, None, sha, now),
            )
    failures = []
    for extractor in registry.CMT_EXTRACTORS:
        try:
            extractor(db, event_id, event_dir)
        except Exception:  # noqa: BLE001 - one extractor's bug must not lose the event
            failures.append(getattr(extractor, "__qualname__", str(extractor)))
            _log(f"{event_id}: extractor {failures[-1]} failed:\n{traceback.format_exc()}")
    db.commit()
    statuses = ", ".join(f"{r['run']} {r.get('status')}" for r in selection if r.get("run"))
    _log(f"{event_id}: {statuses}; computed {computed_at}, applied {applied}; {len(failures)} extractor failure(s)")
    return True


def new_postings(db: sqlite3.Connection, listed: list[source.CmtEvent], since: str) -> list[source.CmtEvent]:
    """The listed events to ingest: posted after ``since``, or re-applied since ingested.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    listed : list of source.CmtEvent
        SeismicNow's listing.
    since : str
        The watermark, ISO 8601 UTC.

    Returns
    -------
    list of source.CmtEvent
        The events to ingest, oldest posting first.
    """
    chosen = []
    for event in listed:
        if not event.applied_at:
            continue
        seen = _meta(db, f"cmt_applied:{event.event_id}")
        if seen is None:
            if event.applied_at > since:
                chosen.append(event)
        elif seen != event.applied_at:
            chosen.append(event)
    return sorted(chosen, key=lambda event: event.applied_at or "")


def collect(config: Config, db: sqlite3.Connection, events: list[str] | None = None, force: bool = False) -> None:
    """Ingest new CMT postings, or the events named.

    Parameters
    ----------
    config : Config
        The settings.
    db : sqlite3.Connection
        The CMT database; committed after each event.
    events : list of str, optional
        Only these event ids, whatever their age. Each is ingested unless its
        ``cmt_applied_at`` is unchanged since it was (or ``force``).
    force : bool, optional
        Re-ingest named events even when unchanged.
    """
    registry.load_cmt_plugins()
    if events:
        for event_id in dict.fromkeys(events):
            try:
                listed = source.find_event(config, event_id)
                seen = _meta(db, f"cmt_applied:{event_id}")
                known = db.execute("SELECT 1 FROM runs WHERE event_id = ?", (event_id,)).fetchone() is not None
                if not force and known and listed is not None and seen == (listed.applied_at or ""):
                    _log(f"{event_id}: unchanged since scanned (use --force to redo)")
                    continue
                ingest(config, db, event_id, listed)
            except Exception:  # noqa: BLE001 - one event's bad download must not stop the others
                db.rollback()
                _log(f"{event_id} failed:\n{traceback.format_exc()}")
        return
    since = _meta(db, WATERMARK)
    if since is None:
        _set_meta(db, WATERMARK, _now())
        db.commit()
        _log(f"first run: reviewing CMTs posted from now ({_meta(db, WATERMARK)}); name older events to review them")
        return
    try:
        listed = source.list_cmt_events(config)
    except Exception:  # noqa: BLE001 - SeismicNow being down must not stop the verify and push after
        _log(f"listing SeismicNow's CMTs failed:\n{traceback.format_exc()}")
        return
    chosen = new_postings(db, listed, since)
    _log(f"{len(listed)} listed CMT event(s), {len(chosen)} new or re-run since scanned")
    for event in chosen:
        try:
            ingest(config, db, event.event_id, event)
        except Exception:  # noqa: BLE001
            db.rollback()
            _log(f"{event.event_id} failed:\n{traceback.format_exc()}")
