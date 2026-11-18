"""Tests for auto_scan.checks.perf: each check on a failing and a passing run."""

import sqlite3
from collections.abc import Iterable, Iterator
from typing import Any

import pytest

from auto_scan.checks import perf as checks
from auto_scan.db import connect, set_metric
from auto_scan.registry import Evaluation

RES = "0_5hz"


@pytest.fixture
def scan() -> Iterator[sqlite3.Connection]:
    """A scan database with eight events a day apart, ev0 oldest."""
    db = connect(":memory:")
    for day in range(8):
        db.execute(
            "INSERT INTO events (event_id, cycle, event_dir, origin_time) VALUES (?, ?, ?, ?)",
            (f"ev{day}", day, f"{day}-ev{day}", f"2026-11-{day + 1:02d}T00:00:00+00:00"),
        )
        db.execute("INSERT INTO runs (event_id, resolution, status) VALUES (?, ?, 'succeeded')", (f"ev{day}", RES))
        db.execute(
            "INSERT INTO run_sources (event_id, resolution, source, commit_sha) VALUES (?, ?, 'sw4', ?)",
            (f"ev{day}", RES, f"sha{day}"),
        )
    yield db
    db.close()


def put(db: sqlite3.Connection, event: str, metrics: dict[str, float], detail: Any = None) -> None:
    for name, value in metrics.items():
        set_metric(db, event, RES, name, value, None, detail)


def only(evaluations: Iterable[Evaluation]) -> Evaluation:
    found = list(evaluations)
    assert len(found) == 1, found
    return found[0]


def test_nothing_without_metrics(scan: sqlite3.Connection) -> None:
    for check in (
        checks.cost_model_overestimate,
        checks.throughput_regression,
        checks.mpi_wait_high,
        checks.binding_wrong,
        checks.time_to_results,
    ):
        assert list(check(scan, "ev7")) == []


def test_cost_model_overestimate(scan: sqlite3.Connection) -> None:
    put(
        scan,
        "ev7",
        {
            "perf.cost_ratio": 5.1,
            "perf.domain_fraction": 0.21,
            "perf.domain_fraction_measured": 0.7,
            "perf.model.speed_spread": 0.88,
            "perf.model.points_factor_spread": 0.11,
        },
    )
    evaluation = only(checks.cost_model_overestimate(scan, "ev7"))
    assert evaluation.failing and evaluation.subject == RES and evaluation.severity == "high"
    assert evaluation.evidence["largest_caution"] == "speed"
    assert "70%" in evaluation.summary
    # the same over-estimate on a domain that was not cut costs nothing
    put(scan, "ev6", {"perf.cost_ratio": 5.1, "perf.domain_fraction": 1.0})
    assert not only(checks.cost_model_overestimate(scan, "ev6")).failing
    put(scan, "ev5", {"perf.cost_ratio": 1.6, "perf.domain_fraction": 0.5})
    assert not only(checks.cost_model_overestimate(scan, "ev5")).failing


def test_cost_model_underestimate(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.cost_ratio": 0.6})
    assert only(checks.cost_model_underestimate(scan, "ev7")).failing
    put(scan, "ev6", {"perf.cost_ratio": 1.4})
    assert not only(checks.cost_model_underestimate(scan, "ev6")).failing


def test_throughput_absolute_floor_before_history(scan: sqlite3.Connection) -> None:
    put(
        scan,
        "ev1",
        {
            "perf.updates_per_core_s": 5.0e4,
            "perf.cores": 96,
            "perf.job.concurrent_sw4_cores": 0,
            "perf.job.shared_node_fraction": 0.0,
        },
    )
    evaluation = only(checks.throughput_regression(scan, "ev1"))
    assert evaluation.failing and evaluation.threshold == checks.THROUGHPUT_FLOOR
    # quiet cluster, ranks placed right: the solver is the suspect
    assert evaluation.suspect_sources == ["sw4"]
    assert not only(checks.contention_slowdown(scan, "ev1")).failing


def test_throughput_against_history(scan: sqlite3.Connection) -> None:
    for day in range(7):
        put(scan, f"ev{day}", {"perf.updates_per_core_s": 6.0e5})
    put(scan, "ev7", {"perf.updates_per_core_s": 3.0e5, "perf.cores": 96, "perf.job.concurrent_sw4_cores": 192})
    evaluation = only(checks.throughput_regression(scan, "ev7"))
    assert evaluation.failing
    assert evaluation.threshold == pytest.approx(6.0e5 * checks.THROUGHPUT_DROP)
    assert evaluation.suspect_sources == ["realtime"]
    assert evaluation.evidence["history_sw4_commits"]["ev6"] == "sha6"
    assert only(checks.contention_slowdown(scan, "ev7")).failing
    put(scan, "ev7", {"perf.updates_per_core_s": 5.5e5})
    assert not only(checks.throughput_regression(scan, "ev7")).failing
    assert not only(checks.contention_slowdown(scan, "ev7")).failing


def test_contention_slowdown_needs_job_metrics(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.updates_per_core_s": 1.0e4})
    assert list(checks.contention_slowdown(scan, "ev7")) == []


def test_mpi_wait_high(scan: sqlite3.Connection) -> None:
    put(
        scan,
        "ev7",
        {
            "perf.mpi_fraction": 0.52,
            "perf.mpi_share.barrier": 0.29,
            "perf.mpi_share.sendrecv": 0.12,
            "perf.rank_imbalance": 1.9,
        },
    )
    evaluation = only(checks.mpi_wait_high(scan, "ev7"))
    assert evaluation.failing and evaluation.suspect_sources == ["realtime"]
    assert "barrier" in evaluation.summary
    put(scan, "ev6", {"perf.mpi_fraction": 0.29, "perf.mpi_share.sendrecv": 0.24})
    evaluation = only(checks.mpi_wait_high(scan, "ev6"))
    assert not evaluation.failing


def test_rank_imbalance(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.rank_imbalance": 1.92, "perf.placement.narrow_ranks": 3})
    evaluation = only(checks.rank_imbalance(scan, "ev7"))
    assert evaluation.failing and evaluation.suspect_sources == ["realtime"]
    put(scan, "ev6", {"perf.rank_imbalance": 1.2})
    assert not only(checks.rank_imbalance(scan, "ev6")).failing


def test_interface_solve_cost(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.share.enforce_ic": 0.28, "perf.interface_jacobi_per_step": 30})
    assert not only(checks.interface_solve_cost(scan, "ev7")).failing
    put(scan, "ev6", {"perf.share.enforce_ic": 0.45, "perf.interface_jacobi_per_step": 30})
    assert only(checks.interface_solve_cost(scan, "ev6")).failing
    # growth against history: Jacobi iterations up 50%
    for day in range(6):
        put(scan, f"ev{day}", {"perf.share.enforce_ic": 0.2, "perf.interface_jacobi_per_step": 20})
    put(scan, "ev7", {"perf.share.enforce_ic": 0.22, "perf.interface_jacobi_per_step": 30})
    evaluation = only(checks.interface_solve_cost(scan, "ev7"))
    assert evaluation.failing and evaluation.evidence["jacobi_limit"] == pytest.approx(26)


def test_interface_solve_cost_with_misplaced_ranks(scan: sqlite3.Connection) -> None:
    # 2026p806127 (0.5 Hz, SW4 e2e904d5): 64% in enforceIC, 93% of it inside MPI, waiting on three
    # ranks with 2 cores for 4 threads; that SW4 build had no consintp/jacobi timers.
    put(scan, "ev7", {"perf.share.enforce_ic": 0.64, "perf.placement.narrow_ranks": 3})
    evaluation = only(checks.interface_solve_cost(scan, "ev7"))
    assert not evaluation.failing
    assert "not timed" in evaluation.summary and "0.0 Jacobi" not in evaluation.summary
    assert "misplaced" in evaluation.summary
    # Its Jacobi iterations are still judged.
    put(scan, "ev7", {"perf.interface_jacobi_per_step": 90})
    assert only(checks.interface_solve_cost(scan, "ev7")).failing


def test_io_share_high_ignores_misplaced_history(scan: sqlite3.Connection) -> None:
    # 2026p866048: 8.8% I/O, against six earlier runs whose walls misplaced ranks stretched 3-20x.
    for day, share in enumerate([0.033, 0.004, 0.022, 0.003, 0.006, 0.031]):
        put(scan, f"ev{day}", {"perf.io_share": share, "perf.placement.overlapping_ranks": 8})
    put(scan, "ev7", {"perf.io_share": 0.088})
    evaluation = only(checks.io_share_high(scan, "ev7"))
    assert not evaluation.failing and evaluation.threshold == pytest.approx(checks.IO_SHARE_LIMIT)
    # The same history from well-placed runs still sets the limit.
    for day in range(6):
        put(scan, f"ev{day}", {"perf.placement.overlapping_ranks": 0})
    assert only(checks.io_share_high(scan, "ev7")).failing


def test_io_share_high(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.io_share": 0.25})
    assert only(checks.io_share_high(scan, "ev7")).failing
    put(scan, "ev6", {"perf.io_share": 0.09})
    assert not only(checks.io_share_high(scan, "ev6")).failing


def test_memory_not_recorded(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.memory.recorded_fraction": 0.0})
    assert only(checks.memory_not_recorded(scan, "ev7")).failing
    put(scan, "ev6", {"perf.memory.recorded_fraction": 1.0})
    assert not only(checks.memory_not_recorded(scan, "ev6")).failing


def test_binding_wrong(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.placement.narrow_ranks": 3, "perf.placement.overlapping_ranks": 0})
    set_metric(scan, "ev7", RES, "perf.slowest_rank", 8, None, {"host": "n04", "cpus": 4})
    evaluation = only(checks.binding_wrong(scan, "ev7"))
    assert evaluation.failing and evaluation.value == 3 and "n04" in evaluation.summary
    put(scan, "ev6", {"perf.placement.narrow_ranks": 0, "perf.placement.overlapping_ranks": 2})
    assert only(checks.binding_wrong(scan, "ev6")).failing
    put(scan, "ev5", {"perf.placement.narrow_ranks": 0, "perf.placement.overlapping_ranks": 0})
    assert not only(checks.binding_wrong(scan, "ev5")).failing


def test_steps_uncounted(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.steps_uncounted_fraction": 0.15})
    assert only(checks.steps_uncounted(scan, "ev7")).failing
    put(scan, "ev6", {"perf.steps_uncounted_fraction": 0.0})
    assert not only(checks.steps_uncounted(scan, "ev6")).failing


def test_time_limit_proximity(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.job.time_limit_fraction": 0.95})
    assert only(checks.time_limit_proximity(scan, "ev7")).failing
    put(scan, "ev6", {"perf.job.time_limit_fraction": 0.2})
    assert not only(checks.time_limit_proximity(scan, "ev6")).failing


def test_memory_headroom_low(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"perf.memory.peak_fraction": 0.9})
    assert only(checks.memory_headroom_low(scan, "ev7")).failing
    put(scan, "ev6", {"perf.memory.peak_fraction": 0.2})
    assert not only(checks.memory_headroom_low(scan, "ev6")).failing


def test_time_to_results(scan: sqlite3.Connection) -> None:
    put(scan, "ev7", {"ops.time_to_results_s": 3 * 3600, "ops.task.sw4.run_s": 7000, "ops.task.record_sw4.run_s": 7100})
    evaluation = only(checks.time_to_results(scan, "ev7"))
    assert evaluation.failing and "sw4 117 min" in evaluation.summary and "record_sw4" not in evaluation.summary
    put(scan, "ev6", {"ops.time_to_results_s": 1975})
    assert not only(checks.time_to_results(scan, "ev6")).failing


def test_sw4_input_ignored(scan: sqlite3.Connection) -> None:
    put(scan, "ev1", {"perf.sw4.ignored_options": 0.0, "perf.sw4.attenuation_mismatches": 0.0, "perf.cfl": 1.3})
    put(scan, "ev1", {"perf.sw4.log_cfl": 1.29999995})
    assert not only(checks.sw4_input_ignored(scan, "ev1")).failing
    set_metric(
        scan,
        "ev2",
        RES,
        "perf.sw4.ignored_options",
        2.0,
        "count",
        [{"command": "attenuation", "option": "cfl=1.3"}, {"command": "attenuation", "option": "reporttiming=1"}],
    )
    set_metric(
        scan,
        "ev2",
        RES,
        "perf.sw4.attenuation_mismatches",
        3.0,
        "count",
        {"used": {"nmech": 3, "maxfreq": 2.0, "phasefreq": 1.0}, "requested": {"cfl": 1.3}, "mismatched": []},
    )
    put(scan, "ev2", {"perf.cfl": 0.9, "perf.sw4.log_cfl": 1.29999995})
    evaluation = only(checks.sw4_input_ignored(scan, "ev2"))
    assert evaluation.failing and evaluation.value == 6
    assert "attenuation cfl=1.3" in evaluation.summary and "CFL 0.9" in evaluation.summary
