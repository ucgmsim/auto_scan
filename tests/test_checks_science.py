"""Tests of the science checks: each fails on a planted bug and passes on a clean run.

The checks read only the database, so each test writes the metrics (and, for
station_bias, station_residuals) a clean or a buggy run would produce.
"""

import math
import sqlite3
from collections.abc import Iterable
from typing import Any

import pytest

from auto_scan import db as scandb
from auto_scan.checks import science as checks
from auto_scan.registry import Evaluation

EVENT = "2026p000010"


def database(events: Iterable[str] = (EVENT,), resolutions: Iterable[str] = ("0_5hz", "1hz")) -> sqlite3.Connection:
    """A scan database with events one day apart and runs at each resolution."""
    con = scandb.connect(":memory:")
    for day, event in enumerate(events, start=1):
        con.execute(
            "INSERT INTO events (event_id, cycle, event_dir, origin_time) VALUES (?, ?, ?, ?)",
            (event, day, f"{day}-{event}", f"2026-01-{day:02d}T00:00:00Z"),
        )
        for res in resolutions:
            con.execute("INSERT INTO runs (event_id, resolution) VALUES (?, ?)", (event, res))
    return con


def put(con: sqlite3.Connection, values: dict[str, Any], res: str = "1hz", event: str = EVENT) -> None:
    for name, value in values.items():
        detail = None
        if isinstance(value, tuple):
            value, detail = value
        scandb.set_metric(con, event, res, name, value, None, detail)


def run(check: Any, con: sqlite3.Connection, event: str = EVENT) -> list[Evaluation]:
    evaluations = list(check(con, event))
    for e in evaluations:
        assert e.title and e.summary and e.kind == "science" and e.suspect_sources
        assert EVENT not in e.title  # titles are not event specific
    return evaluations


def only(evaluations: list[Evaluation], subject: str = "1hz") -> Evaluation:
    matching = [e for e in evaluations if e.subject == subject]
    assert len(matching) == 1, evaluations
    return matching[0]


def test_nothing_without_inputs() -> None:
    con = database()
    for check in (
        checks.invalid_waveforms,
        checks.hv_ratio,
        checks.energy_before_p,
        checks.truncated_waveforms,
        checks.crossover_step,
        checks.gross_bias,
        checks.period_dependent_bias,
        checks.distance_trend,
        checks.vs30_trend,
        checks.station_bias,
        checks.cross_resolution_mismatch,
        checks.timing_offset,
        checks.source_mismatch,
        checks.event_term,
        checks.station_coverage,
        checks.unsimulated_stations,
    ):
        assert run(check, con) == []


@pytest.mark.parametrize(("n_invalid", "failing"), [(0, False), (2, True)])
def test_invalid_waveforms(n_invalid: int, failing: bool) -> None:
    con = database()
    put(
        con,
        {
            "science.wave.n_invalid": (n_invalid, {"stations": {"AAA": "non-finite samples"}}),
            "science.wave.n_stations": 30,
        },
    )
    e = only(run(checks.invalid_waveforms, con))
    assert e.failing is failing and e.severity == "critical"


@pytest.mark.parametrize(("hv", "failing"), [(2.9, False), (0.4, True), (20.0, True)])
def test_hv_ratio(hv: float, failing: bool) -> None:
    con = database()
    put(con, {"science.wave.hv.median": hv})
    assert only(run(checks.hv_ratio, con)).failing is failing


@pytest.mark.parametrize(("frac", "failing"), [(0.0, False), (0.6, True)])
def test_energy_before_p(frac: float, failing: bool) -> None:
    con = database()
    put(con, {"science.wave.frac_early": frac})
    assert only(run(checks.energy_before_p, con)).failing is failing


@pytest.mark.parametrize(("frac", "failing"), [(0.02, False), (0.5, True)])
def test_truncated_waveforms(frac: float, failing: bool) -> None:
    con = database()
    put(con, {"science.wave.frac_truncated": (frac, {"duration_s": 51.1, "stations": {}})})
    e = only(run(checks.truncated_waveforms, con))
    assert e.failing is failing and "realtime" in e.suspect_sources


@pytest.mark.parametrize(("step", "failing"), [(0.65, False), (math.log(10), True), (-2.0, True)])
def test_crossover_step(step: float, failing: bool) -> None:
    con = database()
    put(con, {"science.crossover.step.median": (step, {"flo_hz": 1.0}), "science.crossover.step.n": 200})
    assert only(run(checks.crossover_step, con)).failing is failing


def test_crossover_step_history() -> None:
    events = [f"2026p00000{i}" for i in range(6)] + [EVENT]
    con = database(events)
    for i, event in enumerate(events[:-1]):
        put(con, {"science.crossover.step.median": 0.6 + 0.02 * (i % 3), "science.crossover.step.n": 200}, event=event)
    put(con, {"science.crossover.step.median": 1.35, "science.crossover.step.n": 200})
    e = only(run(checks.crossover_step, con))
    assert e.failing and e.evidence["history_n"] == 6
    put(con, {"science.crossover.step.median": 0.7})
    assert not only(run(checks.crossover_step, con)).failing


def _bands(lf: float, hf: float, glf: float | None, ghf: float | None, pgv: float = 0.1) -> dict[str, Any]:
    values: dict[str, Any] = {
        "science.residual.band_median.lf": lf,
        "science.residual.band_median.hf": hf,
        "science.residual.median.PGV": pgv,
        "science.residual.n.PGV": 30,
    }
    if glf is not None:
        values["science.gmm_all.band_median.lf"] = glf
    if ghf is not None:
        values["science.gmm_all.band_median.hf"] = ghf
    return values


@pytest.mark.parametrize(
    ("bands", "failing"),
    [
        (_bands(0.3, -0.2, -0.4, -0.1), False),  # ordinary misfit
        (_bands(-6.9, -6.8, -6.9, -6.9), True),  # sim 981x too large: units
        (_bands(-6.9, 0.1, 0.2, 0.1), False),  # obs odd, GMM agrees with sim: the event, not a bug
        (_bands(0.1, 0.1, None, None, pgv=4.6), True),  # PGV 100x: m/s vs cm/s
        (_bands(0.1, 2.3, None, None), True),  # no GMM: obs alone
    ],
)
def test_gross_bias(bands: dict[str, Any], failing: bool) -> None:
    con = database()
    put(con, bands)
    e = only(run(checks.gross_bias, con))
    assert e.failing is failing and e.severity == "critical"


def test_gross_bias_without_observations_uses_gmm() -> None:
    con = database()
    put(con, {"science.gmm_all.band_median.lf": 3.0, "science.gmm_all.band_median.hf": 0.1})
    assert only(run(checks.gross_bias, con)).failing


@pytest.mark.parametrize(
    ("bands", "failing", "suspect"),
    [
        (_bands(0.3, 0.1, -0.3, -0.1), False, None),
        (_bands(-1.6, 0.1, -1.4, 0.0), True, "sw4"),  # LF half off
        (_bands(0.1, 1.5, 0.0, 1.2), True, "hf-simulation"),  # HF half off
        (_bands(-1.6, 0.1, 0.2, 0.0), False, None),  # GMM disagrees: event-specific
    ],
)
def test_period_dependent_bias(bands: dict[str, Any], failing: bool, suspect: str | None) -> None:
    con = database()
    put(con, bands)
    e = only(run(checks.period_dependent_bias, con))
    assert e.failing is failing
    if suspect:
        assert e.suspect_sources[0] == suspect


def _trend(variable: str, slope: float, se: float, gmm: float, event: str = EVENT) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for im in checks.TREND_IMS:
        values[f"science.residual.slope_{variable}.{im}"] = slope
        values[f"science.residual.slope_{variable}_se.{im}"] = se
        values[f"science.residual.n.{im}"] = 40
        gmm_name = (
            f"science.gmm_all.slope_lnrrup.{im}" if variable == "lnrrup" else f"science.gmm_residual.slope_lnvs30.{im}"
        )
        values[gmm_name] = gmm
    return values


@pytest.mark.parametrize("check_name", ["distance_trend", "vs30_trend"])
@pytest.mark.parametrize(
    ("slope", "se", "gmm", "failing"),
    [
        (0.3, 0.2, 0.1, False),  # ordinary
        (1.2, 0.2, 0.9, True),  # obs and GMM both see it
        (1.2, 0.2, 0.1, False),  # GMM does not: the event's own path/site effects
        (1.2, 0.6, 0.9, False),  # not significant
    ],
)
def test_trends(check_name: str, slope: float, se: float, gmm: float, failing: bool) -> None:
    con = database()
    variable = "lnrrup" if check_name == "distance_trend" else "lnvs30"
    put(con, _trend(variable, slope, se, gmm))
    assert only(run(getattr(checks, check_name), con)).failing is failing


@pytest.mark.parametrize("check_name", ["distance_trend", "vs30_trend"])
def test_trend_without_gmm_slope_is_not_corroborated(check_name: str) -> None:
    # 2026p742111 1hz PGA: -0.842 +- 0.280 (t = 3.0) with no GMM slope; the other IMs ordinary.
    con = database()
    variable = "lnrrup" if check_name == "distance_trend" else "lnvs30"
    values = _trend(variable, -0.3, 0.2, 0.1)
    gmm_name = "science.gmm_all.slope_lnrrup" if variable == "lnrrup" else "science.gmm_residual.slope_lnvs30"
    values[f"science.residual.slope_{variable}.PGA"] = -0.85
    values[f"science.residual.slope_{variable}_se.PGA"] = 0.28
    del values[f"{gmm_name}.PGA"]
    put(con, values)
    e = only(run(getattr(checks, check_name), con))
    assert not e.failing
    assert e.evidence["per_im"]["PGA"] == {"slope": -0.85, "se": 0.28, "n": 40, "gmm_slope": None, "failing": False}
    # The same observed slope, corroborated by the GMM, still fails.
    put(con, {f"{gmm_name}.PGA": -0.9})
    assert only(run(getattr(checks, check_name), con)).failing


def test_trend_needs_persistence_with_history() -> None:
    events = [f"2026p00000{i}" for i in range(6)] + [EVENT]
    con = database(events)
    for event in events[:-1]:
        put(con, _trend("lnrrup", 0.1, 0.2, 0.1, event), event=event)
    put(con, _trend("lnrrup", 1.2, 0.2, 0.9))
    e = only(run(checks.distance_trend, con))
    assert not e.failing and e.evidence["earlier_failing"] == 0
    for event in events[-3:-1]:
        put(con, _trend("lnrrup", 1.2, 0.2, 0.9, event), event=event)
    assert only(run(checks.distance_trend, con)).failing


def _station_rows(con: sqlite3.Connection, event: str, biased: dict[str, float]) -> None:
    rows = []
    for k in range(8):
        station = f"S{k}"
        for im in ("PGA", "pSA_1.0"):
            residual = 0.1 * ((k * 3) % 5 - 2) + biased.get(station, 0.0)
            rows.append((event, "1hz", station, im, 1.0, 1.0, None, residual, 50.0, 400.0, 1))
    con.executemany("INSERT INTO station_residuals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def test_station_bias() -> None:
    events = ["2026p000001", "2026p000002", "2026p000003", EVENT]
    con = database(events, ("1hz",))
    for event in events:
        _station_rows(con, event, {"S3": 1.8})
    evaluations = run(checks.station_bias, con)
    assert only(evaluations, "S3").failing
    assert len(only(evaluations, "S3").evidence["events"]) == 4
    assert not only(evaluations, "S1").failing


def test_station_bias_one_off_passes() -> None:
    events = ["2026p000001", "2026p000002", "2026p000003", EVENT]
    con = database(events, ("1hz",))
    for event in events:
        _station_rows(con, event, {"S3": 1.8} if event == EVENT else {})
    assert not only(run(checks.station_bias, con), "S3").failing


def test_station_bias_needs_three_events() -> None:
    con = database(["2026p000001", EVENT], ("1hz",))
    for event in ("2026p000001", EVENT):
        _station_rows(con, event, {"S3": 1.8})
    assert run(checks.station_bias, con) == []


def _xres(median: float, spread: float = 0.18, xcorr: float = 0.99, lag: float = 0.03) -> dict[str, Any]:
    p = "science.xres.0_5hz-1hz"
    return {
        f"{p}.n": 7000,
        f"{p}.median": median,
        f"{p}.mad_sigma": spread,
        f"{p}.wave.n": 60,
        f"{p}.wave.xcorr0.median": xcorr,
        f"{p}.wave.lag.median": lag,
    }


@pytest.mark.parametrize(
    ("values", "failing"),
    [
        (_xres(-0.03), False),
        (_xres(math.log(2)), True),
        (_xres(0.0, spread=1.0), True),
        (_xres(0.0, xcorr=0.3), True),
    ],
)
def test_cross_resolution_mismatch(values: dict[str, Any], failing: bool) -> None:
    con = database()
    put(con, values, res="")
    e = only(run(checks.cross_resolution_mismatch, con), "0_5hz-1hz")
    assert e.failing is failing and e.severity == "high"


@pytest.mark.parametrize(("intercept", "se", "failing"), [(-0.5, 0.5, False), (5.0, 0.6, True), (5.0, 2.5, False)])
def test_timing_offset_from_picks(intercept: float, se: float, failing: bool) -> None:
    con = database()
    put(
        con,
        {
            "science.wave.s_offset.n": 25,
            "science.wave.s_offset.intercept": intercept,
            "science.wave.s_offset.intercept_se": se,
            "science.wave.s_offset.median": intercept - 2.0,
        },
    )
    assert only(run(checks.timing_offset, con)).failing is failing


def _s_offsets(
    corrected: float, uncorrected: float, pick_geonet: float | None, pick_sim: float | None, se: float = 0.5
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "science.wave.s_offset.n": 15,
        "science.wave.s_offset.median": corrected - 2.0,
        "science.wave.s_offset.intercept": corrected,
        "science.wave.s_offset.intercept_se": se,
        "science.wave.s_offset.intercept_uncorrected": uncorrected,
        "science.wave.s_offset.intercept_uncorrected_se": se,
    }
    if pick_geonet is not None:
        values["science.wave.s_pick.intercept_geonet"] = pick_geonet
    if pick_sim is not None:
        values["science.wave.s_pick.intercept_simulated"] = pick_sim
    return values


@pytest.mark.parametrize(
    ("values", "failing", "judged_on"),
    [
        # 2026p742111: picks follow the simulated 28.5 km source, not GeoNet's 10 km hypocentre.
        (_s_offsets(-3.64, 0.29, 3.91, 0.67), False, "simulated"),
        # ... and the same with a 5 s clock error: the hypocentre bias must not hide it.
        (_s_offsets(1.36, 5.29, 3.91, 0.67), True, "simulated"),
        # GeoNet's hypocentre right, the simulated source misplaced: the corrected intercept counts.
        (_s_offsets(0.2, -4.0, 0.3, -3.5), False, "GeoNet"),
        (_s_offsets(5.2, 1.0, 0.3, -3.5), True, "GeoNet"),
        # Hypocentres agree: both the same.
        (_s_offsets(5.0, 5.1, 0.1, 0.1), True, "GeoNet"),
        # Picks unknown: fail only when both intercepts do, with the same sign.
        (_s_offsets(-3.64, 0.29, None, None), False, "both"),
        (_s_offsets(5.0, -5.0, None, None), False, "both"),
        (_s_offsets(5.0, 6.0, None, None), True, "both"),
    ],
)
def test_timing_offset_judged_in_the_geometry_the_picks_follow(
    values: dict[str, Any], failing: bool, judged_on: str
) -> None:
    con = database()
    put(con, values)
    e = only(run(checks.timing_offset, con))
    assert e.failing is failing
    assert judged_on in e.evidence["judged_on"]
    assert (
        e.evidence["metrics"]["science.wave.s_offset.intercept_uncorrected"]
        == values["science.wave.s_offset.intercept_uncorrected"]
    )


def test_timing_offset_without_geonet_hypocentre_uses_uncorrected() -> None:
    con = database()
    values = _s_offsets(0.0, 6.0, None, None)
    del values["science.wave.s_offset.intercept"]
    put(con, values)
    e = only(run(checks.timing_offset, con))
    assert e.failing and e.value == 6.0


@pytest.mark.parametrize(("lag", "failing"), [(0.03, False), (2.0, True)])
def test_timing_offset_between_resolutions(lag: float, failing: bool) -> None:
    con = database()
    put(con, _xres(0.0, lag=lag), res="")
    assert only(run(checks.timing_offset, con), "0_5hz-1hz").failing is failing


@pytest.mark.parametrize(
    ("values", "failing"),
    [
        (
            {
                "science.source.hypo_offset_km": 3.9,
                "science.source.depth_diff_km": 5.0,
                "science.source.mag_spread": 0.0,
            },
            False,
        ),
        ({"science.source.hypo_offset_km": 80.0, "science.source.mag_spread": 0.0}, True),
        ({"science.source.hypo_offset_km": 3.0, "science.source.mag_spread": 0.4}, True),
        ({"science.source.hypo_offset_km": 3.0, "science.source.mag_diff_obs": 0.9}, True),
    ],
)
def test_source_mismatch(values: dict[str, Any], failing: bool) -> None:
    con = database()
    put(con, values, res="")
    assert only(run(checks.source_mismatch, con), "").failing is failing


def _event_term(
    hf: tuple[float, float | None, float | None],
    lf: tuple[float, float | None, float | None] | None = None,
    n: int = 19,
) -> dict[str, Any]:
    """Band medians ``(ln obs/sim, ln obs/gmm, ln gmm/sim)`` per band, over ``n`` stations."""
    values: dict[str, Any] = {}
    for band, triple, im in (("hf", hf, "PGA"), ("lf", lf, "pSA_3.0")):
        if triple is None:
            continue
        obs_sim, obs_gmm, gmm_sim = triple
        values[f"science.residual.band_median.{band}"] = (obs_sim, {"per_im_median": {im: obs_sim}, "flo_hz": 1.0})
        values[f"science.residual.n.{im}"] = n
        if obs_gmm is not None:
            values[f"science.obs_gmm_residual.band_median.{band}"] = obs_gmm
        if gmm_sim is not None:
            values[f"science.gmm_residual.band_median.{band}"] = gmm_sim
    return values


def _event_source(con: sqlite3.Connection) -> None:
    put(
        con,
        {
            "science.source.mag_sim": 4.50,
            "science.source.mag_obs": (5.29, {"mag_type": "ML"}),
            "science.source.depth_diff_km": 18.4,
            "science.source.hypo_offset_km": 7.3,
        },
        res="",
    )


@pytest.mark.parametrize(
    ("hf", "lf", "failing"),
    [
        ((1.43, 1.03, 0.31), (0.79, 1.14, -0.41), True),  # 2026p742111: obs above sim and GMM, which agree
        ((-2.12, -2.09, 0.07), None, True),  # 2026p778829: no LF band, HF alone
        ((0.96, 0.26, 0.73), (0.79, 0.10, 0.49), False),  # 2026p806127: obs above sim but near the GMM
        ((0.06, 0.21, -0.06), (0.28, 0.85, -0.44), False),  # 2026p866048: obs near sim
        ((1.2, -1.1, 2.3), None, False),  # opposite signs
        ((0.4, 0.3, 0.1), (-0.3, -0.2, -0.1), False),  # ordinary event terms
        ((2.5, 0.8, 1.7), None, False),  # sim far from the GMM too: the simulation, not the source
        ((1.4, 1.1, 0.3), (-1.2, -1.0, -0.2), False),  # bands disagree: spectral shape, not the source
    ],
)
def test_event_term(hf: tuple[float, float, float], lf: tuple[float, float, float] | None, failing: bool) -> None:
    con = database(resolutions=("1hz",))
    _event_source(con)
    put(con, _event_term(hf, lf))
    e = only(run(checks.event_term, con), "")
    assert e.failing is failing and e.severity == "high" and e.resolution == ""
    assert "source-modelling" in e.suspect_sources
    assert e.evidence["source"]["geonet_mag_type"] == "ML" and e.evidence["source"]["depth_diff_km"] == 18.4
    if failing:
        assert math.copysign(1, e.value or 0) == math.copysign(1, hf[0]) and abs(e.value or 0) > checks.LN2
        assert "source" in e.summary and "GMM" in e.title
        change = e.evidence["approximate_source_change"]["hf"]
        assert change["delta_mw"][0] * hf[0] > 0 and change["moment_factor"][0] > 0


def test_event_term_small_n_is_not_evaluated() -> None:
    con = database(resolutions=("1hz",))
    put(con, _event_term((1.43, 1.03, 0.31), (0.79, 1.14, -0.41), n=3))
    assert run(checks.event_term, con) == []


def test_event_term_without_gmm_comparison_is_not_evaluated() -> None:
    con = database(resolutions=("1hz",))
    # obs/sim far off but no obs-vs-GMM or GMM-vs-sim medians: nothing to tell the source from the simulation.
    put(con, _event_term((2.0, None, None), (1.8, None, None)))
    assert run(checks.event_term, con) == []


@pytest.mark.parametrize(("n_bad", "failing"), [(1, False), (2, True), (3, True)])
def test_event_term_one_evaluation_per_event(n_bad: int, failing: bool) -> None:
    resolutions = ("0_25hz", "0_5hz", "1hz")
    con = database(resolutions=resolutions)
    for i, res in enumerate(resolutions):
        bad = i >= len(resolutions) - n_bad
        put(con, _event_term((1.3, 1.05, 0.2) if bad else (0.3, 0.2, 0.1)), res=res)
    e = only(run(checks.event_term, con), "")
    assert e.failing is failing and set(e.evidence["runs"]) == set(resolutions)
    assert sum(r["failing"] for r in e.evidence["runs"].values()) == n_bad


@pytest.mark.parametrize(("n", "frac", "failing"), [(3, 0.04, False), (30, 0.5, True), (5, 0.5, False)])
def test_station_coverage(n: int, frac: float, failing: bool) -> None:
    con = database()
    put(con, {"science.coverage.n_good_outside": n, "science.coverage.frac_good_outside": frac}, res="")
    e = only(run(checks.station_coverage, con), "")
    assert e.failing is failing and e.severity == "low"


@pytest.mark.parametrize(("n", "failing"), [(0, False), (2, True)])
def test_unsimulated_stations(n: int, failing: bool) -> None:
    con = database()
    put(con, {"science.coverage.n_good_inside_unsimulated": (n, {"stations": ["ASBS", "IFMZ"][:n]})}, res="")
    e = only(run(checks.unsimulated_stations, con), "")
    assert e.failing is failing and "sites" in e.suspect_sources
