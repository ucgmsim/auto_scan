"""The realtime workflow's uploaded results, read with rclone.

Every call goes through ``run_rclone`` so tests can replace it. rclone reads its
remotes from the environment or ``~/.config/rclone``.

Remote layout: ``<results_url>/<cycle>-<event_id>/`` holds the event's
realisation files and one directory per resolution. Only the files the scan
reads are downloaded; the multi-GB waveform files never are.
"""

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
    """

    name: str
    cycle: int
    event_id: str


def run_rclone(args: list[str]) -> str:
    """Run rclone and return its stdout.

    Parameters
    ----------
    args : list of str
        The arguments after ``rclone``.

    Returns
    -------
    str
        rclone's standard output.

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


def list_events(config: Config) -> list[EventDir]:
    """The event directories under ``results_url``, one per event.

    Parameters
    ----------
    config : Config
        The scan's settings (``results_url``).

    Returns
    -------
    list of EventDir
        By cycle. When an event was simulated in two cycles, only the highest is kept.
    """
    entries = json.loads(run_rclone(["lsjson", "--dirs-only", config.results_url]))
    latest: dict[str, EventDir] = {}
    for entry in entries:
        parsed = parse_event_dir(entry["Name"])
        if parsed is not None and (parsed.event_id not in latest or parsed.cycle > latest[parsed.event_id].cycle):
            latest[parsed.event_id] = parsed
    return sorted(latest.values(), key=lambda event: event.cycle)


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
        rclone's ``lsjson`` entries for files (``Path`` relative to the event
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
        rclone ``--include`` patterns: the event-level realisation files, and each
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
