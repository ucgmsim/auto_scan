"""Readers and statistics shared by the science extractors.

Nothing here registers an extractor: it reads an event's cached files (the
SeismicNow flatfile, the run's IM databases, SimDB's station-IMs JSON, the
realisation and the waveforms) into plain pandas/numpy structures, and holds the
robust statistics every science metric is built from.

Observed-record quality (NZGMDB conventions, as SeismicNow's flatfiles carry them):

- ``score_X``/``score_Y`` is the ground-motion classifier's probability that the
  horizontal component is a usable earthquake record; a record passes when both
  are at least ``MIN_SCORE``. A missing score (the classifier did not run, as for
  records it could not process) fails.
- ``multi_X``/``multi_Y`` is the classifier's probability that the record holds
  more than one earthquake; it must be below ``MAX_MULTI``.
- ``is_ground_level`` false (an instrument in a structure) fails.
- ``fmin_*``/``fmax_*`` bound the usable frequency band of each component. A
  pSA at period ``T`` is usable when ``1/fmax <= T <= 1/(1.25 fmin)``, with
  ``fmin`` the larger and ``fmax`` the smaller of the two horizontals (the 1.25
  factor is the usual NZGMDB/Bayless-Abrahamson margin above the high-pass
  corner). PGA and PGV need only the record to pass.
"""

import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

# The classifier's score is a probability of a good record; 0.5 is NZGMDB's own cut.
MIN_SCORE = 0.5
# NZGMDB drops records the classifier thinks hold several earthquakes (multi >= 0.2).
MAX_MULTI = 0.2
# Usable long-period limit is 1 / (FMIN_MARGIN * fmin) (NZGMDB convention).
FMIN_MARGIN = 1.25

# The periods kept in station_residuals: a log-spaced subset of the 111 the
# flatfile and the IM databases share, enough to resolve LF, HF and crossover.
RESIDUAL_PERIODS: tuple[float, ...] = (
    0.01,
    0.02,
    0.05,
    0.1,
    0.2,
    0.3,
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    5.0,
    7.5,
    10.0,
)
# The crossover frequency for each resolution when the realisation does not give bb.flo.
DEFAULT_FLO = {"0_25hz": 0.25, "0_5hz": 0.5, "1hz": 1.0}
# Resolution order, coarsest first (also the LF maximum frequency in Hz).
RESOLUTION_HZ = {"0_25hz": 0.25, "0_5hz": 0.5, "1hz": 1.0}

MAD_SCALE = 1.4826  # MAD -> sigma for a normal distribution


def warn(message: str) -> None:
    """Print a one-line warning on stderr.

    Parameters
    ----------
    message : str
        The warning.
    """
    print(f"auto_scan.science: {message}", file=sys.stderr)


def im_name(period: float) -> str:
    """The IM name of a pSA period, as the flatfile spells it.

    Parameters
    ----------
    period : float
        The period in seconds.

    Returns
    -------
    str
        ``pSA_<period>``, e.g. ``pSA_0.5``, ``pSA_1.0``, ``pSA_10.0``.
    """
    return f"pSA_{float(period)}"


def im_period(im: str) -> float | None:
    """The period of a pSA IM name.

    Parameters
    ----------
    im : str
        An IM name.

    Returns
    -------
    float or None
        The period in seconds, or None for a non-pSA IM.
    """
    if im.startswith("pSA_"):
        try:
            return float(im[4:])
        except ValueError:
            return None
    return None


def im_band(im: str, flo: float) -> str:
    """Which half of the broadband an IM is dominated by.

    Parameters
    ----------
    im : str
        The IM name.
    flo : float
        The LF/HF crossover frequency (``bb.flo``) in Hz.

    Returns
    -------
    str
        ``lf`` for pSA with ``T >= 1.5 / flo`` (deterministic SW4 part),
        ``hf`` for PGA and pSA with ``T <= 0.5 / flo`` (stochastic part),
        ``mixed`` otherwise (PGV and periods near the crossover).
    """
    if im == "PGA":
        return "hf"
    period = im_period(im)
    if period is None:
        return "mixed"
    if period >= 1.5 / flo:
        return "lf"
    if period <= 0.5 / flo:
        return "hf"
    return "mixed"


def jsonable(value: Any) -> Any:
    """A copy of a value that ``json.dumps`` accepts: numpy scalars as Python ones, NaN as None.

    Parameters
    ----------
    value : Any
        Nested dicts, lists, tuples and scalars.

    Returns
    -------
    Any
        The converted value.
    """
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    if value is None or isinstance(value, str):
        return value
    return str(value)


# --------------------------------------------------------------------------- statistics


@dataclass
class Summary:
    """Robust and plain statistics of a sample of ln residuals.

    Attributes
    ----------
    n : int
        The sample size.
    mean, median, mad_sigma, std : float
        Mean, median, ``1.4826 * MAD`` and the sample standard deviation (NaN
        when undefined).
    frac_gt_ln10 : float
        The fraction with ``|x| > ln 10``.
    """

    n: int
    mean: float
    median: float
    mad_sigma: float
    std: float
    frac_gt_ln10: float


def summarise(values: Iterable[float]) -> Summary:
    """Summarise a sample, ignoring non-finite values.

    Parameters
    ----------
    values : iterable of float
        The sample.

    Returns
    -------
    Summary
        Its statistics; NaN where the sample is too small.
    """
    x = np.asarray(list(values), dtype=float)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n == 0:
        nan = float("nan")
        return Summary(0, nan, nan, nan, nan, nan)
    median = float(np.median(x))
    return Summary(
        n=n,
        mean=float(np.mean(x)),
        median=median,
        mad_sigma=float(MAD_SCALE * np.median(np.abs(x - median))) if n >= 2 else float("nan"),
        std=float(np.std(x, ddof=1)) if n >= 2 else float("nan"),
        frac_gt_ln10=float(np.mean(np.abs(x) > math.log(10))),
    )


def ols_slope(x: Iterable[float], y: Iterable[float], min_n: int = 5) -> tuple[float, float, float, float]:
    """Least-squares line ``y = a + b x`` and the standard errors of ``b`` and ``a``.

    Parameters
    ----------
    x, y : iterable of float
        The paired sample; pairs with a non-finite member are dropped.
    min_n : int, optional
        Fewer pairs than this (or no spread in ``x``) gives NaN.

    Returns
    -------
    tuple of float
        ``(b, se_b, a, se_a)``.
    """
    xa = np.asarray(list(x), dtype=float)
    ya = np.asarray(list(y), dtype=float)
    keep = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[keep], ya[keep]
    nan = float("nan")
    n = xa.size
    if n < min_n:
        return nan, nan, nan, nan
    xbar = xa.mean()
    sxx = float(np.sum((xa - xbar) ** 2))
    if sxx <= 0:
        return nan, nan, nan, nan
    b = float(np.sum((xa - xbar) * (ya - ya.mean())) / sxx)
    a = float(ya.mean() - b * xbar)
    s2 = float(np.sum((ya - a - b * xa) ** 2) / (n - 2))
    se_b = math.sqrt(s2 / sxx)
    se_a = math.sqrt(s2 * (1.0 / n + xbar**2 / sxx))
    return b, se_b, a, se_a


def robust_centre(values: Sequence[float]) -> float:
    """The median of the finite values, NaN if none.

    Parameters
    ----------
    values : sequence of float
        The values.

    Returns
    -------
    float
        Their median.
    """
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    return float(np.median(x)) if x.size else float("nan")


# --------------------------------------------------------------------------- geometry


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> Any:
    """Great-circle distance in km (vectorised).

    Parameters
    ----------
    lat1, lon1, lat2, lon2 : float or array
        Coordinates in degrees.

    Returns
    -------
    float or array
        The distance in km.
    """
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _local_xy(lat: Any, lon: Any, lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    """Equirectangular km coordinates about (lat0, lon0); good to <1% over a few hundred km."""
    x = (np.asarray(lon, dtype=float) - lon0) * 111.32 * math.cos(math.radians(lat0))
    y = (np.asarray(lat, dtype=float) - lat0) * 110.57
    return x, y


def polygon_edge_distance(lat: Any, lon: Any, polygon: Sequence[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    """Whether points lie inside a small lat/lon polygon, and their distance to its edge.

    Parameters
    ----------
    lat, lon : array
        The points, degrees.
    polygon : sequence of (lat, lon)
        The polygon's vertices in order (not repeated at the end).

    Returns
    -------
    inside : numpy.ndarray of bool
        Whether each point is inside.
    distance_km : numpy.ndarray
        Its distance to the nearest edge, km.
    """
    lat0 = float(np.mean([p[0] for p in polygon]))
    lon0 = float(np.mean([p[1] for p in polygon]))
    px, py = _local_xy([p[0] for p in polygon], [p[1] for p in polygon], lat0, lon0)
    x, y = _local_xy(lat, lon, lat0, lon0)
    x, y = np.atleast_1d(x), np.atleast_1d(y)
    inside = np.zeros(x.shape, dtype=bool)
    distance = np.full(x.shape, np.inf)
    k = len(px)
    for i in range(k):
        ax, ay, bx, by = px[i], py[i], px[(i + 1) % k], py[(i + 1) % k]
        crosses = ((ay > y) != (by > y)) & (x < (bx - ax) * (y - ay) / np.where(by != ay, by - ay, 1e-12) + ax)
        inside ^= crosses
        length2 = (bx - ax) ** 2 + (by - ay) ** 2
        t = np.clip(((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / max(length2, 1e-12), 0, 1)
        distance = np.minimum(distance, np.hypot(x - ax - t * (bx - ax), y - ay - t * (by - ay)))
    return inside, distance


# --------------------------------------------------------------------------- files


def read_json(path: Path) -> Any:
    """A JSON file, or None (with a warning) if it is missing or malformed.

    Parameters
    ----------
    path : Path
        The file.

    Returns
    -------
    Any
        The parsed value, or None.
    """
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        warn(f"{path}: unreadable JSON ({error})")
        return None


def read_csv(path: Path) -> pd.DataFrame | None:
    """A CSV file, or None (with a warning if it exists but is malformed).

    Parameters
    ----------
    path : Path
        The file.

    Returns
    -------
    pandas.DataFrame or None
        The table.
    """
    if not path.is_file():
        return None
    try:
        return pd.read_csv(path)
    except (OSError, ValueError) as error:
        warn(f"{path}: unreadable CSV ({error})")
        return None


def realisation(run_or_event_dir: Path) -> dict[str, Any]:
    """The realisation of a run (or of the event), an empty dict if absent.

    Parameters
    ----------
    run_or_event_dir : Path
        A run directory or the event directory.

    Returns
    -------
    dict
        The parsed ``realisation.json``.
    """
    value = read_json(run_or_event_dir / "realisation.json")
    return value if isinstance(value, dict) else {}


def crossover_frequency(run_dir: Path, resolution: str) -> tuple[float | None, str]:
    """The LF/HF crossover frequency of a run.

    Parameters
    ----------
    run_dir : Path
        The run's directory.
    resolution : str
        Its resolution (for the fallback).

    Returns
    -------
    tuple of (float or None, str)
        ``bb.flo`` from the run's realisation, else the resolution's frequency;
        and where it came from (``realisation`` or ``default``).
    """
    flo = realisation(run_dir).get("bb", {}).get("flo")
    if isinstance(flo, (int, float)) and flo > 0:
        return float(flo), "realisation"
    default = DEFAULT_FLO.get(resolution)
    return default, "default"


def domain_polygon(run_dir: Path) -> list[tuple[float, float]] | None:
    """The run's simulation domain as (lat, lon) corners, from its realisation.

    Parameters
    ----------
    run_dir : Path
        The run's directory.

    Returns
    -------
    list of (float, float) or None
        The corners, None if the realisation does not give them.
    """
    corners = realisation(run_dir).get("domain", {}).get("domain")
    if not isinstance(corners, list) or len(corners) < 3:
        return None
    try:
        return [(float(c["latitude"]), float(c["longitude"])) for c in corners]
    except (KeyError, TypeError, ValueError):
        return None


def supergrid_width_km(run_dir: Path) -> float | None:
    """The width of SW4's absorbing layer, from the run's ``supergrid`` command.

    Parameters
    ----------
    run_dir : Path
        The run's directory.

    Returns
    -------
    float or None
        The width in km, None if the realisation does not give it in metres.
    """
    commands = realisation(run_dir).get("sw4", {}).get("commands", [])
    for command in commands if isinstance(commands, list) else []:
        if isinstance(command, dict) and command.get("name") == "supergrid":
            width = (command.get("parameters") or {}).get("width")
            if isinstance(width, (int, float)):
                return float(width) / 1000.0
    return None


def simulated_hypocentre(event_or_run_dir: Path) -> tuple[float, float, float] | None:
    """The simulated hypocentre from a realisation's single-plane source.

    The hypocentre is at fault coordinates ``(s, d)`` on the plane whose corners
    are, in order, top-left, top-right, bottom-right, bottom-left
    (source_modelling's ``Plane``): ``c0 + s (c1 - c0) + d (c3 - c0)``.

    Parameters
    ----------
    event_or_run_dir : Path
        A directory holding ``realisation.json``.

    Returns
    -------
    tuple of float or None
        ``(lat, lon, depth_km)``, None for a missing realisation or a
        multi-plane source (use the IM database's ``realisations`` instead).
    """
    rel = realisation(event_or_run_dir)
    geometries = rel.get("sources", {}).get("source_geometries", {})
    hypocentre = rel.get("rupture_propagation", {}).get("hypocentre") or {}
    if not isinstance(geometries, dict) or len(geometries) != 1:
        return None
    corners = next(iter(geometries.values())).get("corners")
    if not isinstance(corners, list) or len(corners) != 4:
        return None
    try:
        c = np.array([[p["latitude"], p["longitude"], p["depth"] / 1000.0] for p in corners], dtype=float)
        s, d = float(hypocentre.get("s", 0.5)), float(hypocentre.get("d", 0.5))
    except (KeyError, TypeError, ValueError):
        return None
    point = c[0] + s * (c[1] - c[0]) + d * (c[3] - c[0])
    return float(point[0]), float(point[1]), float(point[2])


def simulated_magnitude(event_or_run_dir: Path) -> float | None:
    """The total moment magnitude a realisation simulates.

    Parameters
    ----------
    event_or_run_dir : Path
        A directory holding ``realisation.json``.

    Returns
    -------
    float or None
        ``Mw`` of the summed moment of every fault, None if absent.
    """
    magnitudes = realisation(event_or_run_dir).get("magnitudes", {}).get("magnitudes")
    if not isinstance(magnitudes, dict) or not magnitudes:
        return None
    try:
        moment = sum(10 ** (1.5 * float(m) + 9.1) for m in magnitudes.values())
    except (TypeError, ValueError):
        return None
    return (math.log10(moment) - 9.1) / 1.5


# --------------------------------------------------------------------------- observed


@dataclass
class Observed:
    """One observed record per station, with its quality.

    Attributes
    ----------
    table : pandas.DataFrame
        Indexed by station: ``record_id, chan, loc, r_rup, Vs30, sta_lat,
        sta_lon, record_ok, fmin_h, fmax_h`` and every rotd50 IM column.
    n_records : int
        The rotd50 records in the flatfile.
    """

    table: pd.DataFrame
    n_records: int

    def usable(self, station: str, im: str) -> bool:
        """Whether an IM of a station's chosen record is usable.

        Parameters
        ----------
        station : str
            The station code.
        im : str
            The IM name.

        Returns
        -------
        bool
            The record passes and the IM's period lies in its usable band.
        """
        row = self.table.loc[station]
        if not bool(row["record_ok"]):
            return False
        period = im_period(im)
        if period is None:
            return True
        fmin, fmax = float(row["fmin_h"]), float(row["fmax_h"])
        if math.isfinite(fmin) and fmin > 0 and period > 1.0 / (FMIN_MARGIN * fmin):
            return False
        return not (math.isfinite(fmax) and fmax > 0 and period < 1.0 / fmax)


def _num(frame: pd.DataFrame, column: str) -> pd.Series:
    """A numeric column, all-NaN if absent."""
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index)


def read_observed(observed_dir: Path) -> Observed | None:
    """The rotd50 flatfile, reduced to one record per station.

    The record kept per station is the first of: passing records before failing
    ones, strong-motion (``HN``) before others, then by location code and record
    id, so the choice is deterministic.

    Parameters
    ----------
    observed_dir : Path
        The unpacked SeismicNow flatfile directory.

    Returns
    -------
    Observed or None
        The chosen records, None if the rotd50 table is missing or malformed.
    """
    frame = read_csv(observed_dir / "ground_motion_im_table_rotd50_flat.csv")
    if frame is None:
        return None
    if "sta" not in frame.columns or "PGA" not in frame.columns:
        warn(f"{observed_dir}: rotd50 flatfile has no sta/PGA columns")
        return None
    n_records = len(frame)
    score = np.fmin(_num(frame, "score_X"), _num(frame, "score_Y"))
    multi = np.fmax(_num(frame, "multi_X"), _num(frame, "multi_Y"))
    ground = frame["is_ground_level"] if "is_ground_level" in frame.columns else pd.Series(True, index=frame.index)
    ground = ~ground.astype(str).str.lower().isin(["false", "0"])
    frame = frame.copy().assign(
        record_ok=(score >= MIN_SCORE) & ~(multi >= MAX_MULTI) & ground & (_num(frame, "PGA") > 0),
        fmin_h=np.fmax(_num(frame, "fmin_X"), _num(frame, "fmin_Y")),
        fmax_h=np.fmin(_num(frame, "fmax_X"), _num(frame, "fmax_Y")),
        _is_hn=frame["chan"].astype(str).eq("HN") if "chan" in frame.columns else False,
        sta=frame["sta"].astype(str),
    )
    sort_cols = ["sta", "record_ok", "_is_hn"]
    ascending = [True, False, False]
    for column in ("loc", "record_id"):
        if column in frame.columns:
            sort_cols.append(column)
            ascending.append(True)
    frame = frame.sort_values(sort_cols, ascending=ascending, kind="stable")
    table = frame.drop_duplicates("sta", keep="first").set_index("sta").drop(columns="_is_hn")
    return Observed(table=table, n_records=n_records)


def observed_phase_times(observed_dir: Path, phase: str, min_prob: float = 0.5) -> pd.Series:
    """Observed phase arrival times after origin, per station.

    Parameters
    ----------
    observed_dir : Path
        The unpacked flatfile directory.
    phase : {"p", "s"}
        The phase.
    min_prob : float, optional
        The picker's minimum probability.

    Returns
    -------
    pandas.Series
        Seconds after the origin time, by station (median over its records);
        empty if the table is missing.
    """
    frame = read_csv(observed_dir / "phase_arrival_table.csv")
    columns = {f"{phase}_wave_datetime", f"{phase}_wave_prob", "evid_datetime", "record_id"}
    if frame is None or not columns <= set(frame.columns):
        return pd.Series(dtype=float)
    arrival = pd.to_datetime(frame[f"{phase}_wave_datetime"], errors="coerce", utc=True)
    origin = pd.to_datetime(frame["evid_datetime"], errors="coerce", utc=True)
    frame = frame.assign(
        t=(arrival - origin).dt.total_seconds(),
        sta=frame["record_id"].astype(str).str.split("_").str[1],
        prob=pd.to_numeric(frame[f"{phase}_wave_prob"], errors="coerce"),
    )
    frame = frame[(frame["prob"] >= min_prob) & frame["t"].notna()]
    return frame.groupby("sta")["t"].median()


# --------------------------------------------------------------------------- simulated


@dataclass
class SimIMs:
    """Rotd50 IMs of every site of a run.

    Attributes
    ----------
    sim : pandas.DataFrame
        Indexed by site: ``lat, lon, vs30, rrup, is_real`` and the IM columns.
    gmm : pandas.DataFrame
        The same for the empirical model's median, empty when the run's files
        hold none.
    source : str
        Where ``sim`` came from: a database path, or ``simdb_station_ims.json``.
    gmm_source : str
        Where ``gmm`` came from (``gmm_key`` and path), empty if none.
    hypocentre : tuple of float or None
        ``(lat, lon, depth_km)`` from the database's realisation, if any.
    magnitude : float or None
        The database's realisation magnitude, if any.
    """

    sim: pd.DataFrame
    gmm: pd.DataFrame
    source: str
    gmm_source: str = ""
    hypocentre: tuple[float, float, float] | None = None
    magnitude: float | None = None


def _ims_query(periods: Sequence[float], period_index: dict[float, int], kind: str, real_only: bool) -> str:
    """SQL for rotd50 IMs of one record kind."""
    psa = ", ".join(f'p.pSA[{period_index[p]}] AS "{im_name(p)}"' for p in periods if p in period_index)
    where = f"r.component = 'rotd50' AND r.kind = '{kind}'"
    if real_only:
        where += " AND s.is_real"
    return f"""
        SELECT s.site_id, s.lat, s.lon, s.vs30, s.is_real, se.rrup,
               r.gmm_key, sc.PGA, sc.PGV{", " + psa if psa else ""}
        FROM records r
        JOIN sites s ON s.site_int_id = r.site_int_id
        LEFT JOIN site_event se ON se.site_int_id = r.site_int_id AND se.event_int_id = r.event_int_id
        LEFT JOIN scalars_ims sc ON sc.record_int_id = r.record_int_id
        LEFT JOIN psa_ims p ON p.record_int_id = r.record_int_id
        LEFT JOIN events e ON e.event_int_id = r.event_int_id
        WHERE {where} AND (e.event_id = $event OR NOT EXISTS (SELECT 1 FROM events WHERE event_id = $event))
    """


def _read_imdb(path: Path, event_id: str, periods: Sequence[float], real_only: bool) -> dict[str, Any] | None:
    """Simulated and GMM rotd50 IMs from one IM database (read-only)."""
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as error:
        warn(f"{path}: cannot open ({error})")
        return None
    try:
        columns = {row[0] for row in con.execute("DESCRIBE sites").fetchall()}
        has_real = "is_real" in columns
        period_index = {
            round(float(p), 6): int(i) for i, p in con.execute("SELECT period_index, period FROM periods").fetchall()
        }
        wanted = [p for p in periods if round(float(p), 6) in period_index]
        index = {p: period_index[round(float(p), 6)] for p in wanted}
        out: dict[str, Any] = {}
        for kind in ("simulated", "gmm"):
            sql = _ims_query(wanted, index, kind, real_only and has_real)
            if not has_real:
                sql = sql.replace("s.is_real,", "TRUE AS is_real,")
            out[kind] = con.execute(sql, {"event": event_id}).df()
        rel = con.execute(
            "SELECT r.hypo_lat, r.hypo_lon, r.hypo_depth, r.magnitude FROM realisations r"
            " JOIN events e USING (event_int_id) ORDER BY (e.event_id = $event) DESC LIMIT 1",
            {"event": event_id},
        ).fetchone()
        out["realisation"] = rel
        return out
    except duckdb.Error as error:
        warn(f"{path}: unreadable IM database ({error})")
        return None
    finally:
        con.close()


def _frame(records: pd.DataFrame) -> pd.DataFrame:
    """Index an IM query result by site, numeric columns as float."""
    frame = records.drop(columns=["gmm_key"]).drop_duplicates("site_id").set_index("site_id")
    for column in frame.columns:
        if column != "is_real":
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame["is_real"] = frame["is_real"].astype(bool)
    return frame


def imdb_paths(run_dir: Path, event_id: str) -> list[Path]:
    """The run's IM databases present in the cache, SimDB's first.

    Parameters
    ----------
    run_dir : Path
        The run's directory.
    event_id : str
        The event.

    Returns
    -------
    list of Path
        ``simdb/<event>.duckdb`` (simulated only) and
        ``intensity_measures.duckdb`` (simulated and the GMM), those that exist.
    """
    candidates = [run_dir / "simdb" / f"{event_id}.duckdb", run_dir / "intensity_measures.duckdb"]
    return [path for path in candidates if path.is_file()]


def read_sim_ims(
    run_dir: Path,
    event_id: str,
    periods: Sequence[float] = RESIDUAL_PERIODS,
    real_only: bool = True,
    json_fallback: bool = False,
) -> SimIMs | None:
    """The run's rotd50 IMs (and the GMM's) at its sites.

    Parameters
    ----------
    run_dir : Path
        The run's directory.
    event_id : str
        The event.
    periods : sequence of float, optional
        The pSA periods wanted.
    real_only : bool, optional
        Only real (station) sites.
    json_fallback : bool, optional
        When no IM database is cached, read ``simdb_station_ims.json`` from the
        event directory (SimDB holds only the finest resolution ingested, so
        the caller decides whether that applies to this run).

    Returns
    -------
    SimIMs or None
        None when nothing is readable.
    """
    sim: pd.DataFrame | None = None
    gmm = pd.DataFrame()
    source, gmm_source = "", ""
    hypocentre: tuple[float, float, float] | None = None
    magnitude: float | None = None
    for path in imdb_paths(run_dir, event_id):
        result = _read_imdb(path, event_id, periods, real_only)
        if result is None:
            continue
        if sim is None and len(result["simulated"]):
            sim, source = _frame(result["simulated"]), str(path)
        if gmm.empty and len(result["gmm"]):
            keys = sorted(set(result["gmm"]["gmm_key"].dropna().astype(str)))
            first = result["gmm"]
            if keys:
                first = first[first["gmm_key"] == keys[0]]
            gmm, gmm_source = _frame(first), f"{keys[0] if keys else '?'} from {path}"
        rel = result["realisation"]
        if hypocentre is None and rel is not None and rel[0] is not None:
            hypocentre = (float(rel[0]), float(rel[1]), float(rel[2]))
            magnitude = None if rel[3] is None else float(rel[3])
    if sim is None and json_fallback:
        sim = read_simdb_json(run_dir.parent / "simdb_station_ims.json", periods)
        source = "simdb_station_ims.json" if sim is not None else ""
    if sim is None:
        return None
    return SimIMs(sim=sim, gmm=gmm, source=source, gmm_source=gmm_source, hypocentre=hypocentre, magnitude=magnitude)


def read_simdb_json(path: Path, periods: Sequence[float] = RESIDUAL_PERIODS) -> pd.DataFrame | None:
    """SimDB's station-IMs response as a site table.

    Parameters
    ----------
    path : Path
        ``simdb_station_ims.json``.
    periods : sequence of float, optional
        The pSA periods wanted (only those SimDB holds are returned).

    Returns
    -------
    pandas.DataFrame or None
        Indexed by site, as ``SimIMs.sim``.
    """
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("sites"), list):
        return None
    json_periods = [float(p) for p in data.get("periods", [])]
    wanted = {round(p, 6) for p in periods}
    rows = []
    for site in data["sites"]:
        try:
            ims = site["ims"]["rotd50"]
            row: dict[str, Any] = {
                "site_id": str(site["site_id"]),
                "lat": site.get("lat"),
                "lon": site.get("lon"),
                "vs30": site.get("vs30"),
                "is_real": True,
                "rrup": site.get("rrup"),
                "PGA": ims.get("PGA"),
                "PGV": ims.get("PGV"),
            }
            for period, value in zip(json_periods, ims.get("pSA") or [], strict=False):
                if round(period, 6) in wanted:
                    row[im_name(period)] = value
            rows.append(row)
        except (KeyError, TypeError):
            continue
    if not rows:
        return None
    frame = pd.DataFrame(rows).set_index("site_id")
    for column in frame.columns:
        if column != "is_real":
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    return frame


def finest_resolution(event_dir: Path) -> str | None:
    """The finest resolution with a run directory in the event's cache.

    Parameters
    ----------
    event_dir : Path
        The event's cache directory.

    Returns
    -------
    str or None
        The resolution, e.g. ``1hz``; None if there is no run directory.
    """
    present = [r for r in RESOLUTION_HZ if (event_dir / r).is_dir()]
    return max(present, key=RESOLUTION_HZ.__getitem__) if present else None


# --------------------------------------------------------------------------- waveforms


@dataclass
class Waveforms:
    """A run's broadband waveforms at real stations.

    Attributes
    ----------
    stations : list of str
        Station codes.
    components : list of str
        Component names, e.g. ``000, 090, ver``.
    acc : numpy.ndarray
        ``(station, component, time)`` acceleration in g, float64.
    dt : float
        The time step, s.
    path : str
        The file read.
    """

    stations: list[str]
    components: list[str]
    acc: np.ndarray
    dt: float
    path: str

    def component(self, name: str) -> int | None:
        """The index of a component, None if absent.

        Parameters
        ----------
        name : str
            The component.

        Returns
        -------
        int or None
            Its index.
        """
        return self.components.index(name) if name in self.components else None


def read_waveforms(run_dir: Path, event_id: str) -> Waveforms | None:
    """The run's ``simdb/<event>_waveforms.nc``.

    Parameters
    ----------
    run_dir : Path
        The run's directory.
    event_id : str
        The event (the file's group).

    Returns
    -------
    Waveforms or None
        None if the file is missing or malformed.
    """
    import xarray as xr

    path = run_dir / "simdb" / f"{event_id}_waveforms.nc"
    if not path.is_file():
        return None
    try:
        with xr.open_dataset(path, group=event_id, engine="h5netcdf") as ds:
            acc = ds["acc"].transpose("station", "component", "time")
            data = np.asarray(acc.values, dtype=float)
            stations = [str(s) for s in ds["station"].values]
            components = [str(c) for c in ds["component"].values]
            dt = float(ds.attrs.get("dt") or (ds["time"].values[1] - ds["time"].values[0]))
    except (OSError, KeyError, ValueError, IndexError) as error:
        warn(f"{path}: unreadable waveforms ({error})")
        return None
    if not (dt > 0) or data.ndim != 3:
        warn(f"{path}: bad waveform shape or dt")
        return None
    return Waveforms(stations=stations, components=components, acc=data, dt=dt, path=str(path))
