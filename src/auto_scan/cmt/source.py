"""SeismicNow's CMT endpoints: which events have a posted CMT, and each one's results.

- ``GET {seismicnow_api}/earthquakes?page=<n>&has_cmt=true&...`` lists the
  earthquakes with a main CMT solution, newest origin first, 25 a page, pages
  numbered from 0. Every filter key must be present, even empty. Each row has
  ``cmt_applied_at``: when SeismicNow applied (or re-applied, after a re-run) the
  solution, an HTTP date such as ``Tue, 17 Nov 2026 10:58:38 GMT``.
- ``GET {seismicnow_api}/download/cmt_results?event_id=<id>&scope=all`` is a zip of
  every run's tables, figures, reports and logs (``README.txt`` in it lists them).

Both are public; Cloudflare in front needs ``Config.user_agent``.
"""

import datetime as dt
import email.utils
import io
import json
import sys
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auto_scan.config import Config
from auto_scan.sources import _http
from auto_scan.sources.seismicnow import unpack_zip

# The listing's empty filters: SeismicNow answers 400 when any key is missing.
LIST_FILTERS = {"event_id": "", "min_magnitude": "", "max_magnitude": "", "min_depth": "", "max_depth": ""}
# SeismicNow's page size (constants.earthquakes_per_page).
PAGE_SIZE = 25
# How far back the listing is read when looking for new CMTs: 4 pages of 25 is
# weeks of NZ earthquakes, while a CMT is posted within hours of its earthquake.
MAX_PAGES = 4


@dataclass(frozen=True)
class CmtEvent:
    """One earthquake in SeismicNow's listing with a posted CMT.

    Attributes
    ----------
    event_id : str
        The GeoNet event id.
    origin_time : str or None
        ISO 8601 UTC origin time.
    applied_at : str or None
        ISO 8601 UTC time SeismicNow applied the CMT solution, None if it never
        did (the event has a selected run but its solution was not applied).
    magnitude : float or None
        The active magnitude (the CMT's Mw once applied).
    latitude, longitude, depth_km : float or None
        The active location (the CMT centroid once applied).
    locality : str or None
        "15 km south-east of Arthur's Pass".
    geonet_magnitude : float or None
        GeoNet's magnitude.
    """

    event_id: str
    origin_time: str | None = None
    applied_at: str | None = None
    magnitude: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    depth_km: float | None = None
    locality: str | None = None
    geonet_magnitude: float | None = None


def http_date_to_iso(value: str | None) -> str | None:
    """An HTTP date (Flask's datetime serialisation) or ISO 8601 time as ISO 8601 UTC.

    Parameters
    ----------
    value : str or None
        e.g. ``Tue, 17 Nov 2026 10:58:38 GMT`` or ``2026-11-17T10:30:48.522607Z``.

    Returns
    -------
    str or None
        The time, e.g. ``2026-11-17T10:58:38+00:00``; None when absent or unparsable.
    """
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC).isoformat(timespec="seconds")


def _float(value: Any) -> float | None:
    """A JSON number as a float, else None."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def parse_event(row: dict[str, Any]) -> CmtEvent:
    """One listing row as a ``CmtEvent``.

    Parameters
    ----------
    row : dict
        A row of ``GET /earthquakes``.

    Returns
    -------
    CmtEvent
        Its fields; absent ones are None.
    """
    origin = row.get("origin_time_iso") or row.get("origin_time")
    return CmtEvent(
        event_id=str(row["event_id"]),
        origin_time=http_date_to_iso(origin) if origin else None,
        applied_at=http_date_to_iso(row.get("cmt_applied_at")),
        magnitude=_float(row.get("magnitude")),
        latitude=_float(row.get("latitude")),
        longitude=_float(row.get("longitude")),
        depth_km=_float(row.get("depth")),
        locality=row.get("locality"),
        geonet_magnitude=_float(row.get("geonet_magnitude")),
    )


def _get_page(config: Config, params: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
    """One page of the listing and whether more follow; a 404 is an empty page."""
    url = f"{config.seismicnow_api}/earthquakes?{urllib.parse.urlencode(params)}"
    status, body = _http.get(url, config.user_agent)
    if status == 404:
        return [], False
    if status != 200:
        raise RuntimeError(f"SeismicNow earthquakes: HTTP {status}: {body[:300]!r}")
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise TypeError(f"SeismicNow earthquakes: not a list: {body[:300]!r}")
    # X-Has-More isn't visible through _http.get; a full page means there may be more.
    return rows, len(rows) >= PAGE_SIZE


def list_cmt_events(config: Config, max_pages: int = MAX_PAGES) -> list[CmtEvent]:
    """The newest earthquakes with a posted CMT, newest origin first.

    Parameters
    ----------
    config : Config
        The settings (``seismicnow_api``, ``user_agent``).
    max_pages : int, optional
        The most pages of 25 read.

    Returns
    -------
    list of CmtEvent
        The events, from at most ``max_pages`` pages.
    """
    events: list[CmtEvent] = []
    for page in range(max_pages):
        rows, more = _get_page(config, {**LIST_FILTERS, "page": str(page), "has_cmt": "true"})
        events += [parse_event(row) for row in rows if isinstance(row, dict) and row.get("event_id")]
        if not more:
            break
    return events


def find_event(config: Config, event_id: str) -> CmtEvent | None:
    """One event's listing row, wherever it is in the listing.

    Parameters
    ----------
    config : Config
        The settings.
    event_id : str
        The GeoNet event id.

    Returns
    -------
    CmtEvent or None
        The event, or None when SeismicNow has no such earthquake.
    """
    # The event_id filter matches a substring of the id or the locality: keep the exact one.
    rows, _ = _get_page(config, {**LIST_FILTERS, "event_id": event_id, "page": "0"})
    for row in rows:
        if isinstance(row, dict) and row.get("event_id") == event_id:
            return parse_event(row)
    return None


def download_results(config: Config, event_id: str, dest: Path) -> dict[str, str] | None:
    """Download every CMT run of an event and unpack it into ``dest``.

    Parameters
    ----------
    config : Config
        The settings.
    event_id : str
        The GeoNet event id.
    dest : Path
        The directory to unpack into; replaced when the download succeeds and
        left alone when it doesn't.

    Returns
    -------
    dict of str to str, or None
        Each member's modification time in the zip (``YYYY-MM-DDTHH:MM:SS``, the
        server's clock, which is UTC: ``cmt_solution.csv``'s is seconds before
        ``cmt_applied_at``), by path; None when SeismicNow has no results (404, or a
        body that is not a zip).
    """
    query = urllib.parse.urlencode({"event_id": event_id, "scope": "all"})
    status, body = _http.get(f"{config.seismicnow_api}/download/cmt_results?{query}", config.user_agent, timeout=300)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"SeismicNow CMT results for {event_id}: HTTP {status}: {body[:300]!r}")
    if not zipfile.is_zipfile(io.BytesIO(body)):
        print(f"auto-scan: SeismicNow returned no CMT zip for {event_id}: {body[:120]!r}", file=sys.stderr)
        return None
    unpack_zip(body, dest)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return {
            info.filename: dt.datetime(*info.date_time).isoformat() for info in archive.infolist() if not info.is_dir()
        }
