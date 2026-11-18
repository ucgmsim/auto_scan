"""Every setting of the scan, each read from one environment variable.

Nothing else in the package hard-codes a URL, a remote or a token path: a move of
the Datasette to another host is one change, to ``AUTO_SCAN_DATASETTE_URL`` (the
repository variable ``DATASETTE_URL`` in GitHub Actions).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    """An environment variable, with empty treated as unset."""
    return os.environ.get(name) or default


def _token(env: str, file: str) -> str | None:
    """A token from an environment variable, else the first word of a file."""
    value = os.environ.get(env)
    if value:
        return value.strip()
    path = Path(_env(f"{env}_FILE", file)).expanduser()
    if path.is_file():
        return path.read_text().split()[0]
    return None


@dataclass(frozen=True)
class Config:
    """The scan's settings.

    Attributes
    ----------
    datasette_url : str
        The Datasette that hosts the scan's database and dashboard
        (``AUTO_SCAN_DATASETTE_URL``).
    monitor_url : str
        The Datasette that holds the Cylc monitor's database
        (``AUTO_SCAN_MONITOR_URL``; the same server unless set).
    monitor_database : str
        The monitor's database name there (``AUTO_SCAN_MONITOR_DATABASE``).
    scan_database : str
        The scan's database name there (``AUTO_SCAN_DATABASE``).
    workflow : str
        The Cylc workflow whose events are scanned (``AUTO_SCAN_WORKFLOW``).
    results_url : str
        The rclone path the realtime workflow uploads to (``AUTO_SCAN_RESULTS_URL``).
    state_url : str
        The rclone directory that keeps ``scan.db``, the store of record
        (``AUTO_SCAN_STATE_URL``). The Datasette only shows a copy.
    simdb_api : str
        SimDB's API root, the part before ``/api/...`` (``AUTO_SCAN_SIMDB_API``).
    seismicnow_api : str
        SeismicNow's API root (``AUTO_SCAN_SEISMICNOW_API``).
    cache_dir : Path
        Where an event's downloaded inputs are kept (``AUTO_SCAN_CACHE``).
    db_path : Path
        The local ``scan.db`` (``AUTO_SCAN_DB``).
    user_agent : str
        Sent with every HTTP request: Cloudflare in front of SimDB and SeismicNow
        refuses the default urllib one (error 1010).
    resolutions : tuple of str
        The resolutions looked for in each event's directory.
    """

    datasette_url: str = field(
        default_factory=lambda: _env("AUTO_SCAN_DATASETTE_URL", "https://cylc.lispandfound.xyz").rstrip("/")
    )
    monitor_url: str = field(
        default_factory=lambda: _env(
            "AUTO_SCAN_MONITOR_URL", _env("AUTO_SCAN_DATASETTE_URL", "https://cylc.lispandfound.xyz")
        ).rstrip("/")
    )
    monitor_database: str = field(default_factory=lambda: _env("AUTO_SCAN_MONITOR_DATABASE", "cylc"))
    scan_database: str = field(default_factory=lambda: _env("AUTO_SCAN_DATABASE", "scan"))
    workflow: str = field(default_factory=lambda: _env("AUTO_SCAN_WORKFLOW", "realtime/run1"))
    results_url: str = field(
        default_factory=lambda: _env("AUTO_SCAN_RESULTS_URL", "dropbox:/QuakeCoRE/Jake/realtime_simulations")
    )
    state_url: str = field(default_factory=lambda: _env("AUTO_SCAN_STATE_URL", "dropbox:/QuakeCoRE/Jake/realtime_scan"))
    simdb_api: str = field(
        default_factory=lambda: _env(
            "AUTO_SCAN_SIMDB_API", "https://quakecoresoft.canterbury.ac.nz/simulation_db/api"
        ).rstrip("/")
    )
    seismicnow_api: str = field(
        default_factory=lambda: _env(
            "AUTO_SCAN_SEISMICNOW_API", "https://quakecoresoft.canterbury.ac.nz/seismicnow/api"
        ).rstrip("/")
    )
    cache_dir: Path = field(default_factory=lambda: Path(_env("AUTO_SCAN_CACHE", "~/.cache/auto-scan")).expanduser())
    db_path: Path = field(default_factory=lambda: Path(_env("AUTO_SCAN_DB", "scan.db")).expanduser())
    user_agent: str = "auto-scan/0.1 (+https://github.com/ucgmsim/auto_scan)"
    resolutions: tuple[str, ...] = ("0_25hz", "0_5hz", "1hz")

    @property
    def monitor_reader_token(self) -> str | None:
        """The monitor's read-only token (``CYLC_MONITOR_READER_TOKEN`` or its file)."""
        return _token("CYLC_MONITOR_READER_TOKEN", "~/.config/cylc-monitor/reader-token")

    @property
    def writer_token(self) -> str | None:
        """The token that may upload ``scan.db`` (``AUTO_SCAN_WRITER_TOKEN`` or its file)."""
        return _token("AUTO_SCAN_WRITER_TOKEN", "~/.config/auto-scan/writer-token")

    def event_cache(self, event_dir: str) -> Path:
        """The cache directory of one event.

        Parameters
        ----------
        event_dir : str
            The event's directory name under ``results_url``, ``<cycle>-<event_id>``.

        Returns
        -------
        Path
            ``cache_dir/<cycle>-<event_id>``, laid out as DESIGN.md describes.
        """
        return self.cache_dir / event_dir
