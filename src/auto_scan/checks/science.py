"""Science checks: each fails on a specific class of bug, not on ordinary model misfit.

Scale of ordinary misfit, which every bound below is set well outside: the
within-event sigma of ``ln(obs/sim)`` for these simulations is about 0.5-0.7,
event terms of +-0.5 are common, a simulation and an empirical model (GMM) differ
by up to ~0.5-1.0 at some periods for a single event, and two resolutions of the
same rupture agree at long periods to a median within +-0.05 and a robust spread
of ~0.2 (2026p866048, 0.5 vs 1 Hz, 7000 sites). Only ~12 events exist, so the
bounds are absolute; where a metric has a history of at least ``MIN_HISTORY``
earlier events, a value far outside that history also fails.

==================================  ========  ====================================================================
check                               severity  the bug it points at
==================================  ========  ====================================================================
science.invalid_waveforms           critical  NaN/inf, all-zero components or > 5 g in the broadband output
science.gross_bias                  critical  units or scaling: the simulation off by > 5x from observations *and*
                                              the GMM (or > 5x in PGV, which has no GMM)
science.cross_resolution_mismatch   high      two resolutions of one rupture disagree at long periods (mesh,
                                              velocity model, source discretisation, LF scaling, site mapping)
science.period_dependent_bias       high      one half of the broadband (LF SW4 or HF stochastic) is off
science.crossover_step              high      a step in the Fourier spectrum at bb.flo: LF/HF mismatch in bb_sim
science.timing_offset               high      a clock/origin offset (distance-independent S onset error), or a
                                              lag between resolutions
science.hv_ratio                    high      components swapped or mislabelled (vertical as horizontal)
science.energy_before_p             medium    energy before the earliest possible P: a shifted or bad start
science.truncated_waveforms         medium    records cut while still shaking: duration too short
science.distance_trend              medium    attenuation/geometric spreading wrong (obs *and* GMM agree)
science.vs30_trend                  medium    site amplification wrong or applied twice (obs *and* GMM agree)
science.station_bias                medium    one station extreme the same way over >= 3 events: wrong site
                                              coordinates/Vs30, a bad instrument or a local model defect
science.source_mismatch             high      hypocentre or magnitude not the one the CMT/GeoNet gave
science.station_coverage            low       many well-recorded stations outside the simulated domain
science.unsimulated_stations        low       well-recorded stations inside the domain that were not simulated
                                              (missing from the workflow's station list)
==================================  ========  ====================================================================
"""

import json
import math
import sqlite3
from collections.abc import Iterator
from typing import Any

import numpy as np

from auto_scan.db import get_metric, metric_history, run_resolutions
from auto_scan.registry import Evaluation, Severity, check

LN5 = math.log(5.0)
LN3 = math.log(3.0)
LN2 = math.log(2.0)
# History-based bounds need at least this many earlier events.
MIN_HISTORY = 5
# A value further than this many robust sigmas from its history fails...
HISTORY_K = 4.0

# gross_bias: 5x in amplitude is ~3 within-event sigmas and 3x the largest
# ordinary event term; a units slip is 9.81x, 100x or 981x.
GROSS_BOUND = LN5
# ... and needs this many quality-ok stations per IM to trust the median.
GROSS_MIN_N = 5

# cross_resolution_mismatch: the median over long periods agreed to 0.03 on
# 2026p866048; 0.35 (x1.4) is ~7x that and ~2 robust spreads of a single site.
XRES_MEDIAN_BOUND = 0.35
# The per-site robust spread was 0.17-0.20; 0.6 means the sites no longer match.
XRES_SPREAD_BOUND = 0.6
XRES_HISTORY_FLOOR = 0.2
# Low-passed waveforms correlated at 0.99 (median); below 0.8 they are different signals.
XRES_XCORR_BOUND = 0.8
XRES_MIN_N = 20

# period_dependent_bias: LF and HF event terms differed by 0.01-0.22 on
# 2026p866048; ln 3 is ~2 within-event sigmas. The GMM must agree on the sign
# (and differ by ln 2) so that an event whose observed spectrum is odd passes.
PERIOD_BOUND = LN3
PERIOD_GMM_BOUND = LN2

# crossover_step: the natural step was +0.64 to +0.67 (robust spread ~1.0 per
# trace, so the median over >= 30 traces is good to ~0.2). ln 4 is ~3x that.
CROSSOVER_BOUND = math.log(4.0)
CROSSOVER_HISTORY_FLOOR = 0.7
CROSSOVER_MIN_N = 10

# timing_offset: the S-onset offset regressed on distance has an intercept
# (clock error) whose standard error is ~1-2 s with ~20-30 picks; 3 s and 3 se.
TIMING_INTERCEPT_BOUND_S = 3.0
TIMING_SIGNIFICANCE = 3.0
TIMING_MIN_N = 8
# Resolutions of one rupture lagged 0.03 s on 2026p866048 (dt = 0.01 s).
XRES_LAG_BOUND_S = 0.5

# hv_ratio: horizontal/vertical PGA was 2.9-3.0 (median); real records sit
# at 1.5-4. Below 1 the vertical is the strongest component; above 8 it is
# nearly missing.
HV_LOW, HV_HIGH = 1.0, 8.0

# energy_before_p / truncated_waveforms: none of 142 traces on 2026p866048;
# a fifth of the stations is a systematic problem, not a single odd site.
EARLY_FRACTION_BOUND = 0.2
TRUNCATED_FRACTION_BOUND = 0.2

# distance_trend / vs30_trend: slope per ln unit. The observed slope must be
# significant (|t| > 3) and large, and the simulation-vs-GMM slope over every
# site must have the same sign and be large too (natural range seen: +-0.4).
TREND_OBS_BOUND = 0.7
TREND_T = 3.0
TREND_GMM_BOUND = 0.5
TREND_IMS = ("PGA", "pSA_0.2", "pSA_1.0", "pSA_3.0")
TREND_MIN_N = 10
# With history, the trend must also have failed on this many of the last 4 earlier events.
TREND_PERSIST = 2

# station_bias: a within-event station term beyond ln 3 (> 2 phi_S2S, ~0.5)...
STATION_BOUND = LN3
# ... the same way on at least this many events, including the latest.
STATION_MIN_EVENTS = 3
# Per (event, IM) at least this many stations to define the event median.
STATION_MIN_PER_IM = 5

# source_mismatch: a GCMT centroid lies within ~10-15 km of GeoNet's hypocentre
# for NZ M4-6 events; 30 km is beyond location error. Depths are looser (CMT
# depth is often fixed): 25 km. All recorded magnitudes must agree to 0.05.
SOURCE_OFFSET_KM = 30.0
SOURCE_DEPTH_KM = 25.0
SOURCE_MAG_SPREAD = 0.05
SOURCE_MW_DIFF = 0.5

# station_coverage: a quarter of the well-recorded stations, and at least 10.
COVERAGE_FRACTION = 0.25
COVERAGE_MIN_N = 10


def _m(db: sqlite3.Connection, event_id: str, resolution: str, name: str) -> float | None:
    """A metric, None if absent or NULL."""
    value = get_metric(db, event_id, resolution, name)
    return None if value is None or not math.isfinite(value) else float(value)


def _detail(db: sqlite3.Connection, event_id: str, resolution: str, name: str) -> Any:
    """A metric's parsed detail, or None."""
    row = db.execute(
        "SELECT detail FROM metrics WHERE event_id = ? AND resolution = ? AND metric = ?",
        (event_id, resolution, name),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def history_outlier(
    db: sqlite3.Connection, metric: str, resolution: str, event_id: str, value: float, floor: float
) -> tuple[bool, dict[str, Any]]:
    """Whether a value lies far outside its metric's history.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    metric : str
        The metric.
    resolution : str
        Its resolution.
    event_id : str
        The event being checked (only earlier events are history).
    value : float
        The value.
    floor : float
        The smallest deviation that can fail.

    Returns
    -------
    tuple of (bool, dict)
        Whether it fails, and the history used (empty when fewer than
        ``MIN_HISTORY`` earlier events exist, in which case it never fails).
    """
    history = metric_history(db, metric, resolution, before_event=event_id)
    if len(history) < MIN_HISTORY:
        return False, {}
    values = np.array([v for _, v in history], dtype=float)
    median = float(np.median(values))
    mad = 1.4826 * float(np.median(np.abs(values - median)))
    bound = max(floor, HISTORY_K * mad)
    info = {
        "history_n": len(history),
        "history_median": round(median, 4),
        "history_mad_sigma": round(mad, 4),
        "history_bound": round(bound, 4),
        "history_last5": [(e, round(v, 4)) for e, v in history[-5:]],
    }
    return abs(value - median) > bound, info


def _r(value: float | None, digits: int = 3) -> float | None:
    """Round for evidence."""
    return None if value is None else round(value, digits)


def _xres_pairs(db: sqlite3.Connection, event_id: str) -> list[str]:
    """The resolution pairs with cross-resolution metrics for an event."""
    rows = db.execute(
        "SELECT DISTINCT metric FROM metrics WHERE event_id = ? AND resolution = '' AND metric LIKE 'science.xres.%'",
        (event_id,),
    ).fetchall()
    return sorted({row[0].split(".")[2] for row in rows})


# --------------------------------------------------------------------------- waveforms


@check
def invalid_waveforms(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Broadband traces that are NaN, all zero in a component, or above 5 g.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with waveforms.
    """
    for res in run_resolutions(db, event_id):
        n_invalid = _m(db, event_id, res, "science.wave.n_invalid")
        n = _m(db, event_id, res, "science.wave.n_stations")
        if n_invalid is None or n is None:
            continue
        detail = _detail(db, event_id, res, "science.wave.n_invalid") or {}
        yield Evaluation(
            check_id="science.invalid_waveforms",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=n_invalid > 0,
            title="Simulated broadband waveforms contain NaN, all-zero or impossibly large traces",
            summary=f"{event_id} {res}: {n_invalid:.0f} of {n:.0f} stations have invalid traces"
            f" (max |a| {_m(db, event_id, res, 'science.wave.max_abs_g')} g).",
            kind="science",
            severity="critical",
            value=n_invalid,
            threshold=0,
            evidence={
                "metrics": {"science.wave.n_invalid": n_invalid, "science.wave.n_stations": n},
                "stations": detail.get("stations"),
                "file": detail.get("file"),
                "max_abs_g_bound": 5.0,
            },
            suspect_sources=["workflow", "hf-simulation", "sw4"],
        )


@check
def hv_ratio(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Horizontal-to-vertical PGA ratio far from any real record's.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with waveforms.
    """
    for res in run_resolutions(db, event_id):
        value = _m(db, event_id, res, "science.wave.hv.median")
        if value is None:
            continue
        failing = not (HV_LOW <= value <= HV_HIGH)
        yield Evaluation(
            check_id="science.hv_ratio",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=failing,
            title="Simulated horizontal/vertical amplitude ratio is implausible (components swapped or mislabelled)",
            summary=f"{event_id} {res}: median sqrt(PGA_000 PGA_090)/PGA_ver = {value:.2f}"
            f" (plausible {HV_LOW}-{HV_HIGH}).",
            kind="science",
            severity="high",
            value=value,
            threshold=HV_LOW if value < HV_LOW else HV_HIGH,
            evidence={
                "metric": "science.wave.hv.median",
                "value": value,
                "bounds": [HV_LOW, HV_HIGH],
                "file": (_detail(db, event_id, res, "science.wave.n_stations") or {}).get("file"),
            },
            suspect_sources=["workflow", "hf-simulation", "sw4"],
        )


@check
def energy_before_p(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Strong motion before the earliest possible P arrival at many stations.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with waveforms and station distances.
    """
    for res in run_resolutions(db, event_id):
        frac = _m(db, event_id, res, "science.wave.frac_early")
        if frac is None:
            continue
        detail = _detail(db, event_id, res, "science.wave.n_early") or {}
        yield Evaluation(
            check_id="science.energy_before_p",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=frac > EARLY_FRACTION_BOUND,
            title="Simulated waveforms have energy before the earliest possible P arrival",
            summary=f"{event_id} {res}: {frac:.0%} of stations peak above 10% of their maximum before rrup/8 km/s - 1 s.",
            kind="science",
            severity="medium",
            value=frac,
            threshold=EARLY_FRACTION_BOUND,
            evidence={"metric": "science.wave.frac_early", "value": frac, "stations": detail.get("stations")},
            suspect_sources=["workflow", "hf-simulation", "sw4"],
        )


@check
def truncated_waveforms(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Many stations still shaking at the end of the simulated record.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with waveforms.
    """
    for res in run_resolutions(db, event_id):
        frac = _m(db, event_id, res, "science.wave.frac_truncated")
        if frac is None:
            continue
        detail = _detail(db, event_id, res, "science.wave.frac_truncated") or {}
        yield Evaluation(
            check_id="science.truncated_waveforms",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=frac > TRUNCATED_FRACTION_BOUND,
            title="Simulated records end while the ground is still shaking (duration too short)",
            summary=f"{event_id} {res}: {frac:.0%} of stations hold > 5% of their Arias intensity in the last 5%"
            f" of a {detail.get('duration_s')} s record.",
            kind="science",
            severity="medium",
            value=frac,
            threshold=TRUNCATED_FRACTION_BOUND,
            evidence={
                "metrics": {
                    "science.wave.frac_truncated": frac,
                    "science.wave.ai_last5.max": _m(db, event_id, res, "science.wave.ai_last5.max"),
                },
                "stations": detail.get("stations"),
                "duration_s": detail.get("duration_s"),
                "note": "duration comes from realtime's size_domain (realisation domain.duration)",
            },
            suspect_sources=["realtime", "workflow"],
        )


@check
def crossover_step(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A step in the broadband Fourier spectrum at the LF/HF crossover.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with enough traces.
    """
    for res in run_resolutions(db, event_id):
        step = _m(db, event_id, res, "science.crossover.step.median")
        n = _m(db, event_id, res, "science.crossover.step.n")
        if step is None or n is None or n < CROSSOVER_MIN_N:
            continue
        hist_fail, hist = history_outlier(
            db, "science.crossover.step.median", res, event_id, step, CROSSOVER_HISTORY_FLOOR
        )
        failing = abs(step) > CROSSOVER_BOUND or hist_fail
        detail = _detail(db, event_id, res, "science.crossover.step.median") or {}
        higher = "HF above LF" if step > 0 else "LF above HF"
        yield Evaluation(
            check_id="science.crossover_step",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=failing,
            title="Broadband spectrum has a step at the LF/HF crossover frequency",
            summary=f"{event_id} {res}: median ln step at {detail.get('flo_hz')} Hz = {step:+.2f}"
            f" ({higher}, x{math.exp(abs(step)):.1f}) over {n:.0f} traces.",
            kind="science",
            severity="high",
            value=step,
            threshold=CROSSOVER_BOUND,
            evidence={
                "metrics": {
                    "science.crossover.step.median": step,
                    "science.crossover.step.mad_sigma": _m(db, event_id, res, "science.crossover.step.mad_sigma"),
                    "science.crossover.step.n": n,
                },
                "method": "ln FAS fitted linearly in ln f on [flo/4, flo/1.25] and [1.25 flo, 4 flo], difference at flo",
                **detail,
                **hist,
            },
            suspect_sources=["workflow", "hf-simulation"],
        )


# --------------------------------------------------------------------------- residuals


def _band(db: sqlite3.Connection, event_id: str, res: str, comparison: str, band: str) -> float | None:
    return _m(db, event_id, res, f"science.{comparison}.band_median.{band}")


@check
def gross_bias(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """The simulation off by a units-like factor from the observations and the GMM.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with residuals or GMM comparisons.
    """
    for res in run_resolutions(db, event_id):
        items: dict[str, dict[str, Any]] = {}
        for band in ("lf", "hf"):
            obs = _band(db, event_id, res, "residual", band)
            gmm = _band(db, event_id, res, "gmm_all", band)
            if gmm is None:
                gmm = _band(db, event_id, res, "gmm_residual", band)
            if obs is None and gmm is None:
                continue
            if obs is not None and gmm is not None:
                failing = abs(obs) > GROSS_BOUND and abs(gmm) > GROSS_BOUND and obs * gmm > 0
            elif obs is not None:
                failing = abs(obs) > GROSS_BOUND
            else:
                failing = abs(gmm or 0.0) > GROSS_BOUND
            items[band] = {"ln_obs_over_sim": _r(obs), "ln_gmm_over_sim": _r(gmm), "failing": failing}
        pgv = _m(db, event_id, res, "science.residual.median.PGV")
        n_pgv = _m(db, event_id, res, "science.residual.n.PGV") or 0
        if pgv is not None and n_pgv >= GROSS_MIN_N:
            # PGV has no GMM: obs alone; units slips in PGV are 100x.
            items["PGV"] = {"ln_obs_over_sim": _r(pgv), "n": n_pgv, "failing": abs(pgv) > GROSS_BOUND}
        if not items:
            continue
        failing = [k for k, v in items.items() if v["failing"]]
        worst_key = max(
            items,
            key=lambda k: max(abs(items[k].get("ln_obs_over_sim") or 0), abs(items[k].get("ln_gmm_over_sim") or 0)),
        )
        worst = items[worst_key]
        value = worst.get("ln_obs_over_sim")
        if value is None:
            value = worst.get("ln_gmm_over_sim")
        suspects = ["workflow", "im-calculation"]
        if "lf" in failing:
            suspects += ["sw4", "source-modelling"]
        if "hf" in failing:
            suspects += ["hf-simulation"]
        yield Evaluation(
            check_id="science.gross_bias",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=bool(failing),
            title="Simulated amplitudes are off by a units-like factor from observations and the GMM",
            summary=f"{event_id} {res}: "
            + "; ".join(
                f"{k}: ln(obs/sim) {v.get('ln_obs_over_sim')}, ln(gmm/sim) {v.get('ln_gmm_over_sim')}"
                for k, v in items.items()
            )
            + f" (bound ln 5 = {GROSS_BOUND:.2f}).",
            kind="science",
            severity="critical",
            value=value,
            threshold=GROSS_BOUND,
            evidence={
                "bands": items,
                "metrics": [
                    "science.residual.band_median.{lf,hf}",
                    "science.gmm_all.band_median.{lf,hf}",
                    "science.residual.median.PGV",
                ],
                "band_detail": {
                    band: _detail(db, event_id, res, f"science.residual.band_median.{band}") for band in ("lf", "hf")
                },
                "sources": _detail(db, event_id, res, "science.residual.n_stations"),
            },
            suspect_sources=suspects,
        )


@check
def period_dependent_bias(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """LF-band and HF-band residual centres far apart: one half of the broadband is off.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with both bands.
    """
    for res in run_resolutions(db, event_id):
        lf, hf = _band(db, event_id, res, "residual", "lf"), _band(db, event_id, res, "residual", "hf")
        if lf is None or hf is None:
            continue
        diff = lf - hf
        glf, ghf = _band(db, event_id, res, "gmm_all", "lf"), _band(db, event_id, res, "gmm_all", "hf")
        gdiff = None if glf is None or ghf is None else glf - ghf
        gmm_agrees = gdiff is None or (gdiff * diff > 0 and abs(gdiff) > PERIOD_GMM_BOUND)
        failing = abs(diff) > PERIOD_BOUND and gmm_agrees
        lf_worse = abs(lf) >= abs(hf)
        suspects = ["sw4", "workflow", "velocity-modelling", "nzcvm"] if lf_worse else ["hf-simulation", "workflow"]
        yield Evaluation(
            check_id="science.period_dependent_bias",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=failing,
            title="Simulation is biased in one half of the broadband (LF vs HF residuals disagree)",
            summary=f"{event_id} {res}: ln(obs/sim) LF {lf:+.2f}, HF {hf:+.2f} (difference {diff:+.2f});"
            f" ln(gmm/sim) LF-HF difference {'n/a' if gdiff is None else f'{gdiff:+.2f}'};"
            f" the {'LF' if lf_worse else 'HF'} band is further off.",
            kind="science",
            severity="high",
            value=diff,
            threshold=PERIOD_BOUND,
            evidence={
                "metrics": {
                    "science.residual.band_median.lf": lf,
                    "science.residual.band_median.hf": hf,
                    "science.gmm_all.band_median.lf": glf,
                    "science.gmm_all.band_median.hf": ghf,
                },
                "lf_detail": _detail(db, event_id, res, "science.residual.band_median.lf"),
                "hf_detail": _detail(db, event_id, res, "science.residual.band_median.hf"),
                "crossover_hz": _m(db, event_id, res, "science.bb.flo"),
            },
            suspect_sources=suspects,
        )


def _trend_failing(db: sqlite3.Connection, event_id: str, res: str, variable: str) -> tuple[bool, dict[str, Any]]:
    """Whether a residual slope against ``variable`` (lnrrup or lnvs30) fails on one run."""
    per_im: dict[str, Any] = {}
    failing_ims = []
    for im in TREND_IMS:
        slope = _m(db, event_id, res, f"science.residual.slope_{variable}.{im}")
        se = _m(db, event_id, res, f"science.residual.slope_{variable}_se.{im}")
        n = _m(db, event_id, res, f"science.residual.n.{im}") or 0
        if slope is None or se is None or n < TREND_MIN_N or se <= 0:
            continue
        if variable == "lnrrup":
            gmm = _m(db, event_id, res, f"science.gmm_all.slope_lnrrup.{im}")
            if gmm is None:
                gmm = _m(db, event_id, res, f"science.gmm_residual.slope_lnrrup.{im}")
        else:
            gmm = _m(db, event_id, res, f"science.gmm_residual.slope_lnvs30.{im}")
        obs_fail = abs(slope) > TREND_OBS_BOUND and abs(slope / se) > TREND_T
        gmm_fail = gmm is None or (gmm * slope > 0 and abs(gmm) > TREND_GMM_BOUND)
        per_im[im] = {"slope": _r(slope), "se": _r(se), "n": n, "gmm_slope": _r(gmm), "failing": obs_fail and gmm_fail}
        if obs_fail and gmm_fail:
            failing_ims.append(im)
    return bool(failing_ims), per_im


def _trend_check(db: sqlite3.Connection, event_id: str, variable: str) -> Iterator[Evaluation]:
    """The distance or Vs30 trend check."""
    name = {"lnrrup": "distance", "lnvs30": "vs30"}[variable]
    for res in run_resolutions(db, event_id):
        failing, per_im = _trend_failing(db, event_id, res, variable)
        if not per_im:
            continue
        persistence: dict[str, Any] = {}
        if failing:
            earlier = [
                e for e, _ in metric_history(db, f"science.residual.slope_{variable}.{TREND_IMS[0]}", res, event_id)
            ]
            if len(earlier) >= MIN_HISTORY:
                recent = earlier[-4:]
                n_fail = sum(_trend_failing(db, e, res, variable)[0] for e in recent)
                persistence = {"earlier_events": recent, "earlier_failing": n_fail, "required": TREND_PERSIST}
                failing = n_fail >= TREND_PERSIST
        worst = max(per_im.values(), key=lambda v: abs(v["slope"] / v["se"]) if v["se"] else 0)
        unit = "ln(rrup)" if variable == "lnrrup" else "ln(vs30)"
        yield Evaluation(
            check_id=f"science.{name}_trend",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=failing,
            title=(
                "Simulation's attenuation with distance disagrees with observations and the GMM"
                if variable == "lnrrup"
                else "Simulation's site amplification against Vs30 disagrees with observations and the GMM"
            ),
            summary=f"{event_id} {res}: slope of ln(obs/sim) per {unit}: "
            + ", ".join(f"{im} {v['slope']:+.2f}+-{v['se']:.2f} (GMM {v['gmm_slope']})" for im, v in per_im.items())
            + ".",
            kind="science",
            severity="medium",
            value=worst["slope"],
            threshold=TREND_OBS_BOUND,
            evidence={
                "per_im": per_im,
                "metrics": [
                    f"science.residual.slope_{variable}.<IM>",
                    f"science.residual.slope_{variable}_se.<IM>",
                    "science.gmm_all.slope_lnrrup.<IM>"
                    if variable == "lnrrup"
                    else "science.gmm_residual.slope_lnvs30.<IM>",
                ],
                "rule": f"|slope| > {TREND_OBS_BOUND}, |t| > {TREND_T}, GMM slope same sign and > {TREND_GMM_BOUND}",
                **persistence,
            },
            suspect_sources=(
                ["sw4", "hf-simulation", "velocity-modelling", "nzcvm", "workflow"]
                if variable == "lnrrup"
                else ["workflow", "hf-simulation", "site-calculation", "sites"]
            ),
        )


@check
def distance_trend(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Residuals trending with distance, in both the observed and the GMM comparison.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with enough stations.
    """
    yield from _trend_check(db, event_id, "lnrrup")


@check
def vs30_trend(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Residuals trending with Vs30, in both the observed and the GMM comparison.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with enough stations.
    """
    yield from _trend_check(db, event_id, "lnvs30")


@check
def station_bias(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A station whose within-event residual is extreme the same way over several events.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per station of this event that has quality-ok residuals on at
        least ``STATION_MIN_EVENTS`` events up to this one.
    """
    origin = db.execute("SELECT origin_time FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if origin is None:
        return
    rows = db.execute(
        """
        SELECT r.event_id, r.resolution, r.station, r.im, r.residual, e.origin_time
        FROM station_residuals r JOIN events e USING (event_id)
        WHERE r.quality_ok = 1 AND r.residual IS NOT NULL
          AND (e.origin_time <= ? OR r.event_id = ?)
          AND r.station IN (SELECT station FROM station_residuals WHERE event_id = ? AND quality_ok = 1)
        """,
        (origin[0], event_id, event_id),
    ).fetchall()
    if not rows:
        return
    order = {"0_25hz": 0, "0_5hz": 1, "1hz": 2}
    finest: dict[str, str] = {}
    for row in rows:
        if order.get(row["resolution"], -1) > order.get(finest.get(row["event_id"], ""), -1):
            finest[row["event_id"]] = row["resolution"]
    # Event medians come from every station of that event, not only this event's.
    medians: dict[tuple[str, str], float] = {}
    for ev, res in finest.items():
        for im, values in _group(
            db.execute(
                "SELECT im, residual FROM station_residuals WHERE event_id = ? AND resolution = ? AND quality_ok = 1",
                (ev, res),
            ).fetchall()
        ).items():
            if len(values) >= STATION_MIN_PER_IM:
                medians[(ev, im)] = float(np.median(values))
    terms: dict[str, dict[str, tuple[float, str, str]]] = {}
    within: dict[tuple[str, str], list[float]] = {}
    times: dict[str, str] = {}
    for row in rows:
        ev = row["event_id"]
        if row["resolution"] != finest[ev] or (ev, row["im"]) not in medians:
            continue
        within.setdefault((row["station"], ev), []).append(row["residual"] - medians[(ev, row["im"])])
        times[ev] = row["origin_time"] or ""
    for (station, ev), values in within.items():
        terms.setdefault(station, {})[ev] = (float(np.mean(values)), finest[ev], times[ev])
    for station, per_event in sorted(terms.items()):
        if event_id not in per_event or len(per_event) < STATION_MIN_EVENTS:
            continue
        current = per_event[event_id][0]
        sign = 1.0 if current >= 0 else -1.0
        extreme = [ev for ev, (t, _, _) in per_event.items() if t * sign > STATION_BOUND]
        failing = abs(current) > STATION_BOUND and len(extreme) >= STATION_MIN_EVENTS
        listing = sorted(
            (
                {"event": ev, "resolution": res, "term": round(t, 3), "origin_time": ot}
                for ev, (t, res, ot) in per_event.items()
            ),
            key=lambda d: d["origin_time"],
        )
        yield Evaluation(
            check_id="science.station_bias",
            event_id=event_id,
            subject=station,
            failing=failing,
            title=f"Station {station} is consistently mis-simulated across events",
            summary=f"{event_id}: {station} within-event term {current:+.2f} ln units; beyond +-{STATION_BOUND:.2f}"
            f" the same way on {len(extreme)} of {len(per_event)} events.",
            kind="science",
            severity="medium",
            value=current,
            threshold=STATION_BOUND,
            evidence={
                "events": listing,
                "definition": "mean over the station's quality-ok IMs of residual minus the event's median for that"
                " IM, at the event's finest resolution (table station_residuals)",
                "query": f"SELECT * FROM station_residuals WHERE station = '{station}' AND quality_ok = 1",
            },
            suspect_sources=["sites", "velocity-modelling", "nzcvm", "workflow"],
        )


def _group(rows: list[sqlite3.Row]) -> dict[str, list[float]]:
    """(im, residual) rows grouped by IM."""
    out: dict[str, list[float]] = {}
    for im, residual in rows:
        out.setdefault(im, []).append(residual)
    return out


# --------------------------------------------------------------------------- cross-resolution and timing


@check
def cross_resolution_mismatch(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Long-period IMs (or low-passed waveforms) of one rupture disagree between resolutions.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per resolution pair compared.
    """
    for pair in _xres_pairs(db, event_id):
        prefix = f"science.xres.{pair}"
        n = _m(db, event_id, "", f"{prefix}.n") or 0
        median = _m(db, event_id, "", f"{prefix}.median")
        spread = _m(db, event_id, "", f"{prefix}.mad_sigma")
        xcorr = _m(db, event_id, "", f"{prefix}.wave.xcorr0.median")
        n_wave = _m(db, event_id, "", f"{prefix}.wave.n") or 0
        reasons = []
        hist: dict[str, Any] = {}
        if median is not None and n >= XRES_MIN_N:
            if abs(median) > XRES_MEDIAN_BOUND:
                reasons.append(f"median ln(fine/coarse) {median:+.3f} beyond +-{XRES_MEDIAN_BOUND}")
            hist_fail, hist = history_outlier(db, f"{prefix}.median", "", event_id, median, XRES_HISTORY_FLOOR)
            if hist_fail:
                reasons.append("median far outside its history")
            if spread is not None and spread > XRES_SPREAD_BOUND:
                reasons.append(f"robust spread {spread:.3f} > {XRES_SPREAD_BOUND}")
        elif xcorr is None:
            continue
        if xcorr is not None and n_wave >= 4 and xcorr < XRES_XCORR_BOUND:
            reasons.append(f"median zero-lag correlation of low-passed waveforms {xcorr:.2f} < {XRES_XCORR_BOUND}")
        detail = _detail(db, event_id, "", f"{prefix}.median") or {}
        yield Evaluation(
            check_id="science.cross_resolution_mismatch",
            event_id=event_id,
            subject=pair,
            failing=bool(reasons),
            title="Simulations of the same rupture at two resolutions disagree at long periods",
            summary=f"{event_id} {pair}: median ln(IM_fine/IM_coarse) {median if median is None else f'{median:+.3f}'}"
            f" (robust spread {spread if spread is None else f'{spread:.3f}'}, {n:.0f} sites), waveform correlation"
            f" {xcorr if xcorr is None else f'{xcorr:.3f}'}" + (f": {'; '.join(reasons)}." if reasons else "."),
            kind="science",
            severity="high",
            value=median,
            threshold=XRES_MEDIAN_BOUND,
            evidence={
                "metrics": {
                    f"{prefix}.median": median,
                    f"{prefix}.mad_sigma": spread,
                    f"{prefix}.n": n,
                    f"{prefix}.wave.xcorr0.median": xcorr,
                    f"{prefix}.wave.lag.median": _m(db, event_id, "", f"{prefix}.wave.lag.median"),
                },
                "per_period": detail,
                "sites": _detail(db, event_id, "", f"{prefix}.n"),
                "waveforms": _detail(db, event_id, "", f"{prefix}.wave.n"),
                **hist,
            },
            suspect_sources=["workflow", "sw4", "realtime", "velocity-modelling", "nzcvm", "source-modelling"],
        )


@check
def timing_offset(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A distance-independent offset of simulated S onsets from observed S picks, or a lag between resolutions.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per run with enough S picks (subject: the resolution), and one per
        resolution pair with waveforms (subject: the pair).
    """
    for res in run_resolutions(db, event_id):
        n = _m(db, event_id, res, "science.wave.s_offset.n") or 0
        intercept = _m(db, event_id, res, "science.wave.s_offset.intercept")
        se = _m(db, event_id, res, "science.wave.s_offset.intercept_se")
        if n < TIMING_MIN_N or intercept is None or se is None:
            continue
        failing = abs(intercept) > TIMING_INTERCEPT_BOUND_S and abs(intercept) > TIMING_SIGNIFICANCE * se
        detail = _detail(db, event_id, res, "science.wave.s_offset.median") or {}
        yield Evaluation(
            check_id="science.timing_offset",
            event_id=event_id,
            resolution=res,
            subject=res,
            failing=failing,
            title="Simulated arrivals are offset in time from the observed ones (clock or origin-time error)",
            summary=f"{event_id} {res}: simulated minus observed S onset at zero distance {intercept:+.2f}+-{se:.2f} s"
            f" (median over {n:.0f} stations {_m(db, event_id, res, 'science.wave.s_offset.median')} s, slope"
            f" {_m(db, event_id, res, 'science.wave.s_offset.slope')} s/km).",
            kind="science",
            severity="high",
            value=intercept,
            threshold=TIMING_INTERCEPT_BOUND_S,
            evidence={
                "metrics": {
                    k: _m(db, event_id, res, f"science.wave.s_offset.{k}")
                    for k in ("n", "median", "mad_sigma", "intercept", "intercept_se", "slope")
                },
                "rule": f"|intercept| > {TIMING_INTERCEPT_BOUND_S} s and > {TIMING_SIGNIFICANCE} se",
                "per_station": detail.get("per_station"),
                "fit_outliers": detail.get("fit_outliers"),
                "note": "t = 0 of the waveforms is the rupture start; observed picks are relative to GeoNet's origin"
                " time (phase_arrival_table.csv); onsets are corrected for the hypocentre difference",
            },
            suspect_sources=["workflow", "realtime", "source-modelling", "hf-simulation"],
        )
    for pair in _xres_pairs(db, event_id):
        lag = _m(db, event_id, "", f"science.xres.{pair}.wave.lag.median")
        if lag is None:
            continue
        yield Evaluation(
            check_id="science.timing_offset",
            event_id=event_id,
            subject=pair,
            failing=abs(lag) > XRES_LAG_BOUND_S,
            title="Simulated arrivals are offset in time from the observed ones (clock or origin-time error)",
            summary=f"{event_id} {pair}: median lag of the finer resolution's low-passed waveforms {lag:+.2f} s.",
            kind="science",
            severity="high",
            value=lag,
            threshold=XRES_LAG_BOUND_S,
            evidence={
                "metrics": {
                    f"science.xres.{pair}.wave.lag.median": lag,
                    f"science.xres.{pair}.wave.lag.mad_sigma": _m(
                        db, event_id, "", f"science.xres.{pair}.wave.lag.mad_sigma"
                    ),
                },
                "waveforms": _detail(db, event_id, "", f"science.xres.{pair}.wave.n"),
            },
            suspect_sources=["workflow", "realtime", "sw4"],
        )


# --------------------------------------------------------------------------- source and coverage


@check
def source_mismatch(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """The simulated hypocentre or magnitude is not the one GeoNet and the CMT gave.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per event with a simulated source.
    """
    offset = _m(db, event_id, "", "science.source.hypo_offset_km")
    depth = _m(db, event_id, "", "science.source.depth_diff_km")
    spread = _m(db, event_id, "", "science.source.mag_spread")
    mw_diff = _m(db, event_id, "", "science.source.mag_diff_obs")
    if offset is None and spread is None:
        return
    reasons = []
    if offset is not None and offset > SOURCE_OFFSET_KM:
        reasons.append(f"hypocentre {offset:.1f} km from GeoNet's (> {SOURCE_OFFSET_KM} km)")
    if depth is not None and abs(depth) > SOURCE_DEPTH_KM:
        reasons.append(f"depth differs by {depth:+.1f} km (> {SOURCE_DEPTH_KM} km)")
    if spread is not None and spread > SOURCE_MAG_SPREAD:
        reasons.append(f"magnitudes recorded for the simulation differ by {spread:.2f}")
    if mw_diff is not None and abs(mw_diff) > SOURCE_MW_DIFF:
        reasons.append(f"Mw differs from GeoNet's moment magnitude by {mw_diff:+.2f}")
    obs_detail = _detail(db, event_id, "", "science.source.mag_obs") or {}
    yield Evaluation(
        check_id="science.source_mismatch",
        event_id=event_id,
        failing=bool(reasons),
        title="Simulated source does not match GeoNet's hypocentre or the CMT magnitude",
        summary=f"{event_id}: hypocentre offset {_r(offset, 1)} km, depth difference {_r(depth, 1)} km,"
        f" simulated Mw {_r(_m(db, event_id, '', 'science.source.mag_sim'), 2)} (GeoNet"
        f" {_m(db, event_id, '', 'science.source.mag_obs')} {obs_detail.get('mag_type', '')})"
        + (f": {'; '.join(reasons)}." if reasons else "."),
        kind="science",
        severity="high",
        value=offset,
        threshold=SOURCE_OFFSET_KM,
        evidence={
            "hypocentre": _detail(db, event_id, "", "science.source.hypo_offset_km"),
            "magnitudes": (_detail(db, event_id, "", "science.source.mag_sim") or {}).get("sources"),
            "geonet_magnitude": obs_detail,
            "bounds": {
                "offset_km": SOURCE_OFFSET_KM,
                "depth_km": SOURCE_DEPTH_KM,
                "mag_spread": SOURCE_MAG_SPREAD,
                "mw_diff": SOURCE_MW_DIFF,
            },
            "note": "an ML-vs-Mw difference is not compared (ML is often well above Mw for NZ events)",
        },
        suspect_sources=["workflow", "source-modelling", "realtime"],
    )


def _coverage_eval(
    db: sqlite3.Connection,
    event_id: str,
    check_id: str,
    failing: bool,
    title: str,
    summary: str,
    value: float,
    threshold: float,
    metric: str,
    suspects: list[str],
    severity: Severity = "low",
) -> Evaluation:
    """An evaluation of a coverage check."""
    return Evaluation(
        check_id=check_id,
        event_id=event_id,
        failing=failing,
        title=title,
        summary=summary,
        kind="science",
        severity=severity,
        value=value,
        threshold=threshold,
        evidence={
            "metric": metric,
            "value": value,
            "stations": (_detail(db, event_id, "", metric) or {}).get("stations"),
            "counts": {
                k: _m(db, event_id, "", f"science.coverage.{k}")
                for k in ("n_observed", "n_observed_good", "n_well_recorded", "n_sim_real", "n_joined")
            },
            "well_recorded": "quality-ok record with PGA >= 0.002 g",
        },
        suspect_sources=suspects,
    )


@check
def station_coverage(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Many well-recorded stations fall outside every simulated domain.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per event with observations and a domain.
    """
    n = _m(db, event_id, "", "science.coverage.n_good_outside")
    frac = _m(db, event_id, "", "science.coverage.frac_good_outside")
    if n is None or frac is None:
        return
    yield _coverage_eval(
        db,
        event_id,
        "science.station_coverage",
        n >= COVERAGE_MIN_N and frac > COVERAGE_FRACTION,
        "Many well-recorded stations lie outside the simulated domain",
        f"{event_id}: {n:.0f} well-recorded stations ({frac:.0%}) are outside every run's domain.",
        frac,
        COVERAGE_FRACTION,
        "science.coverage.n_good_outside",
        ["realtime"],
    )


@check
def unsimulated_stations(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Well-recorded stations inside a domain that no run simulated.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One per event with observations and a domain.
    """
    n = _m(db, event_id, "", "science.coverage.n_good_inside_unsimulated")
    if n is None:
        return
    yield _coverage_eval(
        db,
        event_id,
        "science.unsimulated_stations",
        n >= 1,
        "Well-recorded stations inside the simulated domain were not simulated (missing from the station list)",
        f"{event_id}: {n:.0f} well-recorded stations inside a run's domain (clear of its absorbing layer) have no"
        " simulated record.",
        n,
        1,
        "science.coverage.n_good_inside_unsimulated",
        ["sites", "realtime", "workflow"],
    )
