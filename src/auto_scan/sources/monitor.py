"""The Cylc monitor: read-only SQL against its Datasette.

The monitor answers ``/{database}.json?sql=...`` with a same-host 302 to
``/{database}/-/query.json``; urllib follows it and keeps the ``Authorization``
header, which it only drops for headers added as unredirected.
"""

import json
import sys
import urllib.parse
from typing import Any

from auto_scan.config import Config
from auto_scan.sources import _http

# Datasette truncates a custom query at its max_returned_rows (1000 by default);
# the jobs view is read in pages so a busy workflow is not silently cut short.
PAGE_ROWS = 1000


def query(config: Config, sql: str, **params: str | int) -> list[dict[str, Any]]:
    """Run one read-only SQL query against the monitor's database.

    Parameters
    ----------
    config : Config
        The scan's settings: ``monitor_url``, ``monitor_database`` and the reader token.
    sql : str
        The query, with ``:name`` placeholders.
    **params : str or int
        Values for the placeholders.

    Returns
    -------
    list of dict
        The rows, or ``[]`` (with a warning) when there is no reader token.

    Raises
    ------
    RuntimeError
        When the monitor answers with an error status.
    """
    token = config.monitor_reader_token
    if not token:
        print("auto-scan: no monitor reader token (CYLC_MONITOR_READER_TOKEN); skipping the monitor", file=sys.stderr)
        return []
    query_string = urllib.parse.urlencode({"sql": sql, "_shape": "array", **params})
    url = f"{config.monitor_url}/{config.monitor_database}.json?{query_string}"
    status, body = _http.get(url, config.user_agent, headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        raise RuntimeError(f"monitor query failed: HTTP {status}: {body[:300]!r}")
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise TypeError(f"monitor query returned {type(rows).__name__}, not rows: {body[:300]!r}")
    return rows


def fetch_jobs(config: Config) -> list[dict[str, Any]]:
    """Every row of the monitor's ``jobs`` view for the scanned workflow.

    Parameters
    ----------
    config : Config
        The scan's settings; ``config.workflow`` selects the rows.

    Returns
    -------
    list of dict
        The rows, whose keys are the ``jobs`` table's columns; ``[]`` without a token.
    """
    rows: list[dict[str, Any]] = []
    while True:
        # LIMIT and OFFSET are inlined: Datasette passes named parameters as text.
        page = query(
            config,
            "SELECT * FROM jobs WHERE workflow = :workflow ORDER BY cycle, name, submit_num"
            f" LIMIT {int(PAGE_ROWS)} OFFSET {len(rows)}",
            workflow=config.workflow,
        )
        rows.extend(page)
        # Stop on an empty page rather than a short one: the server's row cap may be below PAGE_ROWS.
        if not page:
            return rows
