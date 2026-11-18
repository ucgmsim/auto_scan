"""Science extractors that read the simulated broadband waveforms.

Per run (``@extractor waveform_sanity``), from ``simdb/<event>_waveforms.nc``
(acceleration in g at real stations, t = 0 at the rupture's start):

==================================================  =====  ==========================================================
metric                                              unit   meaning
==================================================  =====  ==========================================================
science.wave.n_stations                             1      stations in the file
science.wave.n_invalid                              1      stations with a NaN/inf sample, an all-zero component or
                                                           ``|a| > MAX_ABS_G``; detail: which and why
science.wave.max_abs_g                              g      largest finite ``|a|`` in the file
science.wave.n_early                                1      stations whose peak before the earliest possible P
                                                           (``rrup / 8 km/s - 1 s``) exceeds ``EARLY_FRACTION`` of
                                                           their peak (a shifted or non-zero start)
science.wave.frac_early                             1      ... as a fraction of stations with a known rrup
science.wave.ai_last5.median / .max                 1      fraction of a station's Arias intensity (3 components)
                                                           in the last 5% of the record
science.wave.frac_truncated                         1      fraction of stations with ``ai_last5 > TRUNCATED_AI``
science.wave.hv.median                              1      median over stations of
                                                           ``sqrt(PGA_000 PGA_090) / PGA_ver``
science.wave.s_offset.n                             1      stations with an observed S pick (prob >= 0.5)
science.wave.s_offset.median                        s      median of simulated minus observed S onset, after
                                                           removing the hypocentre difference
                                                           (``(R_sim - R_geonet) / VS_KM_S``)
science.wave.s_offset.mad_sigma                     s      its robust spread
science.wave.s_offset.intercept / .intercept_se     s      intercept of the offset regressed (OLS, outliers beyond
                                                           ``max(5 sigma, 3 s)`` dropped) on GeoNet hypocentral
                                                           distance: the distance-independent part, i.e. a clock
                                                           or origin-time error (a velocity error grows with distance)
science.wave.s_offset.slope                         s/km   the slope of that regression
science.crossover.step.median                       ln     median over stations (both horizontals) of the step in
                                                           the smoothed Fourier amplitude spectrum at ``bb.flo``:
                                                           a line fitted to ``ln FAS`` against ``ln f`` on
                                                           ``[flo/4, flo/1.25]`` and one on ``[1.25 flo, 4 flo]``,
                                                           each extrapolated to ``flo``, upper minus lower
science.crossover.step.mad_sigma                    ln     its robust spread
science.crossover.step.n                            1      station-components measured
==================================================  =====  ==========================================================

Per event (``@event_extractor cross_resolution_waveforms``), for each pair
``<pair>`` of resolutions whose waveforms exist, at the stations both hold, both
horizontals low-passed at half the coarse run's frequency (where both are SW4):

==================================================  =====  ==========================================================
science.xres.<pair>.wave.n                          1      station-components compared
science.xres.<pair>.wave.xcorr0.median              1      median zero-lag normalised cross-correlation
science.xres.<pair>.wave.xcorr_best.median          1      median of the best normalised cross-correlation within
                                                           ``MAX_LAG_S``
science.xres.<pair>.wave.lag.median                 s      median lag of that best (fine relative to coarse;
                                                           positive = fine run later)
science.xres.<pair>.wave.lag.mad_sigma              s      its robust spread
==================================================  =====  ==========================================================

The onset pick is the first time the horizontal envelope ``hypot(a_000,
a_090)`` exceeds ``ONSET_FRACTION`` of its peak. The stochastic HF part carries
no P wave, so the onset of a simulated broadband trace is its S wave; hence the
comparison with observed S picks (the observed P is often invisible in the
simulation at 0.5 Hz and below).
"""

import math
import sqlite3
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

from auto_scan.db import set_metric
from auto_scan.extract import science_io as io
from auto_scan.extract.science import resolution_pairs
from auto_scan.registry import event_extractor, extractor

# A strong-motion record above 5 g has never been observed; a simulated one is a bug.
MAX_ABS_G = 5.0
# Fastest crustal/upper-mantle P speed: no energy can arrive before rrup / 8 km/s.
MAX_VP_KM_S = 8.0
# Slack for the acausal (two-pass) filters SW4's prefilter and bb_sim apply, s.
ACAUSAL_SLACK_S = 1.0
# Pre-P peak above this fraction of the trace's peak counts as early energy (acausal
# filter ringing measured at ~1e-5 of the peak on 2026p866048).
EARLY_FRACTION = 0.1
# A record still holding 5% of its Arias intensity in its last 5% was cut short
# (2026p866048: < 1e-3 at every station).
TRUNCATED_AI = 0.05
# Envelope fraction defining the onset.
ONSET_FRACTION = 0.1
# Shear speed used only to move an onset between the two hypocentres.
VS_KM_S = 3.5
# Offsets further than this many robust sigmas (or OUTLIER_FLOOR_S) from the median
# are left out of the intercept fit.
OUTLIER_MADS = 5.0
OUTLIER_FLOOR_S = 3.0
# Cross-resolution lags are searched within this window.
MAX_LAG_S = 5.0
# Crossover fit bands, as factors of flo.
CROSSOVER_INNER = 1.25
CROSSOVER_OUTER = 4.0
# Minimum Fourier samples in each crossover fit band.
MIN_BAND_SAMPLES = 4


def _put(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    metric: str,
    value: object,
    unit: str | None = None,
    detail: object = None,
) -> None:
    """Record a metric, non-finite as NULL."""
    number: float | None = None
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = float(value)
        if not math.isfinite(number):
            number = None
    set_metric(db, event_id, resolution, metric, number, unit, io.jsonable(detail))


def _station_geometry(run_dir: Path, event_id: str, stations: list[str]) -> pd.DataFrame:
    """rrup, lat and lon of the run's real stations, from the IM database, else SimDB's JSON or the flatfile."""
    frame = pd.DataFrame(index=pd.Index(stations, name="site_id"), columns=["rrup", "lat", "lon"], dtype=float)
    sims = io.read_sim_ims(run_dir, event_id, periods=(), real_only=True, json_fallback=True)
    if sims is not None:
        known = sims.sim.reindex(frame.index)
        frame = frame.fillna(known[["rrup", "lat", "lon"]])
    observed = io.read_observed(run_dir.parent / "observed")
    if observed is not None:
        table = observed.table.reindex(frame.index)
        fallback = pd.DataFrame(
            {
                "rrup": pd.to_numeric(table.get("r_rup"), errors="coerce"),
                "lat": pd.to_numeric(table.get("sta_lat"), errors="coerce"),
                "lon": pd.to_numeric(table.get("sta_lon"), errors="coerce"),
            },
            index=frame.index,
        )
        frame = frame.fillna(fallback)
    return frame.astype(float)


def _onset(envelope: np.ndarray, dt: float) -> float:
    """First time the envelope exceeds ``ONSET_FRACTION`` of its peak, s."""
    peak = float(np.max(envelope))
    if not peak > 0:
        return float("nan")
    return float(np.argmax(envelope > ONSET_FRACTION * peak)) * dt


def crossover_step(horizontal: np.ndarray, dt: float, flo: float) -> float:
    """The step in ``ln FAS`` at the crossover frequency of one trace.

    Parameters
    ----------
    horizontal : numpy.ndarray
        One acceleration trace.
    dt : float
        Its time step, s.
    flo : float
        The crossover frequency, Hz.

    Returns
    -------
    float
        Upper-band fit minus lower-band fit, both evaluated at ``flo``
        (NaN when a band has too few Fourier samples).
    """
    n = horizontal.size
    freq = np.fft.rfftfreq(n, dt)
    amplitude = np.abs(np.fft.rfft(horizontal)) * dt
    with np.errstate(divide="ignore"):
        ln_a = np.log(amplitude)
    values = []
    for lo, hi in ((flo / CROSSOVER_OUTER, flo / CROSSOVER_INNER), (flo * CROSSOVER_INNER, flo * CROSSOVER_OUTER)):
        band = (freq >= lo) & (freq <= hi) & np.isfinite(ln_a)
        if band.sum() < MIN_BAND_SAMPLES:
            return float("nan")
        coeffs = np.polyfit(np.log(freq[band]), ln_a[band], 1)
        values.append(float(np.polyval(coeffs, math.log(flo))))
    return values[1] - values[0]


@extractor
def waveform_sanity(db: sqlite3.Connection, event_id: str, resolution: str, run_dir: Path) -> None:
    """Sanity, timing and crossover metrics of a run's broadband waveforms.

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
    for prefix in ("science.wave.", "science.crossover."):
        db.execute(
            "DELETE FROM metrics WHERE event_id = ? AND resolution = ? AND metric LIKE ?",
            (event_id, resolution, prefix + "%"),
        )
    wf = io.read_waveforms(run_dir, event_id)
    if wf is None:
        return
    put = partial(_put, db, event_id, resolution)
    acc, dt = wf.acc, wf.dt
    n_st, _, nt = acc.shape
    put("science.wave.n_stations", n_st, "1", {"file": wf.path, "dt": dt, "nt": nt})
    finite = np.isfinite(acc)
    invalid: dict[str, str] = {}
    for i, station in enumerate(wf.stations):
        if not finite[i].all():
            invalid[station] = "non-finite samples"
        elif (np.max(np.abs(acc[i]), axis=1) == 0).any():
            invalid[station] = "all-zero component " + ",".join(
                c for c, m in zip(wf.components, np.max(np.abs(acc[i]), axis=1), strict=True) if m == 0
            )
        elif np.max(np.abs(acc[i])) > MAX_ABS_G:
            invalid[station] = f"|a| = {np.max(np.abs(acc[i])):.3g} g > {MAX_ABS_G} g"
    put("science.wave.n_invalid", len(invalid), "1", {"stations": invalid, "file": wf.path})
    clean = np.where(finite, acc, 0.0)
    put("science.wave.max_abs_g", float(np.max(np.abs(clean))) if clean.size else None, "g")
    good = [i for i, s in enumerate(wf.stations) if s not in invalid]
    if not good:
        return

    geometry = _station_geometry(run_dir, event_id, wf.stations)
    time = np.arange(nt) * dt
    i000, i090, iver = wf.component("000"), wf.component("090"), wf.component("ver")

    # Energy before the earliest possible arrival.
    early: dict[str, float] = {}
    n_known = 0
    for i in good:
        rrup = geometry["rrup"].iloc[i]
        if not math.isfinite(rrup):
            continue
        n_known += 1
        t_min = rrup / MAX_VP_KM_S - ACAUSAL_SLACK_S
        before = time < t_min
        if before.sum() < 2:
            continue
        ratio = float(np.max(np.abs(clean[i][:, before])) / np.max(np.abs(clean[i])))
        if ratio > EARLY_FRACTION:
            early[wf.stations[i]] = round(ratio, 3)
    put("science.wave.n_early", len(early), "1", {"stations": early, "max_vp_km_s": MAX_VP_KM_S})
    put("science.wave.frac_early", len(early) / n_known if n_known else None, "1")

    # Arias intensity left at the end of the record.
    tail = max(1, round(0.05 * nt))
    energy = np.sum(clean[good] ** 2, axis=1)
    total = energy.sum(axis=1)
    last5 = np.where(total > 0, energy[:, -tail:].sum(axis=1) / np.where(total > 0, total, 1.0), np.nan)
    truncated = {wf.stations[good[k]]: round(float(v), 3) for k, v in enumerate(last5) if v > TRUNCATED_AI}
    put("science.wave.ai_last5.median", float(np.nanmedian(last5)), "1")
    put("science.wave.ai_last5.max", float(np.nanmax(last5)), "1")
    put("science.wave.frac_truncated", len(truncated) / len(good), "1", {"stations": truncated, "duration_s": nt * dt})

    if i000 is None or i090 is None:
        return
    pga = np.max(np.abs(clean), axis=2)
    if iver is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            hv = np.sqrt(pga[good, i000] * pga[good, i090]) / pga[good, iver]
        hv = hv[np.isfinite(hv)]
        if hv.size:
            put("science.wave.hv.median", float(np.median(hv)), "1", {"n": int(hv.size)})

    _s_offset(db, event_id, resolution, run_dir, wf, good, clean, geometry)
    _crossover(db, event_id, resolution, run_dir, wf, good, clean)


def _s_offset(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    run_dir: Path,
    wf: io.Waveforms,
    good: list[int],
    clean: np.ndarray,
    geometry: pd.DataFrame,
) -> None:
    """``science.wave.s_offset.*``: simulated onset against observed S picks."""
    picks = io.observed_phase_times(run_dir.parent / "observed", "s")
    if picks.empty:
        return
    i000, i090 = wf.component("000"), wf.component("090")
    assert i000 is not None and i090 is not None
    sim_hypo = io.simulated_hypocentre(run_dir) or io.simulated_hypocentre(run_dir.parent)
    if sim_hypo is None:
        sims = io.read_sim_ims(run_dir, event_id, periods=(), real_only=True)
        sim_hypo = sims.hypocentre if sims is not None else None
    source = io.read_csv(run_dir.parent / "observed" / "earthquake_source_table.csv")
    geonet = None
    if source is not None and len(source) and {"lat", "lon", "depth"} <= set(source.columns):
        row = source.iloc[0]
        geonet = (float(row["lat"]), float(row["lon"]), float(row["depth"]))
    rows = []
    for i in good:
        station = wf.stations[i]
        if station not in picks.index:
            continue
        onset = _onset(np.hypot(clean[i, i000], clean[i, i090]), wf.dt)
        lat, lon = geometry["lat"].iloc[i], geometry["lon"].iloc[i]
        correction, r_obs = 0.0, float("nan")
        if geonet is not None and math.isfinite(lat) and math.isfinite(lon):
            r_obs = math.hypot(float(io.haversine_km(geonet[0], geonet[1], lat, lon)), geonet[2])
            if sim_hypo is not None:
                r_sim = math.hypot(float(io.haversine_km(sim_hypo[0], sim_hypo[1], lat, lon)), sim_hypo[2])
                correction = (r_sim - r_obs) / VS_KM_S
        rows.append((station, onset - float(picks[station]) - correction, r_obs, onset, float(picks[station])))
    if not rows:
        return
    offsets = np.array([r[1] for r in rows])
    s = io.summarise(offsets)
    # Bad picks (e.g. a P picked as S) are far outliers; keep them out of the line fit.
    spread = max(OUTLIER_MADS * s.mad_sigma, OUTLIER_FLOOR_S) if math.isfinite(s.mad_sigma) else math.inf
    inlier = np.abs(offsets - s.median) <= spread
    distances = np.array([r[2] for r in rows])
    slope, _, intercept, intercept_se = io.ols_slope(distances[inlier], offsets[inlier])
    detail = {
        "per_station": {
            r[0]: {"sim_onset_s": round(r[3], 2), "obs_s_s": round(r[4], 2), "offset_s": round(r[1], 2)} for r in rows
        },
        "onset_fraction": ONSET_FRACTION,
        "hypocentre_correction_vs_km_s": VS_KM_S,
        "simulated_hypocentre": sim_hypo,
        "geonet_hypocentre": geonet,
        "fit_outliers": [r[0] for r, keep in zip(rows, inlier, strict=True) if not keep],
    }
    put = partial(_put, db, event_id, resolution)
    put("science.wave.s_offset.n", s.n, "1")
    put("science.wave.s_offset.median", s.median, "s", detail)
    put("science.wave.s_offset.mad_sigma", s.mad_sigma, "s")
    put("science.wave.s_offset.intercept", intercept, "s")
    put("science.wave.s_offset.intercept_se", intercept_se, "s")
    put("science.wave.s_offset.slope", slope, "s/km")


def _crossover(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    run_dir: Path,
    wf: io.Waveforms,
    good: list[int],
    clean: np.ndarray,
) -> None:
    """``science.crossover.step.*``."""
    flo, flo_source = io.crossover_frequency(run_dir, resolution)
    if flo is None:
        return
    steps = []
    for i in good:
        for c in ("000", "090"):
            k = wf.component(c)
            if k is not None:
                steps.append(crossover_step(clean[i, k], wf.dt, flo))
    s = io.summarise(steps)
    if s.n == 0:
        return
    detail = {
        "flo_hz": flo,
        "flo_source": flo_source,
        "bands_hz": [flo / CROSSOVER_OUTER, flo / CROSSOVER_INNER, flo * CROSSOVER_INNER, flo * CROSSOVER_OUTER],
    }
    _put(db, event_id, resolution, "science.crossover.step.median", s.median, "ln", detail)
    _put(db, event_id, resolution, "science.crossover.step.mad_sigma", s.mad_sigma, "ln")
    _put(db, event_id, resolution, "science.crossover.step.n", s.n, "1")


def best_lag(a: np.ndarray, b: np.ndarray, dt: float, max_lag_s: float) -> tuple[float, float, float]:
    """Normalised cross-correlation of two equal-length traces.

    Parameters
    ----------
    a, b : numpy.ndarray
        The traces (``b`` is the fine run's).
    dt : float
        The time step, s.
    max_lag_s : float
        The largest lag searched, s.

    Returns
    -------
    tuple of float
        ``(cc at zero lag, best cc, lag of the best in s)``; the lag is
        positive when ``b`` is later than ``a``.
    """
    norm = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
    if norm == 0:
        return float("nan"), float("nan"), float("nan")
    full = signal.correlate(b, a, mode="full", method="fft") / norm
    lags = np.arange(-a.size + 1, b.size)
    window = np.abs(lags) <= round(max_lag_s / dt)
    k = int(np.argmax(full[window]))
    zero = float(full[lags == 0][0])
    return zero, float(full[window][k]), float(lags[window][k]) * dt


@event_extractor
def cross_resolution_waveforms(db: sqlite3.Connection, event_id: str, event_dir: Path) -> None:
    """Low-passed waveforms of the same rupture at two resolutions: correlation and lag.

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
        "DELETE FROM metrics WHERE event_id = ? AND resolution = '' AND metric LIKE 'science.xres.%.wave.%'",
        (event_id,),
    )
    waveforms = {r: io.read_waveforms(event_dir / r, event_id) for r in io.RESOLUTION_HZ if (event_dir / r).is_dir()}
    present = [r for r, w in waveforms.items() if w is not None]
    for coarse, fine in resolution_pairs(present):
        a, b = waveforms[coarse], waveforms[fine]
        assert a is not None and b is not None
        if abs(a.dt - b.dt) > 1e-9:
            io.warn(f"{event_id}: {coarse} and {fine} waveforms have different dt; not compared")
            continue
        cutoff = 0.5 * io.RESOLUTION_HZ[coarse]
        sos = signal.butter(4, cutoff, "low", fs=1.0 / a.dt, output="sos")
        nt = min(a.acc.shape[2], b.acc.shape[2])
        rows = []
        for station in sorted(set(a.stations) & set(b.stations)):
            ia, ib = a.stations.index(station), b.stations.index(station)
            for c in ("000", "090"):
                ka, kb = a.component(c), b.component(c)
                if ka is None or kb is None:
                    continue
                xa, xb = a.acc[ia, ka, :nt], b.acc[ib, kb, :nt]
                if not (np.isfinite(xa).all() and np.isfinite(xb).all()):
                    continue
                fa, fb = signal.sosfiltfilt(sos, xa), signal.sosfiltfilt(sos, xb)
                zero, best, lag = best_lag(fa, fb, a.dt, MAX_LAG_S)
                if math.isfinite(zero):
                    rows.append((station, c, zero, best, lag))
        if not rows:
            continue
        prefix = f"science.xres.{coarse}-{fine}.wave"
        lags = io.summarise([r[4] for r in rows])
        detail = {
            "lowpass_hz": cutoff,
            "files": [a.path, b.path],
            "worst": sorted(
                ({"station": r[0], "component": r[1], "xcorr0": round(r[2], 3), "lag_s": round(r[4], 2)} for r in rows),
                key=lambda d: d["xcorr0"],
            )[:5],
        }
        _put(db, event_id, "", f"{prefix}.n", len(rows), "1", detail)
        _put(db, event_id, "", f"{prefix}.xcorr0.median", float(np.median([r[2] for r in rows])), "1", detail)
        _put(db, event_id, "", f"{prefix}.xcorr_best.median", float(np.median([r[3] for r in rows])), "1")
        _put(db, event_id, "", f"{prefix}.lag.median", lags.median, "s", detail)
        _put(db, event_id, "", f"{prefix}.lag.mad_sigma", lags.mad_sigma, "s")
