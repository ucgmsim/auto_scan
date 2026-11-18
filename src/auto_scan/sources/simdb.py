"""SimDB: the simulated intensity measures it has ingested for an event.

SimDB answers 400 (``unknown event_id``) or 404 for an event it has not
ingested; both mean "not yet", not an error.
"""

import json
import urllib.parse
from typing import Any

from auto_scan.config import Config
from auto_scan.sources import _http

NOT_INGESTED = (400, 404)


def _event_json(config: Config, event_id: str, endpoint: str) -> dict[str, Any] | None:
    """GET ``{simdb_api}/api/events/<id>/<endpoint>?dataset=realtime``; None when not ingested."""
    url = f"{config.simdb_api}/api/events/{urllib.parse.quote(event_id)}/{endpoint}?dataset=realtime"
    status, body = _http.get(url, config.user_agent)
    if status in NOT_INGESTED:
        return None
    if status != 200:
        raise RuntimeError(f"SimDB {endpoint} for {event_id}: HTTP {status}: {body[:300]!r}")
    return json.loads(body)


def station_ims(config: Config, event_id: str) -> dict[str, Any] | None:
    """The simulated intensity measures at real stations for an event.

    Parameters
    ----------
    config : Config
        The scan's settings (``simdb_api``, ``user_agent``).
    event_id : str
        The GeoNet event id.

    Returns
    -------
    dict or None
        SimDB's response (``event_id``, ``periods``, ``sites``), or None when the
        event is not ingested.
    """
    return _event_json(config, event_id, "station-ims")


def event_summary(config: Config, event_id: str) -> dict[str, Any] | None:
    """SimDB's summary of an event: magnitude, hypocentre, geometry.

    Parameters
    ----------
    config : Config
        The scan's settings (``simdb_api``, ``user_agent``).
    event_id : str
        The GeoNet event id.

    Returns
    -------
    dict or None
        The summary (``magnitude``, ``hypo_lat``, ``hypo_lon``, ``hypo_depth`` in km, ...),
        or None when the event is not ingested.
    """
    return _event_json(config, event_id, "summary")
