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
