"""The realtime workflow's uploaded results, read with rclone.

Every call goes through ``run_rclone`` so tests can replace it. rclone reads its
remotes from the environment or ``~/.config/rclone``.

Remote layout: ``<results_url>/<cycle>-<event_id>/`` holds the event's
realisation files and one directory per resolution. Only the files the scan
reads are downloaded; the multi-GB waveform files never are.
"""

import dataclasses
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auto_scan.config import Config

# <cycle>-<GeoNet public id>, e.g. 12-2026p866048. Older layouts
# (20261002T0000+1300-...), drafts and builds/ don't match.
EVENT_DIR = re.compile(r"^(?P<cycle>\d+)-(?P<event_id>\d{4}p\d+)$")

EVENT_FILES = ("realisation.json", "realisation.toml", "realisation.geojson")
# Relative to a run directory. intensity_measures.duckdb is the run's whole IM
# database: every site's simulated IMs and the empirical model's (`gmm`) records.
# simdb/<event>.duckdb is the same with the gmm records stripped, so it isn't fetched.
RUN_PATTERNS = ("*.json", "*.toml", "*.geojson", "logs.tar.gz", "simdb/*_waveforms.nc", "intensity_measures.duckdb")


@dataclass(frozen=True)
class EventDir:
    """One event's directory under ``results_url``.

    Attributes
    ----------
    name : str
        The directory name, ``<cycle>-<event_id>``.
    cycle : int
        The workflow's cycle point.
    event_id : str
        The GeoNet public id.
    written : str
        When its latest ``manifest.json`` was modified (rclone's ``ModTime``), or
        ``""`` if unknown.
    superseded : tuple of str
        The event's other directories: earlier simulations of the same event.
    """

    name: str
    cycle: int
    event_id: str
    written: str = ""
    superseded: tuple[str, ...] = ()


def run_rclone(args: list[str]) -> str:
    """Run rclone and return its stdout.

    Parameters
    ----------
    args : list of str
        The arguments after ``rclone``.

    Returns
    -------
    str
        The standard output of rclone.

    Raises
    ------
    RuntimeError
        When rclone exits non-zero; the message carries its stderr.
    """
    result = subprocess.run(["rclone", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"rclone {' '.join(args[:2])} failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result.stdout


def parse_event_dir(name: str) -> EventDir | None:
    """Parse ``<cycle>-<event_id>``.

    Parameters
    ----------
    name : str
        A directory name, with or without a trailing ``/``.

    Returns
    -------
    EventDir or None
        The parsed directory, or None for anything that is not an event's.
    """
    match = EVENT_DIR.match(name.rstrip("/"))
    if match is None:
        return None
    return EventDir(match.group(0), int(match.group("cycle")), match.group("event_id"))


def _utc(timestamp: str | None) -> str:
    """rclone's ``ModTime`` in UTC to the second, so that the strings sort by time; ``""`` if unreadable."""
    try:
        moment = dt.datetime.fromisoformat(timestamp or "")
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat(timespec="seconds")


def list_events(config: Config) -> list[EventDir]:
    """The event directories under ``results_url`` that hold a manifest, one per event.

    An event simulated more than once has a directory per cycle. The one whose
    manifest was written last is the event's current simulation; the others are
    named in its ``superseded``. Cycle numbers can't decide this: they start
    again from 1 when the workflow is reinstalled.

    Parameters
    ----------
    config : Config
        The scan's settings (``results_url``).

    Returns
    -------
    list of EventDir
        In the order they were simulated (by ``written``), so a scan ingests
        events in the order every history is read in.
    """
    listing = run_rclone(["lsjson", "-R", "--files-only", "--include", "/*/*/manifest.json", config.results_url])
    written: dict[str, str] = {}
    for entry in json.loads(listing):
        name = entry["Path"].split("/", 1)[0]
        written[name] = max(written.get(name, ""), _utc(entry.get("ModTime")))
    by_event: dict[str, list[EventDir]] = {}
    for name, modified in written.items():
        parsed = parse_event_dir(name)
        if parsed is not None:
            by_event.setdefault(parsed.event_id, []).append(dataclasses.replace(parsed, written=modified))
    current = []
    for directories in by_event.values():
        directories.sort(key=lambda event: (event.written, event.cycle))
        others = tuple(event.name for event in directories[:-1])
        current.append(dataclasses.replace(directories[-1], superseded=others))
    return sorted(current, key=lambda event: (event.written, event.cycle))


def list_files(config: Config, event_dir: str) -> list[dict[str, Any]]:
    """Every file under one event's directory, in one recursive listing.

    Parameters
    ----------
    config : Config
        The scan's settings (``results_url``).
    event_dir : str
        The event's directory name.

    Returns
    -------
    list of dict
        The ``lsjson`` entries of rclone for files (``Path`` relative to the event
        directory, ``Size``, ``ModTime``); directories are left out.
    """
    entries = json.loads(run_rclone(["lsjson", "-R", "--files-only", f"{config.results_url}/{event_dir}"]))
    return [entry for entry in entries if not entry.get("IsDir")]


def run_files(files: list[dict[str, Any]], resolution: str) -> list[dict[str, Any]]:
    """The entries of one run among an event's listing.

    Parameters
    ----------
    files : list of dict
        ``list_files``'s result.
    resolution : str
        The run's resolution.

    Returns
    -------
    list of dict
        The entries under ``<resolution>/``, with ``Path`` made relative to it.
    """
    prefix = f"{resolution}/"
    return [{**entry, "Path": entry["Path"][len(prefix) :]} for entry in files if entry["Path"].startswith(prefix)]


def copy(config: Config, event_dir: str, includes: list[str]) -> None:
    """Copy the files of an event matching ``includes`` into its cache directory.

    Parameters
    ----------
    config : Config
        The scan's settings (``results_url``, ``cache_dir``).
    event_dir : str
        The event's directory name.
    includes : list of str
        rclone filter patterns, rooted at the event directory (``/0_5hz/*.json``).
    """
    if not includes:
        return
    dest = config.event_cache(event_dir)
    dest.mkdir(parents=True, exist_ok=True)
    filters = [arg for pattern in includes for arg in ("--include", pattern)]
    run_rclone(["copy", f"{config.results_url}/{event_dir}", str(dest), *filters])


def manifest_includes(resolutions: list[str]) -> list[str]:
    """The filters that select the manifests of some runs.

    Parameters
    ----------
    resolutions : list of str
        The runs' resolutions.

    Returns
    -------
    list of str
        One rclone ``--include`` pattern per run.
    """
    return [f"/{resolution}/manifest.json" for resolution in resolutions]


def run_includes(resolutions: list[str]) -> list[str]:
    """The filters for everything the scan reads of an event and some of its runs.

    Parameters
    ----------
    resolutions : list of str
        The runs' resolutions.

    Returns
    -------
    list of str
        The rclone ``--include`` patterns: the event-level realisation files, and each
        run's small files plus its IM database and waveforms.
    """
    patterns = [f"/{name}" for name in EVENT_FILES]
    patterns += [f"/{resolution}/{pattern}" for resolution in resolutions for pattern in RUN_PATTERNS]
    return patterns


def unpack_logs(run_dir: Path) -> bool:
    """Unpack ``<run_dir>/logs.tar.gz`` into a fresh ``<run_dir>/logs/``.

    Parameters
    ----------
    run_dir : Path
        The run's cache directory.

    Returns
    -------
    bool
        True when the archive existed and unpacked.
    """
    archive = run_dir / "logs.tar.gz"
    if not archive.is_file():
        return False
    logs = run_dir / "logs"
    if logs.exists():
        shutil.rmtree(logs)
    logs.mkdir()
    try:
        with tarfile.open(archive) as tar:
            # The "data" filter refuses absolute paths, `..`, links out of logs/ and device files.
            tar.extractall(logs, filter="data")
    except (tarfile.TarError, OSError) as error:
        print(f"auto-scan: could not unpack {archive}: {error}", file=sys.stderr)
        return False
    return True


def download_runs(config: Config, event_dir: str, resolutions: list[str]) -> Path:
    """Download an event's realisation files and some of its runs, then unpack their logs.

    Parameters
    ----------
    config : Config
        The scan's settings.
    event_dir : str
        The event's directory name.
    resolutions : list of str
        The runs to download.

    Returns
    -------
    Path
        The event's cache directory, laid out as DESIGN.md describes.
    """
    copy(config, event_dir, run_includes(resolutions))
    cache = config.event_cache(event_dir)
    for resolution in resolutions:
        unpack_logs(cache / resolution)
    return cache
