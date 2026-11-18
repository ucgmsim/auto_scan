"""SeismicNow: the observed ground motions of an event, as NZGMDB-style flatfiles."""

import io
import shutil
import sys
import tempfile
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from auto_scan.config import Config
from auto_scan.sources import _http


def _safe_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """The archive's files with their relative paths; raises on any path that could escape."""
    members = []
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
            raise ValueError(f"unsafe path in flatfile zip: {info.filename!r}")
        if not info.is_dir():
            members.append((info, path))
    # A zip whose files all sit in one top-level folder is unpacked without it,
    # so observed/ always holds the CSVs directly.
    tops = {path.parts[0] for _, path in members if len(path.parts) > 1}
    if members and len(tops) == 1 and all(len(path.parts) > 1 for _, path in members):
        members = [(info, PurePosixPath(*path.parts[1:])) for info, path in members]
    return members


def unpack_zip(data: bytes, dest: Path) -> None:
    """Unpack a zip into a fresh ``dest``, rejecting absolute paths and ``..``.

    Parameters
    ----------
    data : bytes
        The zip.
    dest : Path
        The directory to create; an existing one is replaced only once the whole
        archive has unpacked.

    Raises
    ------
    ValueError
        When a member's path is absolute or climbs out of ``dest``.
    zipfile.BadZipFile
        When ``data`` is not a zip.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = _safe_members(archive)
        staging = Path(tempfile.mkdtemp(prefix=".observed-", dir=dest.parent))
        try:
            for info, path in members:
                target = staging.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
            if dest.exists():
                shutil.rmtree(dest)
            staging.rename(dest)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def download_flatfiles(config: Config, event_id: str, dest: Path) -> bool:
    """Download an event's observed flatfiles and unpack them into ``dest``.

    Parameters
    ----------
    config : Config
        The scan's settings (``seismicnow_api``, ``user_agent``).
    event_id : str
        The GeoNet event id.
    dest : Path
        The directory to unpack into (``<event_dir>/observed``). It is replaced
        when the download succeeds and left alone when it doesn't.

    Returns
    -------
    bool
        True when flatfiles were unpacked; False when SeismicNow has none for the
        event (404, or a body that is not a zip).
    """
    url = f"{config.seismicnow_api}/download/flatfiles?{urllib.parse.urlencode({'event_id': event_id})}"
    status, body = _http.get(url, config.user_agent, timeout=300)
    if status == 404:
        return False
    if status != 200:
        raise RuntimeError(f"SeismicNow flatfiles for {event_id}: HTTP {status}: {body[:300]!r}")
    if not zipfile.is_zipfile(io.BytesIO(body)):
        print(f"auto-scan: SeismicNow returned no zip for {event_id}: {body[:120]!r}", file=sys.stderr)
        return False
    unpack_zip(body, dest)
    return True
