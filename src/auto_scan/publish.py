"""Move ``scan.db`` between this machine, the state store and the Datasette.

The state store (``Config.state_url``, an rclone path) holds the store of record:
every finding's history lives there and nowhere else. The Datasette only shows a
copy. So the one thing this module must never do is replace a populated store
with an empty or truncated database:

- ``pull`` tells a store that has no ``scan.db`` yet (a first run, which starts
  empty) from an rclone failure (which raises, so the run stops before it can
  push an empty database over the real one).
- ``pull`` records how many ``events`` rows it fetched (``meta.pulled_events``),
  and ``push`` refuses to upload fewer, unless ``AUTO_SCAN_ALLOW_SHRINK=1``. A
  ``push`` from a database that was never pulled checks the store itself and
  refuses if it already holds a ``scan.db``.

``push`` also keeps the first snapshot of each day under ``history/``, so a bad
upload can be undone by hand from yesterday's copy.

The CMT review's database is stored the same way under another name
(``store="cmt.db"``, history ``history/cmt-<YYYYMMDD>.db``) and uploaded to
``/-/scan/upload/cmt``, which makes it the Datasette's ``cmt`` database.
"""

import datetime
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

from auto_scan.config import Config

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Poster = Callable[[urllib.request.Request], bytes]

STORE_NAME = "scan.db"
CMT_STORE_NAME = "cmt.db"
SQLITE_HEADER = b"SQLite format 3\x00"
# rclone's exit codes for "directory not found" and "file not found"
# (https://rclone.org/docs/#exit-code). Anything else is a real failure.
RCLONE_NOT_FOUND = {3, 4}
UPLOAD_TIMEOUT_S = 300


class StoreError(RuntimeError):
    """The state store could not be read or written, or an upload would lose data."""


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing its output as text."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def _post(request: urllib.request.Request) -> bytes:
    """Send an HTTP request and return the response body."""
    with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT_S) as response:
        return response.read()


def _warn(message: str) -> None:
    """Print a one-line warning on stderr."""
    print(f"auto-scan: warning: {message}", file=sys.stderr)


def _store_path(config: Config, name: str = STORE_NAME) -> str:
    """The rclone path of a file in the state store."""
    return f"{config.state_url.rstrip('/')}/{name}"


def _remote_exists(path: str, runner: Runner) -> bool:
    """Whether an rclone path names an existing file.

    Raises
    ------
    StoreError
        If rclone fails for any reason other than the file (or its directory)
        not existing.
    """
    result = runner(["rclone", "lsjson", "--stat", path])
    if result.returncode == 0:
        return True
    if result.returncode in RCLONE_NOT_FOUND and "not found" in (result.stderr or "").lower():
        return False
    raise StoreError(f"rclone could not read {path} (exit {result.returncode}): {(result.stderr or '').strip()}")


def _count_events(path: Path) -> int:
    """The number of ``events`` rows in a database file (0 if it has no such table)."""
    connection = sqlite3.connect(path)
    try:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        if not has_table:
            return 0
        return int(connection.execute("SELECT count(*) FROM events").fetchone()[0])
    finally:
        connection.close()


def _get_meta(path: Path, key: str) -> str | None:
    """A value of the ``meta`` table, or None."""
    connection = sqlite3.connect(path)
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone():
            return None
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        connection.close()


def _set_meta(path: Path, key: str, value: str) -> None:
    """Set a value of the ``meta`` table, creating the table if needed."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    finally:
        connection.close()


def _remove_sidecars(db_path: Path) -> None:
    """Delete a database's journal files: a stale hot journal would be applied to the new file."""
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        sidecar.unlink(missing_ok=True)


def pull(config: Config, db_path: Path, *, runner: Runner | None = None, store: str = STORE_NAME) -> None:
    """Fetch ``scan.db`` (or ``store``) from the state store.

    When the store has no ``scan.db`` yet (the first run), nothing is written and
    ``auto_scan.db.connect`` later creates an empty database. Otherwise the local
    file is replaced, and the number of events it holds is recorded in
    ``meta.pulled_events`` for ``push``'s shrink guard.

    Parameters
    ----------
    config : Config
        The settings; ``state_url`` is read.
    db_path : Path
        Where the local ``scan.db`` goes.
    runner : callable, optional
        Runs a command and returns its ``CompletedProcess`` (for tests); by
        default ``subprocess.run`` with captured text output.
    store : str, optional
        The file's name in the state store, by default ``scan.db``.

    Raises
    ------
    StoreError
        If rclone fails for any reason other than the file not existing, or the
        fetched file is not an SQLite database.
    """
    runner = runner or _run
    db_path = Path(db_path)
    remote = _store_path(config, store)
    if not _remote_exists(remote, runner):
        if db_path.exists():
            _warn(f"{remote} does not exist; keeping the local {db_path}, which push will upload")
        else:
            print(f"auto-scan: {remote} does not exist yet; starting from an empty database", file=sys.stderr)
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=db_path.parent, prefix=".pull-") as tmp:
        fetched = Path(tmp) / store
        result = runner(["rclone", "copyto", remote, str(fetched)])
        if result.returncode != 0 or not fetched.is_file():
            raise StoreError(
                f"rclone copyto {remote} failed (exit {result.returncode}): {(result.stderr or '').strip()}"
            )
        with fetched.open("rb") as handle:
            if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                raise StoreError(f"{remote} is not an SQLite database")
        events = _count_events(fetched)
        _set_meta(fetched, "pulled_events", str(events))
        _remove_sidecars(db_path)
        os.replace(fetched, db_path)
    print(f"auto-scan: pulled {remote} ({events} events)", file=sys.stderr)


def _snapshot(db_path: Path, out: Path) -> None:
    """Write a consistent, compact copy of a database (``VACUUM INTO``)."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("VACUUM INTO ?", (str(out),))
    finally:
        connection.close()


def _check_shrink(config: Config, db_path: Path, events: int, runner: Runner, store: str = STORE_NAME) -> None:
    """Refuse an upload that holds fewer events than the store did when pulled.

    Raises
    ------
    StoreError
        If the upload would shrink the store and ``AUTO_SCAN_ALLOW_SHRINK`` is not 1.
    """
    if os.environ.get("AUTO_SCAN_ALLOW_SHRINK") == "1":
        return
    pulled = _get_meta(db_path, "pulled_events")
    if pulled is None:
        # Never pulled: a local database must not silently replace the store's.
        if _remote_exists(_store_path(config, store), runner):
            raise StoreError(
                f"{db_path} was never pulled but {_store_path(config, store)} exists: run `auto-scan pull` first, "
                "or set AUTO_SCAN_ALLOW_SHRINK=1 to overwrite the store"
            )
        return
    if events < int(pulled):
        raise StoreError(
            f"{db_path} has {events} events but the store had {pulled} when pulled: refusing to upload a smaller "
            "database (set AUTO_SCAN_ALLOW_SHRINK=1 if this is intended)"
        )


def _upload_datasette(config: Config, snapshot: Path, token: str, poster: Poster, store: str = STORE_NAME) -> None:
    """POST a gzipped snapshot to the Datasette's upload route; failures are warnings."""
    url = f"{config.datasette_url}/-/scan/upload"
    if store != STORE_NAME:
        url += f"/{Path(store).stem}"
    body = gzip.compress(snapshot.read_bytes(), compresslevel=6)
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": config.user_agent,
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        reply = poster(request)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        _warn(f"Datasette upload to {url} failed: HTTP {error.code} {detail}")
        return
    except (urllib.error.URLError, OSError) as error:
        _warn(f"Datasette upload to {url} failed: {error}")
        return
    try:
        answer = json.loads(reply or b"{}")
    except ValueError:
        answer = {}
    if not isinstance(answer, dict) or not answer.get("ok"):
        _warn(f"Datasette upload to {url} was not accepted: {reply[:500]!r}")
        return
    print(f"auto-scan: uploaded to {url} ({len(body)} bytes gzipped)", file=sys.stderr)


def push(
    config: Config,
    db_path: Path,
    datasette: bool = True,
    *,
    runner: Runner | None = None,
    poster: Poster | None = None,
    today: datetime.date | None = None,
    store: str = STORE_NAME,
) -> None:
    """Store ``scan.db`` (or ``store``) in the state store, then upload a copy to the Datasette.

    A consistent snapshot (``VACUUM INTO``) is copied to ``{state_url}/scan.db``
    and, if the day has none yet, to ``{state_url}/history/scan-<YYYYMMDD>.db``.
    Then, if ``datasette`` and a writer token exists, it is POSTed gzipped to
    ``{datasette_url}/-/scan/upload``. The store is the record, so a failure
    there raises; the Datasette is only a view, so a failure there warns.

    Parameters
    ----------
    config : Config
        The settings; ``state_url``, ``datasette_url``, ``user_agent`` and the
        writer token are read.
    db_path : Path
        The local ``scan.db``.
    datasette : bool, optional
        Whether to upload to the Datasette too, by default True.
    runner : callable, optional
        Runs a command and returns its ``CompletedProcess`` (for tests).
    poster : callable, optional
        Sends a ``urllib.request.Request`` and returns the body (for tests).
    today : datetime.date, optional
        The date naming the history copy, by default today in UTC.
    store : str, optional
        The file's name in the state store, by default ``scan.db``; its stem
        names the history copies and, for any other store, the upload route
        (``/-/scan/upload/<stem>``).

    Raises
    ------
    StoreError
        If the database is missing, the upload would shrink the store, or
        rclone fails to write ``scan.db``.
    """
    runner = runner or _run
    poster = poster or _post
    db_path = Path(db_path)
    if not db_path.is_file():
        raise StoreError(f"{db_path} does not exist: nothing to push")
    events = _count_events(db_path)
    _check_shrink(config, db_path, events, runner, store)
    day = (today or datetime.datetime.now(datetime.UTC).date()).strftime("%Y%m%d")
    with tempfile.TemporaryDirectory(prefix="auto-scan-push-") as tmp:
        snapshot = Path(tmp) / store
        _snapshot(db_path, snapshot)
        remote = _store_path(config, store)
        result = runner(["rclone", "copyto", str(snapshot), remote])
        if result.returncode != 0:
            raise StoreError(
                f"rclone copyto {remote} failed (exit {result.returncode}): {(result.stderr or '').strip()}"
            )
        # The day's first snapshot is kept: a later bad push the same day can't overwrite it.
        history = _store_path(config, f"history/{Path(store).stem}-{day}.db")
        result = runner(["rclone", "copyto", "--ignore-existing", str(snapshot), history])
        if result.returncode != 0:
            _warn(f"could not keep the daily copy {history}: {(result.stderr or '').strip()}")
        # The store now holds this many events: the baseline for a later push from this file.
        _set_meta(db_path, "pulled_events", str(events))
        print(f"auto-scan: stored {remote} ({events} events)", file=sys.stderr)
        if not datasette:
            return
        token = config.writer_token
        if not token:
            _warn("no AUTO_SCAN_WRITER_TOKEN (or ~/.config/auto-scan/writer-token): not uploading to the Datasette")
            return
        _upload_datasette(config, snapshot, token, poster, store)


__all__ = ["StoreError", "pull", "push"]
