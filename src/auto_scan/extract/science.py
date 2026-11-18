"""Science extractors: residuals against observations and an empirical model,
cross-resolution consistency, and the simulated source against GeoNet's.

Every metric's resolution is the run's, except the ``@event_extractor`` ones
(``science.xres.*``, ``science.source.*``, ``science.coverage.*``) whose
resolution is ``""``. ``<IM>`` is ``PGA``, ``PGV`` or ``pSA_<T>`` with ``T`` from
``science_io.RESIDUAL_PERIODS`` (as the flatfile spells it: ``pSA_0.5``,
``pSA_10.0``). ``<cmp>`` is one of three comparisons, each over the same
quality-ok (station, IM) pairs of real stations joined with observations:

- ``residual``: ``ln(observed / simulated)``
- ``gmm_residual``: ``ln(gmm / simulated)``, the empirical model's median (the
  NSHM2022 logic-tree ``gmm`` records im-calc writes to the run's
  ``intensity_measures.duckdb``) against the simulation
- ``obs_gmm_residual``: ``ln(observed / gmm)``, how odd the event itself is

Per run (``@extractor residuals``):

==================================================  =====  ==========================================================
metric                                              unit   meaning
==================================================  =====  ==========================================================
science.bb.flo                                      Hz     LF/HF crossover (``bb.flo`` of the run's realisation)
science.residual.n_stations                         1      real stations joined with an observed record; detail: the
                                                           sim/GMM sources, observed counts, rejected stations
science.<cmp>.n.<IM>                                1      quality-ok stations with a value for the IM
science.<cmp>.mean.<IM>                             ln     mean (the event term)
science.<cmp>.median.<IM>                           ln     median (robust event term)
science.<cmp>.mad_sigma.<IM>                        ln     1.4826 * median absolute deviation
science.<cmp>.std.<IM>                              ln     sample standard deviation
science.<cmp>.frac_gt_ln10.<IM>                     1      fraction of stations with ``|x| > ln 10``
science.<cmp>.slope_lnrrup.<IM>                     ln     OLS slope against ``ln max(rrup, 1 km)`` (residual and
                                                           gmm_residual only)
science.<cmp>.slope_lnrrup_se.<IM>                  ln     its standard error
science.<cmp>.slope_lnvs30.<IM>                     ln     OLS slope against ``ln vs30`` (the vs30 the simulation used)
science.<cmp>.slope_lnvs30_se.<IM>                  ln     its standard error
science.<cmp>.band_median.lf / .hf                  ln     median of the per-IM medians (n >= 3) of the IMs in the LF
                                                           band (pSA, ``T >= 1.5/flo``) or HF band (PGA, pSA
                                                           ``T <= 0.5/flo``); detail: the per-IM medians
science.gmm_all.n.<IM>                              1      every site of the run (real and virtual) with both values
science.gmm_all.median.<IM>                         ln     median ``ln(gmm / simulated)`` over every site
science.gmm_all.mad_sigma.<IM>                      ln     its robust spread
science.gmm_all.slope_lnrrup.<IM>                   ln     slope against ``ln max(rrup, 1 km)`` over every site
science.gmm_all.slope_lnrrup_se.<IM>                ln     its standard error
science.gmm_all.band_median.lf / .hf                ln     as for ``<cmp>``
==================================================  =====  ==========================================================

Per event (``@event_extractor cross_resolution``), for each pair ``<pair>`` =
``<coarse>-<fine>`` of resolutions with an IM database (or, if the collector has
already deleted it, the ``science_long_period_ims.csv`` the residuals extractor
leaves in the run's cache directory), over the sites both runs hold, inside both domains and at least the absorbing-layer width plus
``EDGE_MARGIN_KM`` from either edge, at long periods only
(``T >= 2 / f_coarse``, where both runs are deterministic SW4 output):

==================================================  =====  ==========================================================
science.xres.<pair>.n                               1      sites compared; detail: the sites excluded near an edge
science.xres.<pair>.mean.<IM>                       ln     mean ``ln(IM_fine / IM_coarse)``
science.xres.<pair>.median.<IM>                     ln     its median
science.xres.<pair>.mad_sigma.<IM>                  ln     its robust spread
science.xres.<pair>.median                          ln     median of the per-IM medians; detail: the periods
science.xres.<pair>.mad_sigma                       ln     median of the per-IM robust spreads
==================================================  =====  ==========================================================

Per event (``@event_extractor source_and_coverage``):

==================================================  =====  ==========================================================
science.source.hypo_offset_km                       km     epicentral distance from GeoNet's hypocentre (flatfile
                                                           ``earthquake_source_table.csv``) to the simulated one
science.source.depth_diff_km                        km     simulated minus GeoNet hypocentre depth
science.source.mag_sim                              Mw     the event realisation's moment magnitude
science.source.mag_spread                           Mw     max - min of the magnitude across the event realisation,
                                                           each run's realisation, the IM databases and
                                                           ``events.magnitude`` (should be 0)
science.source.mag_obs                              mag    the flatfile's magnitude; detail: its type (often ML)
science.source.mag_diff_obs                         Mw     simulated minus GeoNet magnitude, only when GeoNet's is
                                                           a moment magnitude (NULL for ML: not comparable)
science.coverage.n_observed                         1      stations with an observed rotd50 record
science.coverage.n_observed_good                    1      ... whose chosen record passes the quality filter
science.coverage.n_well_recorded                    1      ... good and PGA >= ``WELL_RECORDED_PGA_G``
science.coverage.n_sim_real                         1      real stations simulated by any run
science.coverage.n_joined                           1      observed stations also simulated
science.coverage.n_good_outside                     1      well-recorded stations outside every run's domain
science.coverage.frac_good_outside                  1      ... as a fraction of the well-recorded ones
science.coverage.n_good_inside_unsimulated          1      well-recorded stations inside a run's domain (clear of
                                                           its absorbing layer) that no run simulated
==================================================  =====  ==========================================================

Waveform metrics (``science.wave.*``, ``science.crossover.*`` and the waveform
part of ``science.xres.*``) are in ``science_waveforms``.
"""

import math
import sqlite3
from itertools import combinations
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from auto_scan.db import set_metric
from auto_scan.extract import science_io as io
from auto_scan.registry import event_extractor, extractor

# Bands summarised separately so a check can tell which half of the broadband is off.
BANDS = ("lf", "hf")
# A per-IM statistic needs this many stations to enter a band summary.
MIN_N_IM = 3
# Long periods compared across resolutions (each kept only if >= 2 / f_coarse).
XRES_PERIODS: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0, 7.5, 10.0)
# Sites this close (beyond the absorbing layer) to a domain edge are left out of
# the cross-resolution comparison: the edge's reflections differ between domains.
EDGE_MARGIN_KM = 2.0
# SW4's default absorbing layer when the realisation does not state it.
DEFAULT_SUPERGRID_KM = 12.0
# "Well recorded": comfortably above noise for the strong-motion network
# (0.002 g ~ MMI 3, felt). Used only for the coverage count.
WELL_RECORDED_PGA_G = 0.002

COMPARISONS = ("residual", "gmm_residual", "obs_gmm_residual")


def _clear(db: sqlite3.Connection, event_id: str, resolution: str, prefixes: tuple[str, ...]) -> None:
    """Delete this extractor's earlier metrics of a run."""
    for prefix in prefixes:
        db.execute(
            "DELETE FROM metrics WHERE event_id = ? AND resolution = ? AND metric LIKE ?",
            (event_id, resolution, prefix + "%"),
        )


def _put(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    metric: str,
    value: Any,
    unit: str | None = None,
    detail: Any = None,
) -> None:
    """Record a metric, turning numpy scalars and non-finite values into floats/NULL."""
    if value is not None:
        value = float(value)
        if not math.isfinite(value):
            value = None
    set_metric(db, event_id, resolution, metric, value, unit, io.jsonable(detail))


def _ln_ratio(numerator: Any, denominator: Any) -> np.ndarray:
    """``ln(a / b)`` with non-positive or missing values as NaN."""
    a = np.asarray(numerator, dtype=float)
    b = np.asarray(denominator, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(a / b)
    out[~((a > 0) & (b > 0))] = np.nan
    return out


def _ln_clip(values: Any, floor: float) -> np.ndarray:
    """``ln max(x, floor)``, NaN for missing values."""
    x = np.asarray(values, dtype=float)
    with np.errstate(invalid="ignore"):
        return np.log(np.maximum(x, floor))


def _ims(frame: pd.DataFrame) -> list[str]:
    """The IM columns of a site table, PGA and PGV first, pSA by period."""
    psa = sorted((c for c in frame.columns if io.im_period(c) is not None), key=lambda c: io.im_period(c) or 0.0)
    return [c for c in ("PGA", "PGV") if c in frame.columns] + psa


def _summary_metrics(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    prefix: str,
    values: np.ndarray,
    im: str,
    lnrrup: np.ndarray | None = None,
    lnvs30: np.ndarray | None = None,
) -> io.Summary:
    """Write ``<prefix>.<stat>.<IM>`` for one sample, and its slopes when given."""
    s = io.summarise(values)
    if s.n == 0:
        return s
    _put(db, event_id, resolution, f"{prefix}.n.{im}", s.n, "1")
    for name in ("mean", "median", "mad_sigma", "std"):
        _put(db, event_id, resolution, f"{prefix}.{name}.{im}", getattr(s, name), "ln")
    _put(db, event_id, resolution, f"{prefix}.frac_gt_ln10.{im}", s.frac_gt_ln10, "1")
    for name, x in (("lnrrup", lnrrup), ("lnvs30", lnvs30)):
        if x is None:
            continue
        slope, se, _, _ = io.ols_slope(x, values)
        _put(db, event_id, resolution, f"{prefix}.slope_{name}.{im}", slope, "ln")
        _put(db, event_id, resolution, f"{prefix}.slope_{name}_se.{im}", se, "ln")
    return s


def _band_metrics(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    prefix: str,
    medians: dict[str, tuple[float, int]],
    flo: float,
) -> None:
    """``<prefix>.band_median.{lf,hf}`` from per-IM (median, n)."""
    for band in BANDS:
        members = {
            im: round(m, 4)
            for im, (m, n) in medians.items()
            if io.im_band(im, flo) == band and n >= MIN_N_IM and math.isfinite(m)
        }
        if members:
            _put(
                db,
                event_id,
                resolution,
                f"{prefix}.band_median.{band}",
                float(np.median(list(members.values()))),
                "ln",
                {"per_im_median": members, "flo_hz": flo},
            )


@extractor
def residuals(db: sqlite3.Connection, event_id: str, resolution: str, run_dir: Path) -> None:
    """Observed, simulated and empirical IMs at real stations: rows and summaries.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.
    resolution : str
        The run's resolution.
    run_dir : Path
        The run's cache directory.
    """
    _clear(
        db,
        event_id,
        resolution,
        ("science.bb.", "science.gmm_all.", *(f"science.{c}." for c in COMPARISONS)),
    )
    db.execute("DELETE FROM station_residuals WHERE event_id = ? AND resolution = ?", (event_id, resolution))
    event_dir = run_dir.parent
    flo, flo_source = io.crossover_frequency(run_dir, resolution)
    if flo is not None and flo_source == "realisation":
        _put(db, event_id, resolution, "science.bb.flo", flo, "Hz", {"source": flo_source})
    flo = flo or io.DEFAULT_FLO.get(resolution, 1.0)

    # SimDB holds only the finest resolution it ingested: its JSON stands in only for that run.
    sims = io.read_sim_ims(
        run_dir, event_id, real_only=False, json_fallback=io.finest_resolution(event_dir) == resolution
    )
    if sims is None:
        io.warn(f"{event_id}/{resolution}: no simulated IMs (no IM database or SimDB JSON)")
        return
    _gmm_all(db, event_id, resolution, sims, flo)
    _keep_long_periods(run_dir, sims)

    observed = io.read_observed(event_dir / "observed")
    if observed is None:
        return
    real = sims.sim[sims.sim["is_real"]]
    stations = sorted(set(real.index) & set(observed.table.index))
    ims = [im for im in _ims(real) if im in observed.table.columns]
    rows = []
    for station in stations:
        obs_row = observed.table.loc[station]
        sim_row = real.loc[station]
        gmm_row = sims.gmm.loc[station] if station in sims.gmm.index else None
        rrup = (
            sim_row["rrup"] if math.isfinite(sim_row["rrup"]) else pd.to_numeric(obs_row.get("r_rup"), errors="coerce")
        )
        vs30 = (
            sim_row["vs30"] if math.isfinite(sim_row["vs30"]) else pd.to_numeric(obs_row.get("Vs30"), errors="coerce")
        )
        for im in ims:
            obs_value = pd.to_numeric(obs_row[im], errors="coerce")
            sim_value = sim_row[im]
            if not (obs_value > 0 and sim_value > 0):
                continue
            gmm_value = float(gmm_row[im]) if gmm_row is not None and im in gmm_row.index else float("nan")
            rows.append(
                {
                    "station": station,
                    "im": im,
                    "observed": float(obs_value),
                    "simulated": float(sim_value),
                    "gmm": gmm_value if gmm_value > 0 else None,
                    "residual": math.log(float(obs_value) / float(sim_value)),
                    "rrup_km": None if pd.isna(rrup) else float(rrup),
                    "vs30": None if pd.isna(vs30) else float(vs30),
                    "quality_ok": int(observed.usable(station, im)),
                }
            )
    db.executemany(
        "INSERT OR REPLACE INTO station_residuals"
        " (event_id, resolution, station, im, observed, simulated, gmm, residual, rrup_km, vs30, quality_ok)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                event_id,
                resolution,
                r["station"],
                r["im"],
                r["observed"],
                r["simulated"],
                r["gmm"],
                r["residual"],
                r["rrup_km"],
                r["vs30"],
                r["quality_ok"],
            )
            for r in rows
        ],
    )
    table = observed.table
    rejected = sorted(s for s in stations if not bool(table.loc[s, "record_ok"]))
    _put(
        db,
        event_id,
        resolution,
        "science.residual.n_stations",
        len(stations),
        "1",
        {
            "sim_source": sims.source,
            "gmm_source": sims.gmm_source or None,
            "n_observed_records": observed.n_records,
            "n_observed_stations": len(table),
            "n_observed_ok": int(table["record_ok"].sum()),
            "n_sim_real": len(real),
            "rejected_stations": rejected,
            "quality_rule": (
                f"min(score_X,score_Y) >= {io.MIN_SCORE}, max(multi) < {io.MAX_MULTI}, ground level;"
                f" pSA T in [1/fmax, 1/({io.FMIN_MARGIN} fmin)]"
            ),
        },
    )
    if not rows:
        return
    frame = pd.DataFrame(rows)
    frame = frame[frame["quality_ok"] == 1]
    medians: dict[str, dict[str, tuple[float, int]]] = {c: {} for c in COMPARISONS}
    for im, group in frame.groupby("im", sort=False):
        im = str(im)
        lnrrup = _ln_clip(group["rrup_km"], 1.0)
        lnvs30 = _ln_clip(group["vs30"], 1.0)
        gmm = group["gmm"].astype(float).to_numpy()
        samples = {
            "residual": group["residual"].to_numpy(dtype=float),
            "gmm_residual": _ln_ratio(gmm, group["simulated"]),
            "obs_gmm_residual": _ln_ratio(group["observed"], gmm),
        }
        for comparison, values in samples.items():
            with_slopes = comparison != "obs_gmm_residual"
            s = _summary_metrics(
                db,
                event_id,
                resolution,
                f"science.{comparison}",
                values,
                im,
                lnrrup if with_slopes else None,
                lnvs30 if with_slopes else None,
            )
            medians[comparison][im] = (s.median, s.n)
    for comparison in COMPARISONS:
        _band_metrics(db, event_id, resolution, f"science.{comparison}", medians[comparison], flo)


def _gmm_all(db: sqlite3.Connection, event_id: str, resolution: str, sims: io.SimIMs, flo: float) -> None:
    """``science.gmm_all.*``: the simulation against the GMM at every site of the run."""
    if sims.gmm.empty:
        return
    joined = sims.sim.merge(sims.gmm, how="inner", left_index=True, right_index=True, suffixes=("", "_gmm"))
    if joined.empty:
        return
    lnrrup = _ln_clip(joined["rrup"], 1.0)
    medians: dict[str, tuple[float, int]] = {}
    for im in _ims(sims.sim):
        if f"{im}_gmm" not in joined.columns:
            continue
        values = _ln_ratio(joined[f"{im}_gmm"], joined[im])
        s = io.summarise(values)
        if s.n == 0:
            continue
        _put(db, event_id, resolution, f"science.gmm_all.n.{im}", s.n, "1", {"gmm_source": sims.gmm_source})
        _put(db, event_id, resolution, f"science.gmm_all.median.{im}", s.median, "ln")
        _put(db, event_id, resolution, f"science.gmm_all.mad_sigma.{im}", s.mad_sigma, "ln")
        slope, se, _, _ = io.ols_slope(lnrrup, values)
        _put(db, event_id, resolution, f"science.gmm_all.slope_lnrrup.{im}", slope, "ln")
        _put(db, event_id, resolution, f"science.gmm_all.slope_lnrrup_se.{im}", se, "ln")
        medians[im] = (s.median, s.n)
    _band_metrics(db, event_id, resolution, "science.gmm_all", medians, flo)


# --------------------------------------------------------------------------- cross-resolution

# The collector may delete a run's IM databases as soon as its per-run extractors
# finish, before the event extractors run: the residuals extractor keeps the
# long-period IMs the cross-resolution comparison needs in this small file.
LONG_PERIOD_FILE = "science_long_period_ims.csv"


def _keep_long_periods(run_dir: Path, sims: io.SimIMs) -> None:
    """Write every site's long-period IMs next to the run's files (only from an IM database)."""
    if not sims.source.endswith(".duckdb"):
        return
    columns = ["lat", "lon"] + [io.im_name(p) for p in XRES_PERIODS if io.im_name(p) in sims.sim.columns]
    try:
        sims.sim[columns].to_csv(run_dir / LONG_PERIOD_FILE, float_format="%.6g")
    except OSError as error:
        io.warn(f"{run_dir}: cannot keep long-period IMs ({error})")


def _read_long_periods(run_dir: Path) -> io.SimIMs | None:
    """The long-period IMs kept by ``_keep_long_periods``, if any."""
    frame = io.read_csv(run_dir / LONG_PERIOD_FILE)
    if frame is None or "site_id" not in frame.columns:
        return None
    return io.SimIMs(sim=frame.set_index("site_id"), gmm=pd.DataFrame(), source=str(run_dir / LONG_PERIOD_FILE))


def _interior(sites: pd.DataFrame, run_dir: Path) -> np.ndarray:
    """Which sites are inside the run's domain, clear of its absorbing layer."""
    polygon = io.domain_polygon(run_dir)
    if polygon is None:
        return np.ones(len(sites), dtype=bool)
    width = io.supergrid_width_km(run_dir) or DEFAULT_SUPERGRID_KM
    inside, distance = io.polygon_edge_distance(sites["lat"].to_numpy(), sites["lon"].to_numpy(), polygon)
    return inside & (distance >= width + EDGE_MARGIN_KM)


def resolution_pairs(resolutions: list[str]) -> list[tuple[str, str]]:
    """Every (coarse, fine) pair of known resolutions.

    Parameters
    ----------
    resolutions : list of str
        Resolutions present.

    Returns
    -------
    list of (str, str)
        Pairs, coarse first, ordered by the coarse then the fine resolution.
    """
    known = sorted((r for r in resolutions if r in io.RESOLUTION_HZ), key=io.RESOLUTION_HZ.__getitem__)
    return list(combinations(known, 2))


@event_extractor
def cross_resolution(db: sqlite3.Connection, event_id: str, event_dir: Path) -> None:
    """Long-period IMs of the same rupture simulated at two resolutions.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.
    event_dir : Path
        The event's cache directory.
    """
    db.execute(
        "DELETE FROM metrics WHERE event_id = ? AND resolution = '' AND metric LIKE 'science.xres.%'"
        " AND metric NOT LIKE 'science.xres.%.wave.%'",
        (event_id,),
    )
    present = [
        r
        for r in io.RESOLUTION_HZ
        if io.imdb_paths(event_dir / r, event_id) or (event_dir / r / LONG_PERIOD_FILE).is_file()
    ]
    pairs = resolution_pairs(present)
    if not pairs:
        return
    cache: dict[str, io.SimIMs | None] = {}
    for coarse, fine in pairs:
        periods = tuple(p for p in XRES_PERIODS if p >= 2.0 / io.RESOLUTION_HZ[coarse])
        for res in (coarse, fine):
            if res not in cache:
                cache[res] = io.read_sim_ims(
                    event_dir / res, event_id, periods=XRES_PERIODS, real_only=False
                ) or _read_long_periods(event_dir / res)
        a, b = cache[coarse], cache[fine]
        if a is None or b is None or not periods:
            continue
        joined = a.sim.merge(b.sim, how="inner", left_index=True, right_index=True, suffixes=("_c", "_f"))
        if joined.empty:
            continue
        sites = joined.rename(columns={"lat_c": "lat", "lon_c": "lon"})
        keep = _interior(sites, event_dir / coarse) & _interior(sites, event_dir / fine)
        pair = f"{coarse}-{fine}"
        prefix = f"science.xres.{pair}"
        kept = joined[keep]
        _put(
            db,
            event_id,
            "",
            f"{prefix}.n",
            len(kept),
            "1",
            {
                "n_common_sites": len(joined),
                "n_near_edge": int((~keep).sum()),
                "periods": list(periods),
                "files": [a.source, b.source],
            },
        )
        medians, spreads = {}, {}
        for period in periods:
            im = io.im_name(period)
            if f"{im}_c" not in kept.columns:
                continue
            s = io.summarise(_ln_ratio(kept[f"{im}_f"], kept[f"{im}_c"]))
            if s.n == 0:
                continue
            _put(db, event_id, "", f"{prefix}.mean.{im}", s.mean, "ln")
            _put(db, event_id, "", f"{prefix}.median.{im}", s.median, "ln")
            _put(db, event_id, "", f"{prefix}.mad_sigma.{im}", s.mad_sigma, "ln")
            medians[im], spreads[im] = round(s.median, 4), round(s.mad_sigma, 4)
        if medians:
            detail = {"per_im_median": medians, "per_im_mad_sigma": spreads}
            _put(db, event_id, "", f"{prefix}.median", float(np.median(list(medians.values()))), "ln", detail)
            _put(db, event_id, "", f"{prefix}.mad_sigma", float(np.nanmedian(list(spreads.values()))), "ln", detail)


# --------------------------------------------------------------------------- source and coverage


def _imdb_realisation(path: Path, event_id: str) -> tuple[float | None, tuple[float, float, float] | None]:
    """(magnitude, hypocentre) of an IM database's realisation."""
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error:
        return None, None
    try:
        row = con.execute(
            "SELECT r.magnitude, r.hypo_lat, r.hypo_lon, r.hypo_depth FROM realisations r"
            " JOIN events e USING (event_int_id) ORDER BY (e.event_id = $event) DESC LIMIT 1",
            {"event": event_id},
        ).fetchone()
    except duckdb.Error:
        return None, None
    finally:
        con.close()
    if row is None:
        return None, None
    hypo = None if row[1] is None else (float(row[1]), float(row[2]), float(row[3]))
    return (None if row[0] is None else float(row[0])), hypo


def _imdb_real_sites(path: Path) -> set[str]:
    """The real sites of an IM database."""
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error:
        return set()
    try:
        columns = {row[0] for row in con.execute("DESCRIBE sites").fetchall()}
        sql = "SELECT site_id FROM sites" + (" WHERE is_real" if "is_real" in columns else "")
        return {str(row[0]) for row in con.execute(sql).fetchall()}
    except duckdb.Error:
        return set()
    finally:
        con.close()


@event_extractor
def source_and_coverage(db: sqlite3.Connection, event_id: str, event_dir: Path) -> None:
    """The simulated source against GeoNet's, and which observed stations were simulated.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.
    event_dir : Path
        The event's cache directory.
    """
    _clear(db, event_id, "", ("science.source.", "science.coverage."))
    runs = [r for r in io.RESOLUTION_HZ if (event_dir / r).is_dir()]

    # Every place the simulated magnitude is written down should agree.
    magnitudes: dict[str, float] = {}
    hypocentre = io.simulated_hypocentre(event_dir)
    hypo_source = "realisation.json"
    mag = io.simulated_magnitude(event_dir)
    if mag is not None:
        magnitudes["realisation.json"] = mag
    sim_real: set[str] = set()
    for res in runs:
        run_dir = event_dir / res
        m = io.simulated_magnitude(run_dir)
        if m is not None:
            magnitudes[f"{res}/realisation.json"] = m
        if hypocentre is None:
            hypocentre, hypo_source = io.simulated_hypocentre(run_dir), f"{res}/realisation.json"
        for path in io.imdb_paths(run_dir, event_id):
            m, hypo = _imdb_realisation(path, event_id)
            if m is not None:
                magnitudes[f"{res}/{path.name}"] = m
            if hypocentre is None and hypo is not None:
                hypocentre, hypo_source = hypo, f"{res}/{path.name}"
            sim_real |= _imdb_real_sites(path)
        waveforms = io.read_waveforms(run_dir, event_id)
        if waveforms is not None:
            sim_real |= set(waveforms.stations)
    db_mag = db.execute("SELECT magnitude FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if db_mag is not None and db_mag[0] is not None:
        magnitudes["events.magnitude"] = float(db_mag[0])
    simdb = io.read_simdb_json(event_dir / "simdb_station_ims.json")
    if simdb is not None:
        sim_real |= set(simdb.index)

    mag_sim = magnitudes.get("realisation.json", next(iter(magnitudes.values()), None))
    if mag_sim is not None:
        _put(db, event_id, "", "science.source.mag_sim", mag_sim, "Mw", {"sources": magnitudes})
        _put(
            db,
            event_id,
            "",
            "science.source.mag_spread",
            max(magnitudes.values()) - min(magnitudes.values()),
            "Mw",
            {"sources": magnitudes},
        )

    observed_dir = event_dir / "observed"
    source = io.read_csv(observed_dir / "earthquake_source_table.csv")
    if source is not None and len(source):
        row = source.iloc[0]
        lat, lon = pd.to_numeric(row.get("lat"), errors="coerce"), pd.to_numeric(row.get("lon"), errors="coerce")
        depth = pd.to_numeric(row.get("depth"), errors="coerce")
        geonet = {"lat": lat, "lon": lon, "depth_km": depth, "file": str(observed_dir / "earthquake_source_table.csv")}
        if hypocentre is not None and pd.notna(lat) and pd.notna(lon):
            detail = {"simulated": hypocentre, "simulated_source": hypo_source, "geonet": geonet}
            offset = float(io.haversine_km(hypocentre[0], hypocentre[1], lat, lon))
            _put(db, event_id, "", "science.source.hypo_offset_km", offset, "km", detail)
            if pd.notna(depth):
                _put(db, event_id, "", "science.source.depth_diff_km", hypocentre[2] - float(depth), "km", detail)
        obs_mag = pd.to_numeric(row.get("mag"), errors="coerce")
        mag_type = str(row.get("mag_type", ""))
        if pd.notna(obs_mag):
            detail = {"mag_type": mag_type, "mag_method": str(row.get("mag_method", "")), "simulated_mw": mag_sim}
            _put(db, event_id, "", "science.source.mag_obs", obs_mag, mag_type or None, detail)
            is_mw = mag_type.lower().startswith("mw")
            diff = (mag_sim - float(obs_mag)) if (is_mw and mag_sim is not None) else None
            if not is_mw:
                detail["note"] = "GeoNet's magnitude is not a moment magnitude: not compared"
            _put(db, event_id, "", "science.source.mag_diff_obs", diff, "Mw", detail)

    observed = io.read_observed(observed_dir)
    if observed is None:
        return
    table = observed.table
    good = table["record_ok"].astype(bool)
    pga = pd.to_numeric(table["PGA"], errors="coerce")
    well = good & (pga >= WELL_RECORDED_PGA_G)
    _put(db, event_id, "", "science.coverage.n_observed", len(table), "1")
    _put(db, event_id, "", "science.coverage.n_observed_good", int(good.sum()), "1")
    _put(db, event_id, "", "science.coverage.n_well_recorded", int(well.sum()), "1", {"min_pga_g": WELL_RECORDED_PGA_G})
    _put(db, event_id, "", "science.coverage.n_sim_real", len(sim_real), "1")
    _put(db, event_id, "", "science.coverage.n_joined", len(sim_real & set(table.index)), "1")
    if not {"sta_lat", "sta_lon"} <= set(table.columns):
        return
    lat = pd.to_numeric(table["sta_lat"], errors="coerce").to_numpy()
    lon = pd.to_numeric(table["sta_lon"], errors="coerce").to_numpy()
    in_any = np.zeros(len(table), dtype=bool)
    interior_any = np.zeros(len(table), dtype=bool)
    domains = 0
    for res in runs:
        polygon = io.domain_polygon(event_dir / res)
        if polygon is None:
            continue
        domains += 1
        inside, distance = io.polygon_edge_distance(lat, lon, polygon)
        width = io.supergrid_width_km(event_dir / res) or DEFAULT_SUPERGRID_KM
        in_any |= inside
        interior_any |= inside & (distance >= width)
    if domains == 0:
        return
    stations = np.asarray(table.index)
    outside = stations[well.to_numpy() & ~in_any]
    missing = [s for s in stations[well.to_numpy() & interior_any] if s not in sim_real]
    n_well = int(well.sum())
    _put(db, event_id, "", "science.coverage.n_good_outside", len(outside), "1", {"stations": sorted(outside.tolist())})
    _put(
        db,
        event_id,
        "",
        "science.coverage.frac_good_outside",
        len(outside) / n_well if n_well else None,
        "1",
        {"n_well_recorded": n_well},
    )
    _put(
        db,
        event_id,
        "",
        "science.coverage.n_good_inside_unsimulated",
        len(missing),
        "1",
        {"stations": sorted(missing), "n_sim_real": len(sim_real)},
    )
