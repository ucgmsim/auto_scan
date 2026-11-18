"""Checks on SW4's cost, speed, placement and latency, from ``auto_scan.extract.perf``'s metrics.

Each check is evaluated per resolution (the subject), so a finding names the resolution it
is about. Structural problems (MPI wait, imbalance, binding, memory, cost model margins)
are tested against absolute bounds, since a problem present from the first event is still
a problem; regressions (throughput, interface solve, I/O) against the median of the last
``HISTORY_WINDOW`` earlier events at the same resolution once there are ``MIN_HISTORY``.

================================ =============================================================================
check                            fails when
================================ =============================================================================
perf.cost_model_overestimate     the cautious estimate was > OVERESTIMATE_RATIO x the time stepping's actual
                                 core-hours and the domain was cut (stations lost for nothing)
perf.cost_model_underestimate    the actual core-hours were > 1 / UNDERESTIMATE_RATIO x the estimate (a domain
                                 sized to the budget would hit the time limit)
perf.throughput_regression       updates per core-second fell below the history (or an absolute floor)
perf.contention_slowdown         the same, while other SW4 jobs of the workflow ran beside it or shared nodes
perf.mpi_wait_high               more than MPI_FRACTION_LIMIT of solve is spent inside MPI
perf.rank_imbalance              evalRHS's slowest rank takes > RANK_IMBALANCE_LIMIT x the average
perf.interface_solve_cost        enforceIC's share of time stepping, or Jacobi iterations a step, grew
perf.io_share_high               output, time-series writing, input parsing and setup take too much of the wall
perf.memory_not_recorded         numa_bind.sh's rank files lack the per-rank peak memory
perf.binding_wrong               ranks bound to fewer CPUs than their threads need, or sharing CPUs
perf.steps_uncounted             sw4_run.json's steps miss steps SW4 ran (pre-roll), so the cost model under-counts
perf.time_limit_proximity        SW4's job used more than TIME_LIMIT_FRACTION of its time limit
perf.memory_headroom_low         a rank's peak memory came within MEMORY_FRACTION_LIMIT of its request
perf.time_to_results             results arrived later than TIME_TO_RESULTS_S after the cycle's first job
perf.sw4_input_ignored           sw4.log warns it ignored an option, fitted attenuation the realisation didn't ask
                                 for, or ran at a CFL other than sw4_run.json records
================================ =============================================================================
"""

import json
import sqlite3
import statistics
from collections.abc import Iterator
from typing import Any

from auto_scan.db import get_metric, metric_history
from auto_scan.registry import Evaluation, Severity, check

# DESIGN.md: compare with a metric's own history once it has 5 earlier events.
MIN_HISTORY = 5
# The median of the last 10 earlier events: recent enough to follow a new build, long
# enough that one contended run doesn't move it.
HISTORY_WINDOW = 10

# The cost model is cautious by design: z=1 spreads on each factor. With its priors'
# spreads (points 0.10, steps 0.12, speed 0.25, efficiency 0.10 in sw4_cost_model.py) the
# intended margin is exp(0.57) = 1.8x; 2.5x leaves another ~40% for run-to-run noise.
OVERESTIMATE_RATIO = 2.5
# size_domain.py sizes a domain to 80% of the time limit (the budget), so a run that takes
# more than 1/0.8 = 1.25x its estimate would have hit the limit had it been sized to the
# budget exactly.
UNDERESTIMATE_RATIO = 0.8
# Speed below 0.6x the history: the speed prior's spread (SPEED_SPREAD 0.25) at 2 sigma
# is exp(-0.5) = 0.61. Before there is history: a third of the 6e5 updates/core-s RCH
# measured at 0.5 and 1 Hz (sw4_cost_model.py's docstring).
THROUGHPUT_DROP = 0.6
THROUGHPUT_FLOOR = 2.0e5
# Other SW4 jobs of the workflow averaging at least half this job's cores during its run,
# or sharing a tenth of its node-time, count as contention (other users' jobs are
# invisible here): SW4 is memory-bandwidth bound, so a neighbour on the node slows it.
CONTENTION_RATIO = 0.5
SHARED_NODE_FRACTION = 0.1
# sw4_cost_model.py's docstring: 96 cores over 8 nodes spent 40% waiting on other nodes,
# 192 over 2 only 7%. 40% of solve inside MPI is the worst placement it describes.
MPI_FRACTION_LIMIT = 0.4
# SW4 splits each grid evenly in x and y, so evalRHS work is near-equal across ranks;
# the boundary ranks' supergrid work adds 10-30% (1 Hz on 2026p866048, 48 ranks, all with
# their cores: 1.31 curvilinear). 1.5 is beyond that.
RANK_IMBALANCE_LIMIT = 1.5
# enforceIC (mesh-refinement interfaces) took 28% of time stepping at 0.5 Hz and 15% at
# 1 Hz on 2026p866048, with 30 and 29 Jacobi iterations a step: absolute bounds well above
# those, and growth against history.
INTERFACE_SHARE_LIMIT = 0.40
INTERFACE_JACOBI_LIMIT = 60.0
INTERFACE_SHARE_GROWTH = 1.5
INTERFACE_JACOBI_GROWTH = 1.3
# I/O was 9% of the 0.5 Hz wall and 2% of the 1 Hz on 2026p866048 (13,073 and 28,679
# receivers): 15% absolute, or twice the history (and at least 5%).
IO_SHARE_LIMIT = 0.15
IO_SHARE_GROWTH = 2.0
IO_SHARE_MIN = 0.05
# Ranks wider than this fraction recorded count as recorded (every rank should be).
MEMORY_RECORDED_MIN = 1.0
# Pre-roll steps (the source filter's precursor) were 15% of 0.5 Hz steps and 6% of 1 Hz:
# 3% is well above rounding.
STEPS_UNCOUNTED_LIMIT = 0.03
# Sized to 80% of the limit, so past 90% the margin is mostly gone.
TIME_LIMIT_FRACTION = 0.9
# A rank's peak above 80% of its --mem-per-cpu x --cpus-per-task is one big domain away
# from an out-of-memory kill.
MEMORY_FRACTION_LIMIT = 0.8
# README: 0.5 Hz results within about an hour of the CMT; twice that. 0.25 Hz: 2.5 h SW4
# limit plus about an hour of the rest; 1 Hz: 24 h limit sized to 80% (19.2 h) plus the rest.
TIME_TO_RESULTS_S = {"0_25hz": 4 * 3600.0, "0_5hz": 2 * 3600.0, "1hz": 26 * 3600.0}

COST_MODEL_CODE = [
    "realtime_simulation_workflow bin/sw4_cost_model.py: fit_factor (spread), fit (speed, efficiency)",
    "realtime_simulation_workflow bin/size_domain.py: fits, main (bisection on reach)",
    "realtime_simulation_workflow bin/record_sw4_run.py: parse_log, record (the run records it learns from)",
]
PLACEMENT_CODE = [
    "realtime_simulation_workflow flow.cylc: [[sw4<res>]] directives and srun line",
    "realtime_simulation_workflow bin/slurm_hostfile.sh, bin/numa_bind.sh",
]
ORDER = {"0_25hz": 0, "0_5hz": 1, "1hz": 2}


def _resolutions(db: sqlite3.Connection, event_id: str, metric: str) -> list[str]:
    rows = db.execute(
        "SELECT DISTINCT resolution FROM metrics WHERE event_id = ? AND metric = ? AND value IS NOT NULL",
        (event_id, metric),
    )
    return sorted((row["resolution"] for row in rows), key=lambda r: ORDER.get(r, 99))


def _detail(db: sqlite3.Connection, event_id: str, resolution: str, metric: str) -> Any:
    row = db.execute(
        "SELECT detail FROM metrics WHERE event_id = ? AND resolution = ? AND metric = ?",
        (event_id, resolution, metric),
    ).fetchone()
    if row is None or row["detail"] is None:
        return None
    return json.loads(row["detail"])


def _values(db: sqlite3.Connection, event_id: str, resolution: str, names: list[str]) -> dict[str, float | None]:
    return {name: get_metric(db, event_id, resolution, name) for name in names}


def _misplaced(db: sqlite3.Connection, event_id: str, resolution: str) -> bool:
    """Whether some of the run's ranks had too few CPUs or shared them (perf.binding_wrong's input)."""
    narrow = get_metric(db, event_id, resolution, "perf.placement.narrow_ranks")
    overlapping = get_metric(db, event_id, resolution, "perf.placement.overlapping_ranks")
    return bool(narrow or overlapping)


def _baseline(
    db: sqlite3.Connection, metric: str, resolution: str, event_id: str, *, placed_only: bool = False
) -> tuple[float | None, list[tuple[str, float]]]:
    """The median of a metric's last HISTORY_WINDOW earlier values, if there are MIN_HISTORY.

    With ``placed_only``, runs with misplaced ranks are left out: every rank waits on the slow
    ones, which stretches the wall and the phases that synchronise, so a share of either is not
    comparable with a well-placed run's.
    """
    history = metric_history(db, metric, resolution, before_event=event_id)
    if placed_only:
        history = [(event, value) for event, value in history if not _misplaced(db, event, resolution)]
    history = history[-HISTORY_WINDOW:]
    if len(history) < MIN_HISTORY:
        return None, history
    return statistics.median(value for _, value in history), history


def _commit(db: sqlite3.Connection, event_id: str, resolution: str, source: str) -> str | None:
    row = db.execute(
        "SELECT commit_sha FROM run_sources WHERE event_id = ? AND resolution = ? AND source = ?",
        (event_id, resolution, source),
    ).fetchone()
    return None if row is None else row["commit_sha"]


def _evaluation(
    check_id: str,
    event_id: str,
    resolution: str,
    failing: bool,
    title: str,
    summary: str,
    value: float | None,
    threshold: float | None,
    evidence: dict[str, Any],
    suspects: list[str],
    severity: Severity = "medium",
) -> Evaluation:
    return Evaluation(
        check_id=check_id,
        event_id=event_id,
        failing=failing,
        title=title,
        summary=summary,
        kind="perf",
        severity=severity,
        resolution=resolution,
        subject=resolution,
        value=value,
        threshold=threshold,
        evidence=evidence,
        suspect_sources=suspects,
    )


COST_METRICS = [
    "perf.cost_ratio",
    "perf.central_cost_ratio",
    "perf.cost_model_caution",
    "perf.points_ratio",
    "perf.steps_ratio",
    "perf.speed_ratio",
    "perf.estimated_core_hours",
    "perf.estimated_core_hours_effective",
    "perf.central_core_hours",
    "perf.actual_core_hours",
    "perf.job.core_hours",
    "perf.job.cost_ratio",
    "perf.budget_core_hours",
    "perf.domain_fraction",
    "perf.domain_fraction_measured",
    "perf.full_domain_core_hours_effective",
    "perf.full_domain_core_hours_measured",
    "perf.updates_per_core_s",
    "perf.model.points_factor_spread",
    "perf.model.steps_per_second_spread",
    "perf.model.speed",
    "perf.model.speed_spread",
    "perf.model.speed_runs",
    "perf.model.multinode_efficiency",
    "perf.model.multinode_efficiency_spread",
    "perf.model.runs",
    "perf.model.z",
    "perf.steps_uncounted_fraction",
]


def _cost_evidence(db: sqlite3.Connection, event_id: str, resolution: str) -> dict[str, Any]:
    values = _values(db, event_id, resolution, COST_METRICS)
    z = values["perf.model.z"] or 1.0
    margins = {
        name: z * spread
        for name in ("points_factor", "steps_per_second", "speed", "multinode_efficiency")
        if (spread := values[f"perf.model.{name}_spread"]) is not None
    }
    return {
        "metrics": values,
        "caution_by_factor_log": margins,
        "largest_caution": max(margins, key=lambda name: margins[name]) if margins else None,
        "domain_fraction_measured": _detail(db, event_id, resolution, "perf.domain_fraction_measured"),
        "domain": _detail(db, event_id, resolution, "perf.domain_fraction"),
        "history_cost_ratio": metric_history(db, "perf.cost_ratio", resolution, before_event=event_id)[
            -HISTORY_WINDOW:
        ],
        "files": [f"{resolution}/sw4_estimate.json", f"{resolution}/sw4_run.json", "size_domain_<res>/NN/job.out"],
        "code": COST_MODEL_CODE,
    }


@check
def cost_model_overestimate(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """The cost model over-estimated SW4's cost and cut the domain for it.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with a cost ratio.
    """
    for resolution in _resolutions(db, event_id, "perf.cost_ratio"):
        ratio = get_metric(db, event_id, resolution, "perf.cost_ratio")
        assert ratio is not None
        fraction = get_metric(db, event_id, resolution, "perf.domain_fraction")
        attainable = get_metric(db, event_id, resolution, "perf.domain_fraction_measured")
        cut = fraction is not None and fraction < 0.999
        evidence = _cost_evidence(db, event_id, resolution)
        summary = f"The effective estimate was {ratio:.1f}x the core-hours SW4's time stepping took"
        if fraction is not None:
            summary += f"; the domain kept {fraction:.0%} of the full area"
        if attainable is not None:
            summary += f", where the measured cost would have allowed {attainable:.0%}"
        largest = evidence["largest_caution"]
        if largest:
            summary += (
                f". The largest caution is the {largest} spread ({evidence['caution_by_factor_log'][largest]:.2f})"
            )
        yield _evaluation(
            "perf.cost_model_overestimate",
            event_id,
            resolution,
            ratio > OVERESTIMATE_RATIO and cut,
            "SW4 cost model over-estimates cost, shrinking domains needlessly",
            summary + ".",
            ratio,
            OVERESTIMATE_RATIO,
            evidence,
            ["realtime"],
            # Each needless cut drops stations from the simulated results.
            "high" if attainable is not None and fraction is not None and attainable > 1.5 * fraction else "medium",
        )


@check
def cost_model_underestimate(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4 took longer than the cost model's cautious estimate.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with a cost ratio.
    """
    for resolution in _resolutions(db, event_id, "perf.cost_ratio"):
        ratio = get_metric(db, event_id, resolution, "perf.cost_ratio")
        assert ratio is not None
        evidence = _cost_evidence(db, event_id, resolution)
        evidence["time_limit_fraction"] = get_metric(db, event_id, resolution, "perf.job.time_limit_fraction")
        evidence["contention"] = _contention(db, event_id, resolution)
        yield _evaluation(
            "perf.cost_model_underestimate",
            event_id,
            resolution,
            ratio < UNDERESTIMATE_RATIO,
            "SW4 cost model under-estimates cost, risking time-limit kills",
            f"The effective estimate was {ratio:.2f}x the core-hours SW4's time stepping took"
            f" (below {UNDERESTIMATE_RATIO} a domain sized to the budget overruns the time limit).",
            ratio,
            UNDERESTIMATE_RATIO,
            evidence,
            ["realtime"],
            "high",
        )


def _contention(db: sqlite3.Connection, event_id: str, resolution: str) -> dict[str, Any]:
    names = [
        "perf.job.contention_cores",
        "perf.job.contention_ratio",
        "perf.job.concurrent_sw4_cores",
        "perf.job.shared_nodes",
        "perf.job.shared_node_fraction",
        "perf.cores",
        "perf.placement.narrow_ranks",
        "perf.placement.overlapping_ranks",
    ]
    values = _values(db, event_id, resolution, names)
    sw4_cores, cores, shared = (
        values["perf.job.concurrent_sw4_cores"],
        values["perf.cores"],
        values["perf.job.shared_node_fraction"],
    )
    if sw4_cores is None and shared is None:
        contended = None
    else:
        contended = bool(
            (sw4_cores and cores and sw4_cores / cores >= CONTENTION_RATIO)
            or (shared and shared >= SHARED_NODE_FRACTION)
        )
    misplaced = bool(values["perf.placement.narrow_ranks"] or values["perf.placement.overlapping_ranks"])
    return {
        "metrics": values,
        "contended": contended,
        "misplaced": misplaced,
        "concurrent_sw4_jobs": _detail(db, event_id, resolution, "perf.job.concurrent_sw4_cores"),
        "shared_nodes": _detail(db, event_id, resolution, "perf.job.shared_nodes"),
    }


def _throughput(db: sqlite3.Connection, event_id: str, resolution: str) -> tuple[float, float, dict[str, Any]]:
    value = get_metric(db, event_id, resolution, "perf.updates_per_core_s")
    assert value is not None
    baseline, history = _baseline(db, "perf.updates_per_core_s", resolution, event_id)
    threshold = baseline * THROUGHPUT_DROP if baseline is not None else THROUGHPUT_FLOOR
    sources = {event: _commit(db, event, resolution, "sw4") for event, _ in history}
    evidence = {
        "updates_per_core_s": value,
        "run": _detail(db, event_id, resolution, "perf.updates_per_core_s"),
        "baseline_median": baseline,
        "history": history,
        "sw4_commit": _commit(db, event_id, resolution, "sw4"),
        "history_sw4_commits": sources,
        "contention": _contention(db, event_id, resolution),
        "mpi_fraction": get_metric(db, event_id, resolution, "perf.mpi_fraction"),
        "rank_imbalance": get_metric(db, event_id, resolution, "perf.rank_imbalance"),
        "files": [f"{resolution}/sw4_run.json", "sw4_<res>/NN/sw4_timers.csv", "sw4_<res>/NN/ranks/"],
    }
    return value, threshold, evidence


@check
def throughput_regression(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4's updates per core-second fell against its history at the same resolution.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with a measured speed.
    """
    for resolution in _resolutions(db, event_id, "perf.updates_per_core_s"):
        value, threshold, evidence = _throughput(db, event_id, resolution)
        contention = evidence["contention"]
        if contention["contended"] is False and not contention["misplaced"]:
            suspects = ["sw4"]
        elif contention["contended"] or contention["misplaced"]:
            suspects = ["realtime"]
        else:
            suspects = ["sw4", "realtime"]
        failing = value < threshold
        basis = "the history's median" if evidence["baseline_median"] else "the absolute floor"
        yield _evaluation(
            "perf.throughput_regression",
            event_id,
            resolution,
            failing,
            "SW4 throughput regressed",
            f"SW4 ran {value:.3g} grid-point updates per core-second against {threshold:.3g} ({basis});"
            f" contended: {contention['contended']}, misplaced ranks: {contention['misplaced']}.",
            value,
            threshold,
            evidence,
            suspects if failing else ["sw4"],
            "medium",
        )


@check
def contention_slowdown(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4 ran slowly while other SW4 jobs of the workflow ran beside it.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with a speed and job metrics.
    """
    for resolution in _resolutions(db, event_id, "perf.updates_per_core_s"):
        value, threshold, evidence = _throughput(db, event_id, resolution)
        contended = evidence["contention"]["contended"]
        if contended is None:
            continue
        yield _evaluation(
            "perf.contention_slowdown",
            event_id,
            resolution,
            value < threshold and contended,
            "SW4 slowed by concurrent workflow jobs",
            f"SW4 ran {value:.3g} updates per core-second against {threshold:.3g} with"
            f" {evidence['contention']['metrics']['perf.job.concurrent_sw4_cores'] or 0:.0f} cores of other SW4 jobs"
            f" beside it on average and {evidence['contention']['metrics']['perf.job.shared_nodes'] or 0:.0f}"
            " shared nodes.",
            value,
            threshold,
            evidence | {"code": ["realtime_simulation_workflow flow.cylc: MAX_CONCURRENT_EVENTS, the sw4<res> graph"]},
            ["realtime"],
        )


@check
def mpi_wait_high(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """Too much of SW4's solve is spent inside MPI.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with timers.
    """
    for resolution in _resolutions(db, event_id, "perf.mpi_fraction"):
        value = get_metric(db, event_id, resolution, "perf.mpi_fraction")
        assert value is not None
        shares = {
            row["metric"].removeprefix("perf.mpi_share."): row["value"]
            for row in db.execute(
                "SELECT metric, value FROM metrics WHERE event_id = ? AND resolution = ? AND metric LIKE"
                " 'perf.mpi_share.%' ORDER BY value DESC",
                (event_id, resolution),
            )
        }
        top = next(iter(shares), None)
        imbalance = get_metric(db, event_id, resolution, "perf.rank_imbalance")
        # Time in MPI_Barrier is time waiting for the slowest rank: imbalance, not the network.
        waiting = top == "barrier" or (imbalance is not None and imbalance > RANK_IMBALANCE_LIMIT)
        evidence = {
            "mpi_fraction": value,
            "mpi_share_of_solve": shares,
            "mpi_max_min": get_metric(db, event_id, resolution, "perf.mpi_max_min"),
            "comm_wait_fraction": get_metric(db, event_id, resolution, "perf.comm_wait_fraction"),
            "allreduce_per_step": get_metric(db, event_id, resolution, "perf.allreduce_per_step"),
            "barrier_per_step": get_metric(db, event_id, resolution, "perf.barrier_per_step"),
            "rank_imbalance": imbalance,
            "nodes": get_metric(db, event_id, resolution, "perf.placement.hosts"),
            "ranks_per_host": _detail(db, event_id, resolution, "perf.placement.hosts"),
            "history": metric_history(db, "perf.mpi_fraction", resolution, before_event=event_id)[-HISTORY_WINDOW:],
            "files": ["sw4_<res>/NN/sw4_timers.csv (solve, MPI_* rows)"],
        }
        yield _evaluation(
            "perf.mpi_wait_high",
            event_id,
            resolution,
            value > MPI_FRACTION_LIMIT,
            "SW4 spends much of its solve waiting in MPI",
            f"{value:.0%} of SW4's solve was inside MPI, mostly {top} ({shares.get(top or '', 0):.0%} of solve);"
            f" evalRHS rank imbalance {imbalance or 0:.2f}.",
            value,
            MPI_FRACTION_LIMIT,
            evidence,
            ["realtime"] if waiting else ["sw4", "realtime"],
        )


@check
def rank_imbalance(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4's ranks do unequal compute work.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with timers.
    """
    for resolution in _resolutions(db, event_id, "perf.rank_imbalance"):
        value = get_metric(db, event_id, resolution, "perf.rank_imbalance")
        assert value is not None
        phases = {
            phase: _values(
                db, event_id, resolution, [f"perf.imbalance.{phase}.max_avg", f"perf.imbalance.{phase}.max_min"]
            )
            for phase in ("curvilinear", "cartesian", "memvar")
        }
        narrow = get_metric(db, event_id, resolution, "perf.placement.narrow_ranks")
        overlapping = get_metric(db, event_id, resolution, "perf.placement.overlapping_ranks")
        slowest = _detail(db, event_id, resolution, "perf.slowest_rank")
        misplaced = bool(narrow or overlapping)
        evidence = {
            "evalRHS": _detail(db, event_id, resolution, "perf.rank_imbalance"),
            "phases": phases,
            "slowest_rank": get_metric(db, event_id, resolution, "perf.slowest_rank"),
            "slowest_rank_detail": slowest,
            "narrow_ranks": _detail(db, event_id, resolution, "perf.placement.narrow_ranks"),
            "overlapping_ranks": overlapping,
            "ranks_per_host": _detail(db, event_id, resolution, "perf.placement.hosts"),
            "files": ["sw4_<res>/NN/sw4_timers.csv", "sw4_<res>/NN/sw4.log (Per-rank spread)", "sw4_<res>/NN/ranks/"],
            "code": PLACEMENT_CODE,
        }
        yield _evaluation(
            "perf.rank_imbalance",
            event_id,
            resolution,
            value > RANK_IMBALANCE_LIMIT,
            "SW4 ranks are load-imbalanced",
            f"SW4's slowest rank spent {value:.2f}x the average in evalRHS"
            + (f"; {narrow:.0f} ranks had fewer cores than threads" if narrow else "")
            + ".",
            value,
            RANK_IMBALANCE_LIMIT,
            evidence,
            ["realtime"] if misplaced else ["sw4", "realtime"],
        )


@check
def interface_solve_cost(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4's mesh-refinement interface solve (enforceIC) got more expensive.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with timers.
    """
    for resolution in _resolutions(db, event_id, "perf.share.enforce_ic"):
        share = get_metric(db, event_id, resolution, "perf.share.enforce_ic")
        assert share is not None
        jacobi = get_metric(db, event_id, resolution, "perf.interface_jacobi_per_step")
        misplaced = _misplaced(db, event_id, resolution)
        share_base, share_history = _baseline(db, "perf.share.enforce_ic", resolution, event_id, placed_only=True)
        jacobi_base, jacobi_history = _baseline(
            db, "perf.interface_jacobi_per_step", resolution, event_id, placed_only=True
        )
        share_limit = (
            INTERFACE_SHARE_LIMIT
            if share_base is None
            else min(INTERFACE_SHARE_LIMIT, share_base * INTERFACE_SHARE_GROWTH)
        )
        jacobi_limit = (
            INTERFACE_JACOBI_LIMIT
            if jacobi_base is None
            else min(INTERFACE_JACOBI_LIMIT, jacobi_base * INTERFACE_JACOBI_GROWTH)
        )
        # enforceIC is the first collective after evalRHS, so with misplaced ranks its time is
        # mostly the others waiting for them (93% inside MPI on 2026p806127): that is
        # perf.binding_wrong's finding, not the interface solve's. Its Jacobi iterations still count.
        failing = (share > share_limit and not misplaced) or (jacobi is not None and jacobi > jacobi_limit)
        iterations = (
            "Jacobi iterations not timed by this SW4 build"
            if jacobi is None
            else f"{jacobi:.1f} Jacobi iterations a step (limit {jacobi_limit:.1f})"
        )
        yield _evaluation(
            "perf.interface_solve_cost",
            event_id,
            resolution,
            failing,
            "SW4 mesh-refinement interface solve is expensive",
            f"enforceIC took {share:.0%} of time stepping (limit {share_limit:.0%}"
            + ("; not judged, ranks were misplaced" if misplaced else "")
            + f"), {iterations}.",
            share,
            share_limit,
            {
                "enforce_ic_share": share,
                "jacobi_per_step": jacobi,
                "jacobi_limit": jacobi_limit,
                "jacobi_detail": _detail(db, event_id, resolution, "perf.interface_jacobi_per_step"),
                "allreduce_per_step": get_metric(db, event_id, resolution, "perf.allreduce_per_step"),
                "mpi_share_allreduce": get_metric(db, event_id, resolution, "perf.mpi_share.allreduce"),
                "grids": _detail(db, event_id, resolution, "perf.sw4.grids"),
                "misplaced_ranks": misplaced,
                "share_history": share_history,
                "jacobi_history": jacobi_history,
                "sw4_commit": _commit(db, event_id, resolution, "sw4"),
                "files": ["sw4_<res>/NN/sw4_timers.csv (enforceIC, consintp/jacobi, convergence_check)"],
            },
            ["sw4"],
            "low",
        )


@check
def io_share_high(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4 spends too much of its wall time on I/O and setup.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with timers.
    """
    for resolution in _resolutions(db, event_id, "perf.io_share"):
        value = get_metric(db, event_id, resolution, "perf.io_share")
        assert value is not None
        # Against well-placed runs only: a run slowed by misplaced ranks has a long wall and so a
        # small I/O share (0.3-3% at 0.5 Hz before 2026-11-04), a baseline no normal run can meet.
        base, history = _baseline(db, "perf.io_share", resolution, event_id, placed_only=True)
        limit = IO_SHARE_LIMIT if base is None else min(IO_SHARE_LIMIT, max(IO_SHARE_MIN, base * IO_SHARE_GROWTH))
        yield _evaluation(
            "perf.io_share_high",
            event_id,
            resolution,
            value > limit,
            "SW4 spends a large share of its run on I/O",
            f"Output, time-series writing, input parsing and setup took {value:.0%} of SW4's wall time"
            f" (limit {limit:.0%}).",
            value,
            limit,
            {
                "io": _detail(db, event_id, resolution, "perf.io_share"),
                "receivers": get_metric(db, event_id, resolution, "perf.sw4.receivers"),
                "history": history,
                "files": ["sw4_<res>/NN/sw4_timers.csv (output/*, write_timeseries, parse_input, setupRun)"],
            },
            ["sw4", "realtime"],
            "low",
        )


@check
def memory_not_recorded(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """numa_bind.sh's rank files lack each rank's peak memory.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with rank files.
    """
    for resolution in _resolutions(db, event_id, "perf.memory.recorded_fraction"):
        value = get_metric(db, event_id, resolution, "perf.memory.recorded_fraction")
        assert value is not None
        yield _evaluation(
            "perf.memory_not_recorded",
            event_id,
            resolution,
            value < MEMORY_RECORDED_MIN,
            "SW4 per-rank peak memory is not recorded",
            f"{value:.0%} of SW4's rank files have a max_rss_kb line; record_sw4 then reports 0.00 GB peaks.",
            value,
            MEMORY_RECORDED_MIN,
            {
                "rank_files": _detail(db, event_id, resolution, "perf.memory.recorded_fraction"),
                "sacct_max_rss_gb": get_metric(db, event_id, resolution, "perf.memory.sacct_max_rss_gb"),
                "sacct": _detail(db, event_id, resolution, "perf.memory.sacct_max_rss_gb"),
                "expected": "numa_bind.sh appends /usr/bin/time's max_rss_kb, elapsed_s, ... to rank-<n>.txt; it skips"
                " that silently when /usr/bin/time is not executable on the compute node",
                "files": ["sw4_<res>/NN/ranks/rank-<n>.txt", "record_sw4_<res>/NN/job.out", "rank_peaks.txt"],
                "code": [
                    "realtime_simulation_workflow bin/numa_bind.sh (the [[ -x /usr/bin/time ]] branch)",
                    "realtime_simulation_workflow flow.cylc [[record_sw4<res>]] (rank_peaks.txt, the awk summary)",
                ],
            },
            ["realtime"],
            "low",
        )


@check
def binding_wrong(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4's ranks got fewer CPUs than their threads, or shared CPUs.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with rank files.
    """
    for resolution in _resolutions(db, event_id, "perf.placement.narrow_ranks"):
        narrow = get_metric(db, event_id, resolution, "perf.placement.narrow_ranks") or 0.0
        overlapping = get_metric(db, event_id, resolution, "perf.placement.overlapping_ranks") or 0.0
        value = narrow + overlapping
        slowest = _detail(db, event_id, resolution, "perf.slowest_rank")
        yield _evaluation(
            "perf.binding_wrong",
            event_id,
            resolution,
            value > 0,
            "SW4 ranks bound to too few or shared CPUs",
            f"{narrow:.0f} SW4 ranks had fewer CPUs than their threads need and {overlapping:.0f} shared CPUs"
            " with another rank"
            + (f"; the slowest rank ({slowest.get('host')}) had {slowest.get('cpus')} CPUs" if slowest else "")
            + ".",
            value,
            0.0,
            {
                "narrow_ranks": _detail(db, event_id, resolution, "perf.placement.narrow_ranks"),
                "expected_cpu_width": _detail(db, event_id, resolution, "perf.placement.expected_cpu_width"),
                "overlapping": _detail(db, event_id, resolution, "perf.placement.overlapping_ranks"),
                "multi_numa_ranks": _detail(db, event_id, resolution, "perf.placement.multi_numa_ranks"),
                "ranks_per_host": _detail(db, event_id, resolution, "perf.placement.hosts"),
                "slowest_rank": slowest,
                "rank_imbalance": get_metric(db, event_id, resolution, "perf.rank_imbalance"),
                "mpi_share_barrier": get_metric(db, event_id, resolution, "perf.mpi_share.barrier"),
                "files": ["sw4_<res>/NN/ranks/rank-<n>.txt (cpus)", "sw4_<res>/NN/sw4.log (OMP_PLACES)", "sw4_hosts"],
                "code": PLACEMENT_CODE,
            },
            ["realtime"],
        )


@check
def steps_uncounted(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """sw4_run.json's steps miss steps SW4 ran, so the cost model under-counts them.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with both step counts.
    """
    for resolution in _resolutions(db, event_id, "perf.steps_uncounted_fraction"):
        value = get_metric(db, event_id, resolution, "perf.steps_uncounted_fraction")
        assert value is not None
        yield _evaluation(
            "perf.steps_uncounted",
            event_id,
            resolution,
            value > STEPS_UNCOUNTED_LIMIT,
            "SW4 run records miss pre-roll time steps",
            f"SW4's timers ran {value:.0%} more steps than sw4_run.json records; the cost model's steps per simulated"
            " second, and so every estimate, leave them out.",
            value,
            STEPS_UNCOUNTED_LIMIT,
            {
                "steps": _detail(db, event_id, resolution, "perf.steps_uncounted_fraction"),
                "preroll_steps": get_metric(db, event_id, resolution, "perf.sw4.preroll_steps"),
                "timestep_wall_s": get_metric(db, event_id, resolution, "perf.timestep_wall_s"),
                "time_stepping_s": get_metric(db, event_id, resolution, "perf.timers.time_stepping_s"),
                "explanation": "record_sw4_run.py takes steps and wall from the last 'Time step N' line, which counts"
                " from t=0; SW4 first runs the pre-roll steps ('Time stepping starts at t=... with N pre-roll steps')",
                "code": ["realtime_simulation_workflow bin/record_sw4_run.py: STEP, parse_log"],
            },
            ["realtime"],
            "low",
        )


@check
def time_limit_proximity(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4's job came close to its time limit.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with an SW4 job.
    """
    for resolution in _resolutions(db, event_id, "perf.job.time_limit_fraction"):
        value = get_metric(db, event_id, resolution, "perf.job.time_limit_fraction")
        assert value is not None
        yield _evaluation(
            "perf.time_limit_proximity",
            event_id,
            resolution,
            value > TIME_LIMIT_FRACTION,
            "SW4 jobs run close to their time limit",
            f"SW4's job used {value:.0%} of its time limit (sized to 80%).",
            value,
            TIME_LIMIT_FRACTION,
            {
                "job": _detail(db, event_id, resolution, "perf.job.time_limit_fraction"),
                "cost": _cost_evidence(db, event_id, resolution)["metrics"],
                "contention": _contention(db, event_id, resolution),
            },
            ["realtime"],
            "high",
        )


@check
def memory_headroom_low(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A rank's peak memory came close to what its job asked for.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with a peak and a request.
    """
    for resolution in _resolutions(db, event_id, "perf.memory.peak_fraction"):
        value = get_metric(db, event_id, resolution, "perf.memory.peak_fraction")
        assert value is not None
        yield _evaluation(
            "perf.memory_headroom_low",
            event_id,
            resolution,
            value > MEMORY_FRACTION_LIMIT,
            "SW4 rank memory close to the job's request",
            f"SW4's largest rank peaked at {value:.0%} of its memory request.",
            value,
            MEMORY_FRACTION_LIMIT,
            {
                "peak": _detail(db, event_id, resolution, "perf.memory.peak_fraction"),
                "request_per_rank_gb": get_metric(db, event_id, resolution, "perf.memory.request_per_rank_gb"),
                "sacct": _detail(db, event_id, resolution, "perf.memory.sacct_max_rss_gb"),
                "points": get_metric(db, event_id, resolution, "perf.points"),
                "ranks": get_metric(db, event_id, resolution, "perf.ranks"),
                "code": [
                    "realtime_simulation_workflow flow.cylc: sw4_mem_per_cpu_gb, SW4_WORST_RANK_BYTES_PER_POINT",
                    "realtime_simulation_workflow bin/size_domain.py: fits (memory)",
                ],
            },
            ["realtime"],
        )


@check
def time_to_results(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A resolution's results arrived late.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution with an upload.
    """
    for resolution in _resolutions(db, event_id, "ops.time_to_results_s"):
        limit = TIME_TO_RESULTS_S.get(resolution)
        if limit is None:
            continue
        value = get_metric(db, event_id, resolution, "ops.time_to_results_s")
        assert value is not None
        tasks = {
            row["metric"].removeprefix("ops.task.").removesuffix(".queue_s"): row["value"]
            for row in db.execute(
                "SELECT metric, value FROM metrics WHERE event_id = ? AND resolution IN (?, '') AND metric LIKE"
                " 'ops.task.%.queue_s'",
                (event_id, resolution),
            )
        }
        runs = {
            row["metric"].removeprefix("ops.task.").removesuffix(".run_s"): row["value"]
            for row in db.execute(
                "SELECT metric, value FROM metrics WHERE event_id = ? AND resolution IN (?, '') AND metric LIKE"
                " 'ops.task.%.run_s'",
                (event_id, resolution),
            )
        }
        # record_sw4 only waits for SW4's job to leave the queue.
        slowest = sorted(
            ((name, seconds) for name, seconds in runs.items() if not name.startswith("record_sw4")),
            key=lambda item: -(item[1] or 0),
        )[:5]
        yield _evaluation(
            "perf.time_to_results",
            event_id,
            resolution,
            value > limit,
            "Results arrive late after the cycle starts",
            f"Results reached the upload {value / 3600:.1f} h after the cycle's first job (limit {limit / 3600:.1f} h);"
            f" the longest tasks: {', '.join(f'{name} {seconds / 60:.0f} min' for name, seconds in slowest)}.",
            value,
            limit,
            {
                "time_to_results": _detail(db, event_id, resolution, "ops.time_to_results_s"),
                "from_origin_s": get_metric(db, event_id, resolution, "ops.time_to_results_from_origin_s"),
                "cycle_start_delay_s": get_metric(db, event_id, "", "ops.cycle_start_delay_s"),
                "queue_s": tasks,
                "run_s": runs,
                "history": metric_history(db, "ops.time_to_results_s", resolution, before_event=event_id)[
                    -HISTORY_WINDOW:
                ],
            },
            ["realtime"],
            "medium",
        )


# Any option SW4 ignores is input the workflow meant and SW4 never applied; a CFL in the
# run record that differs from the one SW4 ran at corrupts the cost model's steps.
CFL_TOLERANCE = 0.01


@check
def sw4_input_ignored(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """SW4 ran with different input from what the realisation asked for.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a resolution whose sw4.log was read.
    """
    for resolution in _resolutions(db, event_id, "perf.sw4.ignored_options"):
        ignored = get_metric(db, event_id, resolution, "perf.sw4.ignored_options") or 0.0
        mismatches = get_metric(db, event_id, resolution, "perf.sw4.attenuation_mismatches") or 0.0
        recorded_cfl = get_metric(db, event_id, resolution, "perf.cfl")
        log_cfl = get_metric(db, event_id, resolution, "perf.sw4.log_cfl")
        cfl_wrong = recorded_cfl is not None and log_cfl is not None and abs(recorded_cfl - log_cfl) > CFL_TOLERANCE
        problems = []
        if ignored:
            options = _detail(db, event_id, resolution, "perf.sw4.ignored_options") or []
            problems.append("SW4 ignored " + ", ".join(f"{item['command']} {item['option']}" for item in options[:6]))
        attenuation = _detail(db, event_id, resolution, "perf.sw4.attenuation_mismatches") or {}
        if mismatches:
            problems.append(
                f"SW4 fitted attenuation {attenuation.get('used')} where the realisation asked for"
                f" {attenuation.get('requested')}"
            )
        if cfl_wrong:
            problems.append(f"sw4_run.json records CFL {recorded_cfl:g} but SW4 ran at {log_cfl:.3g}")
        value = ignored + mismatches + (1.0 if cfl_wrong else 0.0)
        yield _evaluation(
            "perf.sw4_input_ignored",
            event_id,
            resolution,
            value > 0,
            "SW4 ran with different input from what the realisation asked for",
            "; ".join(problems) + "." if problems else "SW4 applied every option it was given.",
            value,
            0.0,
            {
                "ignored_options": _detail(db, event_id, resolution, "perf.sw4.ignored_options"),
                "attenuation": attenuation,
                "recorded_cfl": recorded_cfl,
                "log_cfl": log_cfl,
                "files": [
                    "sw4_<res>'s sw4.log ('Warning: ignoring', 'Attenuation parameters', 'CFL=')",
                    "realisation.json",
                ],
                "code": [
                    "workflow workflow/default_parameters/<version>/defaults.yaml: the sw4 commands",
                    "realtime_simulation_workflow patches/, bin/size_domain.py: sw4_cfl",
                ],
            },
            ["workflow", "realtime"],
            "high",
        )
