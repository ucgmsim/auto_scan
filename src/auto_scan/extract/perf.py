"""Performance and operations metrics of each run: SW4's cost against the cost model's
estimate, SW4's own timers, where its ranks ran, and the Cylc jobs around it.

Inputs (DESIGN.md's cache layout): ``sw4_run.json`` (``bin/record_sw4_run.py`` in
realtime_simulation_workflow), ``sw4_estimate.json`` (``bin/size_domain.py``, also in the
manifest as ``sw4_estimate``), and from ``logs/log/job/<cycle>/<task>/<NN>/`` (highest
``NN`` holding the file): ``sw4_<res>``'s ``sw4_timers.csv``, ``sw4.log``, ``sw4_hosts``,
``ranks/rank-<n>.txt`` and job script, ``record_sw4_<res>``'s ``sacct.txt`` and
``size_domain_<res>``'s ``job.out``. The ``jobs`` table (the Cylc monitor's view) feeds the
event extractor; when it is empty no job metrics are written.

Every per-run metric below has the run's resolution; ``(event)`` marks metrics with
resolution ``''``. "Effective" estimates divide by the cautious multi-node efficiency, as
``size_domain.py``'s ``fits`` does before comparing with the budget.

Per-run metrics (``extract_run``)

======================================== ======== ==============================================================
metric                                   unit     meaning
======================================== ======== ==============================================================
perf.points                              points   grid points SW4 reported (sw4_run.json ``points``)
perf.steps                               steps    time steps after t=0 (sw4_run.json; excludes pre-roll)
perf.cores / perf.ranks / perf.threads   count    cores (ranks x threads), MPI ranks, OpenMP threads a rank
perf.nodes                               count    nodes the job spanned (SLURM_JOB_NUM_NODES)
perf.completed                           bool     1 when sw4.log says "program sw4 finished"
perf.timestep_wall_s                     s        wall clock of the last "Time step" line (from t=0)
perf.updates_per_core_s                  1/s      points x steps / (cores x timestep wall): the model's speed
perf.steps_per_simulated_s               1/s      steps / simulated seconds
perf.comm_wait_fraction                  ratio    SW4's "Comm. split" wait / total (sw4_run.json)
perf.cfl                                 -        the CFL number the run used
perf.estimated_points / _steps           -        the cautious estimate of points and steps
perf.points_ratio / perf.steps_ratio     ratio    estimated / actual
perf.speed_ratio                         ratio    actual updates_per_core_s / (cautious speed x efficiency)
perf.estimated_core_hours                core-h   sw4_estimate.json ``core_hours`` (single-node)
perf.estimated_core_hours_effective      core-h   core_hours / multinode_efficiency: what was tested against
                                                  the budget (= expected_wall_hours x cores)
perf.central_core_hours                  core-h   the same estimate from the factors' central values (no z)
perf.actual_core_hours                   core-h   timestep_wall_s x cores / 3600: the scope the model predicts
perf.cost_ratio                          ratio    estimated_core_hours_effective / actual_core_hours (>1: over);
                                                  only for a run that completed, as are central_cost_ratio and
                                                  steps_ratio
perf.central_cost_ratio                  ratio    central_core_hours / actual_core_hours
perf.cost_model_caution                  ratio    estimated_core_hours_effective / central_core_hours
perf.budget_core_hours                   core-h   the budget the domain was sized to
perf.extent_x_km / perf.extent_y_km      km       simulated domain
perf.domain_area_km2                     km2      simulated domain's area
perf.full_domain_area_km2                km2      the uncut domain's area
perf.domain_fraction                     ratio    domain_area / full_domain_area (1: not cut)
perf.domain_fits                         bool     1 when the full domain fitted the budget
perf.full_domain_core_hours_effective    core-h   the full domain's effective estimate
perf.full_domain_core_hours_measured     core-h   the same at this run's measured cost (/ cost_ratio)
perf.domain_fraction_measured            ratio    area of the largest domain size_domain tried that would have
                                                  fitted at the measured cost, over the full area
perf.reach_km / perf.full_reach_km       km       the reach (rrup) kept and the full one
perf.model.<factor>                      -        points_factor, steps_per_second, speed, multinode_efficiency:
perf.model.<factor>_spread               log      central value and log spread; ``perf.model.runs``, ``.z``
perf.model.speed_runs                    weight   the weight of runs behind the speed factor
perf.timers.wall_s                       s        sw4_timers.csv ``wall_avg_s``
perf.timers.solve_s / time_stepping_s    s        average over ranks
perf.timers.steps                        steps    predictor calls a rank: every step, pre-roll included
perf.timers.output_steps                 steps    output calls a rank: steps from t=0
perf.steps_uncounted_fraction            ratio    (timers.steps - perf.steps) / timers.steps: steps the cost model
                                                  never sees (pre-roll)
perf.mpi_fraction                        ratio    solve's mpi_avg_s / avg_s
perf.mpi_max_min                         ratio    solve's mpi_max_s / mpi_min_s
perf.mpi_share.<call>                    ratio    MPI_<Call> mpi_avg_s / solve avg_s (barrier, allreduce, ...)
perf.allreduce_per_step                  count    MPI_Allreduce calls a rank / timers.steps
perf.barrier_per_step                    count    MPI_Barrier calls a rank / timers.steps
perf.rank_imbalance                      ratio    evalRHS (predictor + corrector) max_s / avg_s over ranks
perf.imbalance.<phase>.max_avg / max_min ratio    the same for curvilinear, cartesian, memvar
perf.share.<phase>                       ratio    phase avg_s / time_stepping avg_s, predictor + corrector:
                                                  curvilinear, cartesian, attenuation, enforce_ic, communicate,
                                                  memvar, enforce_bc, output
perf.interface_jacobi_per_step           count    consintp/jacobi calls (predictor + corrector) / timers.steps
perf.io_share                            ratio    (output + write_timeseries + parse_input + setupRun) / wall
perf.io.<part>_share                     ratio    each of those / wall
perf.omp.wait_frac / perf.omp.imb_max    ratio    time_stepping's omp_wait_frac, max omp_imb_max (only if non-zero)
perf.placement.hosts                     count    distinct hosts in sw4_hosts
perf.placement.max_ranks_per_host        count    detail: ranks on each host
perf.placement.min_ranks_per_host        count
perf.placement.expected_cpu_width        cpus     threads x hardware threads a core (OMP_PLACES ``{n:k}``)
perf.placement.narrow_ranks              count    ranks bound to fewer CPUs than that (detail: which, where)
perf.placement.overlapping_ranks         count    ranks whose CPUs overlap another rank's on the same host
perf.placement.multi_numa_ranks          count    ranks whose CPUs span more than one NUMA node
perf.slowest_rank                        rank     sw4.log's per-rank spread: Div-stress max@rank (detail: host)
perf.memory.recorded_fraction            ratio    rank files with a non-zero ``max_rss_kb`` / rank files
perf.memory.rank_peak_max_gb / _mean_gb  GB       from rank files' max_rss_kb, when recorded
perf.memory.sacct_max_rss_gb / _ave      GB       sacct's MaxRSS / AveRSS of the SW4 step (record_sw4's sacct.txt)
perf.memory.request_per_rank_gb          GB       --mem-per-cpu x --cpus-per-task of the SW4 job script
perf.memory.peak_fraction                ratio    largest per-rank peak / request_per_rank_gb
perf.sw4.dt_s                            s        SW4's time step
perf.sw4.preroll_steps                   steps    steps before t=0 (the source filter's precursor)
perf.sw4.grids / finest_h_m              -        grids and finest spacing (detail: the grid table)
perf.sw4.curvilinear_points_fraction     ratio    points on curvilinear grids / all points
perf.sw4.receivers                       count    receivers (detail: how many sit in the sponge)
perf.sw4.ignored_options                 count    "Warning: ignoring <command> line option" lines in sw4.log
                                                  (detail: each command and option)
perf.sw4.log_cfl                         -        the CFL number sw4.log reports it ran at
perf.sw4.attenuation_mismatches          count    attenuation sw4.log fitted (mechanisms, max freq, velo_freq) that
                                                  differs from, or is unset in, realisation.json's ``attenuation``
                                                  command (detail: used, requested, mismatched)
======================================== ======== ==============================================================

Job metrics (``extract_event_jobs``, from the ``jobs`` table; per resolution unless (event))

======================================== ======== ==============================================================
perf.job.run_s / perf.job.queue_s        s        SW4's last submission that ran
perf.job.core_hours                      core-h   its cores x run_s / 3600 (cores: perf.cores if the monitor's is
                                                  smaller, as for early runs where it is --cpus-per-task)
perf.job.cost_ratio                      ratio    perf.estimated_core_hours_effective / perf.job.core_hours
perf.job.time_limit_fraction             ratio    run_s / time limit
perf.job.contention_cores                cores    mean cores of the workflow's other running jobs during it
perf.job.contention_ratio                ratio    contention_cores / its own cores
perf.job.concurrent_sw4_cores            cores    the same counting only other SW4 jobs (detail: which)
perf.job.shared_nodes                    count    hosts shared with an overlapping SW4 job whose sw4_hosts is known
perf.job.shared_node_fraction            ratio    node-seconds shared that way / (run_s x its own hosts)
perf.job.sw4_core_hours_share (event)    ratio    SW4 core-hours / all the cycle's core-hours
ops.task.<task>.queue_s / .run_s         s        each task's last submission (task without ``_<res>``)
ops.task.<task>.submits                  count    submissions of the task
ops.task.<task>.time_limit_fraction      ratio    the largest run_s / time limit over its submissions
ops.time_to_results_s                    s        first job submission of the cycle -> upload_<res> succeeded
ops.time_to_results_from_origin_s        s        event origin -> upload_<res> succeeded
ops.cycle_start_delay_s (event)          s        origin -> the first cycle that took the event: its new_cmt
                                                  broadcast in the scheduler logs, else the cycle's first job
ops.jobs / ops.jobs_failed (event)       count    jobs, and failed or submit-failed ones
ops.extra_submits (event)                count    sum over tasks of submissions - 1
ops.core_hours (event)                   core-h   sum of the cycle's jobs' core-hours
======================================== ======== ==============================================================

A job's queue time is computed from its own times, not the monitor's ``queue_s``: for a
job that never started, the monitor counts up to its latest upload, so a job killed in the
queue on 3 November reads as queued for 15 days. Its wait then comes from the
``[jobs-kill out] <time>`` line of its ``job_err``, or is unknown.
"""

import csv
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_scan.db import get_metric, set_metric
from auto_scan.registry import event_extractor, extractor

# The CFL number the cost model's steps_per_second is measured at (REFERENCE_CFL in
# realtime_simulation_workflow's bin/sw4_cost_model.py).
REFERENCE_CFL = 0.9
KNOWN_RESOLUTIONS = ("0_25hz", "0_5hz", "1hz")
TIMESTEPPING = "solve/time_stepping"


def _both(sub: str) -> list[str]:
    return [f"{TIMESTEPPING}/predictor/{sub}", f"{TIMESTEPPING}/corrector/{sub}"]


# Time-stepping phases, each the sum of its predictor and corrector regions.
PHASES: dict[str, list[str]] = {
    "curvilinear": _both("evalRHS/curvilinear"),
    "cartesian": _both("evalRHS/cartesian"),
    "attenuation": _both("evalRHS/curvilinear/attenuation") + _both("evalRHS/cartesian/attenuation"),
    "enforce_ic": _both("enforceIC"),
    "communicate": _both("communicate"),
    "memvar": [*_both("memvar"), f"{TIMESTEPPING}/corrector/memvar_corrected"],
    "enforce_bc": _both("enforceBC"),
    "output": [f"{TIMESTEPPING}/output"],
}
IMBALANCE_PHASES = ("curvilinear", "cartesian", "memvar")
IO_REGIONS = {
    "output": f"{TIMESTEPPING}/output",
    "write_timeseries": "write_timeseries",
    "parse_input": "parse_input",
    "setup": "setupRun",
}
TIMER_COLUMNS = (
    "region",
    "depth",
    "calls_total",
    "ranks",
    "min_s",
    "avg_s",
    "max_s",
    "wall_avg_s",
    "mpi_min_s",
    "mpi_avg_s",
    "mpi_max_s",
    "omp_wait_frac",
    "omp_imb_avg",
    "omp_imb_max",
)


def _warn(message: str) -> None:
    print(f"auto_scan.extract.perf: {message}", file=sys.stderr)


def _read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from a file, or None (with a warning if it exists but is bad)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        _warn(f"{path}: {error}")
        return None
    if not isinstance(data, dict):
        _warn(f"{path}: not a JSON object")
        return None
    return data


def _num(value: Any) -> float | None:
    """A finite float, or None."""
    if isinstance(value, bool):
        return float(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def parse_time(value: str | None) -> datetime | None:
    """An ISO 8601 time, or None.

    Parameters
    ----------
    value : str or None
        e.g. ``2026-11-18T09:15:48+13:00``.

    Returns
    -------
    datetime or None
        The time, or None when missing or unreadable.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def event_cycle(db: sqlite3.Connection, event_id: str) -> str | None:
    """The event's cycle point as the ``jobs`` table spells it.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Returns
    -------
    str or None
        The cycle, or None if the event or its cycle is unknown.
    """
    row = db.execute("SELECT cycle FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if row is None or row["cycle"] is None:
        return None
    cycle = str(row["cycle"])
    return cycle if cycle_matches_event(db, event_id, cycle) else None


# A run's manifest is written by its upload or failure handler, after the cycle's first
# job and at most a little after its last; a day either side allows for a late manual
# re-upload without letting another install's cycle of the same number match.
CYCLE_MATCH_SLACK_S = 86400.0


def cycle_matches_event(db: sqlite3.Connection, event_id: str, cycle: str) -> bool:
    """Whether the ``jobs`` table's ``cycle`` is the run that wrote this event's results.

    Cycle numbers restart with each install of the workflow, so event directories from
    earlier installs (``1-<event>`` on the results store) share a cycle number with the
    monitor's current one. The cycle's jobs belong to the event only if one of the
    event's manifests was written within the cycle's job window (with
    ``CYCLE_MATCH_SLACK_S`` either side). An event with no manifest times, or a cycle
    with no jobs, matches.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.
    cycle : str
        The cycle point.

    Returns
    -------
    bool
        False when the event's manifests were all written outside the cycle's jobs.
    """
    written = [
        moment
        for row in db.execute("SELECT written FROM runs WHERE event_id = ?", (event_id,))
        if (moment := parse_time(row["written"])) is not None and moment.tzinfo is not None
    ]
    times = [
        moment
        for row in db.execute(
            "SELECT time_submit, time_run_exit FROM jobs WHERE cycle = ?",
            (cycle,),
        )
        for value in (row["time_submit"], row["time_run_exit"])
        if (moment := parse_time(value)) is not None and moment.tzinfo is not None
    ]
    if not written or not times:
        return True
    first, last = min(times), max(times)
    return any(
        (first - moment).total_seconds() <= CYCLE_MATCH_SLACK_S
        and (moment - last).total_seconds() <= CYCLE_MATCH_SLACK_S
        for moment in written
    )


def split_task(name: str) -> tuple[str, str]:
    """A task name's base and resolution: ``sw4_0_5hz`` -> ``("sw4", "0_5hz")``.

    Parameters
    ----------
    name : str
        The Cylc task name.

    Returns
    -------
    tuple of str
        The name without its ``_<resolution>`` suffix, and the resolution (``""`` for a
        task shared by every resolution).
    """
    for resolution in KNOWN_RESOLUTIONS:
        if name.endswith(f"_{resolution}"):
            return name[: -len(resolution) - 1], resolution
    return name, ""


def job_log_dir(run_dir: Path, cycle: str, task: str, filename: str) -> Path | None:
    """The newest job log directory of a task that holds a file.

    Parameters
    ----------
    run_dir : Path
        The run's cache directory.
    cycle : str
        The cycle point.
    task : str
        The task name, e.g. ``sw4_0_5hz``.
    filename : str
        The file (or directory) wanted, e.g. ``sw4_timers.csv``.

    Returns
    -------
    Path or None
        ``logs/log/job/<cycle>/<task>/<NN>`` with the highest ``NN`` holding the file.
    """
    for root in (run_dir / "logs" / "log" / "job", run_dir / "logs" / "job"):
        base = root / cycle / task
        if not base.is_dir():
            continue
        submissions = sorted(
            (path for path in base.iterdir() if path.name.isdigit() and path.is_dir()),
            key=lambda path: int(path.name),
            reverse=True,
        )
        for submission in submissions:
            if (submission / filename).exists():
                return submission
    return None


def parse_cpu_list(text: str) -> set[int]:
    """CPUs from a list such as ``98,99,108-113``.

    Parameters
    ----------
    text : str
        A Linux CPU list.

    Returns
    -------
    set of int
        The CPUs; empty if unreadable.
    """
    cpus: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            cpus.update(range(int(low), int(high or low) + 1))
        except ValueError:
            return set()
    return cpus


@dataclass
class RankInfo:
    """What ``numa_bind.sh`` wrote about one rank (``ranks/rank-<n>.txt``).

    Parameters
    ----------
    rank : int
        The MPI rank.
    host : str
        The node it ran on.
    cpus : set of int
        The CPUs it was bound to.
    numa_nodes : list of str
        The NUMA nodes those CPUs are on.
    max_rss_kb : float or None
        Its peak resident memory, if recorded.
    """

    rank: int
    host: str
    cpus: set[int]
    numa_nodes: list[str]
    max_rss_kb: float | None


RANK_HOST = re.compile(r"^rank (\d+) on (\S+)", re.MULTILINE)
RANK_CPUS = re.compile(r"^cpus (.+)$", re.MULTILINE)
RANK_NODEBIND = re.compile(r"^nodebind:(.*)$", re.MULTILINE)
RANK_MAX_RSS = re.compile(r"^max_rss_kb (\d+)", re.MULTILINE)


def parse_rank_file(text: str) -> RankInfo | None:
    """One rank's binding and peak memory.

    Parameters
    ----------
    text : str
        The contents of ``rank-<n>.txt``.

    Returns
    -------
    RankInfo or None
        None when the file doesn't name its rank and host.
    """
    host = RANK_HOST.search(text)
    if not host:
        return None
    cpus = RANK_CPUS.search(text)
    nodebind = RANK_NODEBIND.search(text)
    rss = RANK_MAX_RSS.search(text)
    return RankInfo(
        rank=int(host.group(1)),
        host=host.group(2),
        cpus=parse_cpu_list(cpus.group(1)) if cpus else set(),
        numa_nodes=nodebind.group(1).split() if nodebind else [],
        max_rss_kb=float(rss.group(1)) if rss else None,
    )


def queue_wait_s(row: sqlite3.Row | dict[str, Any]) -> float | None:
    """How long a job waited in the queue, from its own times.

    Parameters
    ----------
    row : sqlite3.Row or dict
        A ``jobs`` row.

    Returns
    -------
    float or None
        ``time_run - time_submit``; for a job that never started, the time to the
        ``[jobs-kill out]`` line of its ``job_err``; for one still queued, the monitor's
        ``queue_s``; otherwise None. The monitor's ``queue_s`` of a killed job runs on to
        its latest upload, so it is not used for one.
    """
    submitted = parse_time(row["time_submit"])
    started = parse_time(row["time_run"])
    if submitted and started:
        return (started - submitted).total_seconds()
    if row["state"] == "submitted":
        return _num(row["queue_s"])
    killed = re.search(r"\[jobs-kill out\] (\S+?)\|", row["job_err"] or "")
    if submitted and killed:
        kill_time = parse_time(killed.group(1))
        if kill_time:
            return (kill_time - submitted).total_seconds()
    return None


def run_seconds(row: sqlite3.Row | dict[str, Any]) -> float | None:
    """A job's run time, from its own times or the monitor's ``run_s``.

    Parameters
    ----------
    row : sqlite3.Row or dict
        A ``jobs`` row.

    Returns
    -------
    float or None
        Seconds from start to exit, or None if it never started.
    """
    started = parse_time(row["time_run"])
    ended = parse_time(row["time_run_exit"])
    if started and ended:
        return (ended - started).total_seconds()
    return _num(row["run_s"]) if started else None


class _Run:
    """One run being extracted: where its files are and how to record a metric."""

    def __init__(self, db: sqlite3.Connection, event_id: str, resolution: str, run_dir: Path) -> None:
        self.db = db
        self.event_id = event_id
        self.resolution = resolution
        self.run_dir = run_dir
        self.manifest = _read_json(run_dir / "manifest.json") or {}
        self.cycle = str(self.manifest.get("cycle_point") or event_cycle(db, event_id) or "")

    def put(self, metric: str, value: float | None, unit: str | None = None, detail: Any = None) -> None:
        if value is None:
            return
        set_metric(self.db, self.event_id, self.resolution, metric, float(value), unit, detail)

    def get(self, metric: str) -> float | None:
        return get_metric(self.db, self.event_id, self.resolution, metric)

    def log_dir(self, task: str, filename: str) -> Path | None:
        if not self.cycle:
            return None
        return job_log_dir(self.run_dir, self.cycle, f"{task}_{self.resolution}", filename)

    def file(self, task: str, filename: str) -> Path | None:
        directory = self.log_dir(task, filename)
        return None if directory is None else directory / filename

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.run_dir.parent))
        except ValueError:
            return str(path)


def central_core_hours(estimate: dict[str, Any]) -> float | None:
    """The cost model's estimate from its factors' central values, with no caution.

    Parameters
    ----------
    estimate : dict
        ``sw4_estimate.json``.

    Returns
    -------
    float or None
        Effective core-hours (divided by the central multi-node efficiency when the job may
        span several nodes), or None when a factor is missing.
    """
    model = estimate.get("cost_model") or {}

    def value(name: str) -> float | None:
        """A cost-model factor's central value.

        Parameters
        ----------
        name : str
            The factor.

        Returns
        -------
        float or None
            Its value, or None if the estimate lacks it.
        """
        factor = model.get(name)
        return _num(factor.get("value")) if isinstance(factor, dict) else None

    points_factor, steps_per_second = value("points_factor"), value("steps_per_second")
    speed, efficiency = value("speed"), value("multinode_efficiency")
    padded, duration = _num(estimate.get("padded_gridpoints")), _num(estimate.get("duration_s"))
    cfl = _num(estimate.get("cfl")) or REFERENCE_CFL
    if None in (points_factor, steps_per_second, speed, padded, duration) or not speed:
        return None
    assert points_factor is not None and steps_per_second is not None and padded is not None
    assert duration is not None
    nodes = _num(estimate.get("nodes_up_to")) or _num(estimate.get("nodes")) or 1.0
    spread = 1.0 if nodes <= 1 or not efficiency else min(1.0, efficiency)
    seconds = points_factor * padded * steps_per_second * duration * REFERENCE_CFL / cfl / speed / spread
    return seconds / 3600


SIZE_LINE = re.compile(
    r"^reach (\S+) km: (\S+) x (\S+) km, \S+ s, \S+ points, (\S+) core-hours: (fits|does not fit)", re.MULTILINE
)


def _sw4_run_metrics(run: _Run) -> None:
    record = _read_json(run.run_dir / "sw4_run.json")
    estimate = _read_json(run.run_dir / "sw4_estimate.json") or run.manifest.get("sw4_estimate")
    if not isinstance(estimate, dict):
        estimate = None
    if record is None and estimate is None:
        return
    record = record or {}
    put = run.put
    source = {"file": "sw4_run.json"}
    points, steps = _num(record.get("points")), _num(record.get("steps"))
    cores, wall = _num(record.get("cores")), _num(record.get("timestep_wall_s"))
    put("perf.points", points, "points", source)
    put("perf.steps", steps, "steps", source)
    put("perf.cores", cores, "cores", source)
    put("perf.ranks", _num(record.get("ranks")), "ranks", source)
    put("perf.threads", _num(record.get("threads_per_rank")), "threads", source)
    put("perf.nodes", _num(record.get("nodes")), "nodes", source)
    put("perf.completed", _num(record.get("completed")), "bool", source)
    put("perf.timestep_wall_s", wall, "s", source)
    put(
        "perf.updates_per_core_s",
        _num(record.get("updates_per_core_s")),
        "1/s",
        {
            "file": "sw4_run.json",
            "sw4_commit": record.get("sw4_commit"),
            "sw4_build": record.get("sw4_build"),
            "launcher": record.get("launcher"),
            "nodes": record.get("nodes"),
            "cores": record.get("cores"),
        },
    )
    put("perf.steps_per_simulated_s", _num(record.get("steps_per_simulated_s")), "1/s", source)
    put("perf.comm_wait_fraction", _num(record.get("comm_wait_fraction")), "ratio", source)
    put("perf.cfl", _num(record.get("cfl") or (estimate or {}).get("cfl")), None, source)
    actual = wall * cores / 3600 if wall and cores else None
    put("perf.actual_core_hours", actual, "core-h", {"formula": "timestep_wall_s x cores / 3600"})
    if estimate is None:
        return

    est_source = {"file": "sw4_estimate.json"}
    est_points, est_steps = _num(estimate.get("gridpoints")), _num(estimate.get("time_steps"))
    core_hours = _num(estimate.get("core_hours"))
    efficiency = _num(estimate.get("multinode_efficiency")) or 1.0
    effective = core_hours / efficiency if core_hours else None
    central = central_core_hours(estimate)
    put("perf.estimated_points", est_points, "points", est_source)
    put("perf.estimated_steps", est_steps, "steps", est_source)
    # A run that stopped part-way (cancelled, failed, out of time) timed only some of the
    # steps the estimate is for: 2026p862161's 1 Hz run, cancelled at step 991, read as a
    # 102x over-estimate. Its speed still counts; its totals don't compare.
    finished = record.get("completed") is True
    put("perf.points_ratio", _ratio(est_points, points), "ratio")
    put("perf.steps_ratio", _ratio(est_steps, steps) if finished else None, "ratio")
    put("perf.estimated_core_hours", core_hours, "core-h", est_source)
    put(
        "perf.estimated_core_hours_effective",
        effective,
        "core-h",
        {"formula": "core_hours / multinode_efficiency", "multinode_efficiency": efficiency},
    )
    put("perf.central_core_hours", central, "core-h", {"formula": "central factors, no z spreads"})
    put("perf.cost_model_caution", _ratio(effective, central), "ratio")
    cost_ratio = _ratio(effective, actual) if finished else None
    put(
        "perf.cost_ratio",
        cost_ratio,
        "ratio",
        {"estimated_effective": effective, "actual": actual, "scope": "time stepping from t=0"},
    )
    put("perf.central_cost_ratio", _ratio(central, actual) if finished else None, "ratio")
    speed = _num(estimate.get("point_steps_per_core_second"))
    put("perf.speed_ratio", _ratio(_num(record.get("updates_per_core_s")), speed * efficiency if speed else None))
    put("perf.budget_core_hours", _num(estimate.get("budget_core_hours")), "core-h", est_source)

    put("perf.extent_x_km", _num(estimate.get("extent_x_km")), "km")
    put("perf.extent_y_km", _num(estimate.get("extent_y_km")), "km")
    area = _num(estimate.get("area_km2"))
    put("perf.domain_area_km2", area, "km2")
    put("perf.reach_km", _num(estimate.get("reach_km")), "km")
    full = estimate.get("full_domain") if isinstance(estimate.get("full_domain"), dict) else None
    if full is not None:
        full_area = _num(full.get("area_km2"))
        put("perf.full_domain_area_km2", full_area, "km2")
        put("perf.full_reach_km", _num(full.get("reach_km")), "km")
        put(
            "perf.domain_fraction",
            _ratio(area, full_area),
            "ratio",
            {
                "extent_km": [estimate.get("extent_x_km"), estimate.get("extent_y_km")],
                "full_extent_km": [full.get("extent_x_km"), full.get("extent_y_km")],
            },
        )
        put("perf.domain_fits", _num(bool(full.get("fits"))), "bool")
        full_effective = _num(full.get("core_hours"))
        full_effective = full_effective / efficiency if full_effective else None
        put("perf.full_domain_core_hours_effective", full_effective, "core-h")
        put("perf.full_domain_core_hours_measured", _ratio(full_effective, cost_ratio), "core-h")
        if cost_ratio and full_area:
            _attainable_domain(run, efficiency, cost_ratio, full_area)

    model = estimate.get("cost_model") or {}
    for name in ("points_factor", "steps_per_second", "speed", "multinode_efficiency"):
        factor = model.get(name)
        if isinstance(factor, dict):
            put(f"perf.model.{name}", _num(factor.get("value")), None, factor)
            put(f"perf.model.{name}_spread", _num(factor.get("spread")), "log")
    speed_factor = model.get("speed")
    if isinstance(speed_factor, dict):
        put("perf.model.speed_runs", _num(speed_factor.get("runs")), "weight")
    put("perf.model.runs", _num(model.get("runs")), "runs")
    put("perf.model.z", _num(model.get("z")), None)


def _attainable_domain(run: _Run, efficiency: float, cost_ratio: float, full_area: float) -> None:
    """The largest domain size_domain tried that the measured cost would have fitted."""
    path = run.file("size_domain", "job.out")
    budget = run.get("perf.budget_core_hours")
    if path is None or not budget:
        return
    tried: list[dict[str, float]] = []
    for match in SIZE_LINE.finditer(path.read_text(errors="replace")):
        # size_domain.py prints thousands with separators ("8,675 core-hours").
        reach, x_km, y_km, core_hours = (_num(match.group(i).replace(",", "")) for i in range(1, 5))
        if None in (reach, x_km, y_km, core_hours):
            continue
        assert reach is not None and x_km is not None and y_km is not None and core_hours is not None
        measured = core_hours / efficiency / cost_ratio
        tried.append({"reach_km": reach, "area_km2": x_km * y_km, "measured_core_hours": measured})
    fitting = [domain for domain in tried if domain["measured_core_hours"] <= budget]
    if not fitting:
        return
    best = max(fitting, key=lambda domain: domain["area_km2"])
    run.put(
        "perf.domain_fraction_measured",
        min(1.0, best["area_km2"] / full_area),
        "ratio",
        {"file": run.relative(path), "largest_fitting": best, "budget_core_hours": budget, "cost_ratio": cost_ratio},
    )


def read_timers(path: Path) -> dict[str, dict[str, Any]]:
    """``sw4_timers.csv`` by region, numbers as floats.

    Parameters
    ----------
    path : Path
        The CSV.

    Returns
    -------
    dict
        Each region's row; numeric columns are floats (None if blank).
    """
    rows: dict[str, dict[str, Any]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            region = row.get("region")
            if not region:
                continue
            rows[region] = {key: (value if key == "region" else _num(value)) for key, value in row.items() if key}
    return rows


def _timer_metrics(run: _Run) -> None:
    path = run.file("sw4", "sw4_timers.csv")
    if path is None:
        return
    timers = read_timers(path)
    if not timers:
        _warn(f"{path}: no timer rows")
        return
    run.db.executemany(
        f"INSERT OR REPLACE INTO sw4_timers (event_id, resolution, {', '.join(TIMER_COLUMNS)})"
        f" VALUES (?, ?, {', '.join('?' * len(TIMER_COLUMNS))})",
        [(run.event_id, run.resolution, *(row.get(column) for column in TIMER_COLUMNS)) for row in timers.values()],
    )
    put = run.put
    file = run.relative(path)

    def col(region: str, column: str) -> float | None:
        row = timers.get(region)
        return None if row is None else row.get(column)

    def total(regions: Iterable[str], column: str) -> float | None:
        values = [col(region, column) for region in regions]
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    def per_rank(region: str, column: str = "calls_total") -> float | None:
        calls, ranks = col(region, column), col(region, "ranks")
        return calls / ranks if calls is not None and ranks else col(region, "calls_max")

    wall = next((row.get("wall_avg_s") for row in timers.values() if row.get("wall_avg_s")), None)
    solve, stepping = col("solve", "avg_s"), col(TIMESTEPPING, "avg_s")
    steps = per_rank(f"{TIMESTEPPING}/predictor")
    put("perf.timers.wall_s", wall, "s", {"file": file})
    put("perf.timers.solve_s", solve, "s", {"file": file})
    put("perf.timers.time_stepping_s", stepping, "s", {"file": file})
    put("perf.timers.steps", steps, "steps", {"file": file, "region": f"{TIMESTEPPING}/predictor"})
    put("perf.timers.output_steps", per_rank(f"{TIMESTEPPING}/output"), "steps")
    recorded_steps = run.get("perf.steps")
    if steps and recorded_steps:
        put(
            "perf.steps_uncounted_fraction",
            (steps - recorded_steps) / steps,
            "ratio",
            {"timer_steps": steps, "sw4_run_steps": recorded_steps, "file": file},
        )

    mpi = {"file": file, "region": "solve"}
    put("perf.mpi_fraction", _ratio(col("solve", "mpi_avg_s"), solve), "ratio", mpi)
    put("perf.mpi_max_min", _ratio(col("solve", "mpi_max_s"), col("solve", "mpi_min_s")), "ratio", mpi)
    for region, row in timers.items():
        if region.startswith("MPI_") and row.get("mpi_avg_s"):
            put(
                f"perf.mpi_share.{region.removeprefix('MPI_').lower()}",
                _ratio(row["mpi_avg_s"], solve),
                "ratio",
                {
                    "file": file,
                    "mpi_min_s": row.get("mpi_min_s"),
                    "mpi_avg_s": row.get("mpi_avg_s"),
                    "mpi_max_s": row.get("mpi_max_s"),
                    "calls_avg": row.get("mpi_calls_avg"),
                },
            )
    put("perf.allreduce_per_step", _ratio(col("MPI_Allreduce", "mpi_calls_avg"), steps), "count")
    put("perf.barrier_per_step", _ratio(col("MPI_Barrier", "mpi_calls_avg"), steps), "count")

    eval_rhs = _both("evalRHS")
    rhs_max, rhs_avg, rhs_min = (total(eval_rhs, column) for column in ("max_s", "avg_s", "min_s"))
    put(
        "perf.rank_imbalance",
        _ratio(rhs_max, rhs_avg),
        "ratio",
        {"file": file, "regions": eval_rhs, "min_s": rhs_min, "avg_s": rhs_avg, "max_s": rhs_max},
    )
    for phase in IMBALANCE_PHASES:
        regions = PHASES[phase]
        high, mean, low = (total(regions, column) for column in ("max_s", "avg_s", "min_s"))
        put(
            f"perf.imbalance.{phase}.max_avg", _ratio(high, mean), "ratio", {"min_s": low, "avg_s": mean, "max_s": high}
        )
        put(f"perf.imbalance.{phase}.max_min", _ratio(high, low), "ratio")
    for phase, regions in PHASES.items():
        put(f"perf.share.{phase}", _ratio(total(regions, "avg_s"), stepping), "ratio", {"regions": regions})

    jacobi = [f"{region}/consintp/jacobi" for region in _both("enforceIC")]
    jacobi_calls = [per_rank(region) for region in jacobi]
    if steps and all(calls is not None for calls in jacobi_calls):
        put(
            "perf.interface_jacobi_per_step",
            sum(calls for calls in jacobi_calls if calls is not None) / steps,
            "count",
            {"regions": jacobi, "calls_per_rank": jacobi_calls, "steps": steps},
        )

    io_parts = {name: col(region, "avg_s") for name, region in IO_REGIONS.items()}
    present = {name: value for name, value in io_parts.items() if value is not None}
    if wall and present:
        put("perf.io_share", sum(present.values()) / wall, "ratio", {"seconds": present, "wall_s": wall, "file": file})
        for name, value in present.items():
            put(f"perf.io.{name}_share", value / wall, "ratio")

    wait_frac = col(TIMESTEPPING, "omp_wait_frac")
    imb_max = max((row.get("omp_imb_max") or 0.0 for row in timers.values()), default=0.0)
    if wait_frac:
        put("perf.omp.wait_frac", wait_frac, "ratio", {"region": TIMESTEPPING})
    if imb_max:
        worst = max(timers.values(), key=lambda row: row.get("omp_imb_max") or 0.0)
        put("perf.omp.imb_max", imb_max, "ratio", {"region": worst["region"]})


OMP_PLACE = re.compile(r"OMP_PLACES = '\{\d+:(\d+)\}")
GRID_ROW = re.compile(
    r"^\s*(\d+)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(Cartesian|Curvilinear)\s*$", re.MULTILINE
)
TIME_STEP = re.compile(r"^\s*time step = (\S+)", re.MULTILINE)
PREROLL = re.compile(r"with (\d+) pre-roll steps")
RECEIVERS = re.compile(r"Supergrid receiver check: (\d+) of (\d+) receivers")
# SW4 skips an option its command does not know with only this warning (parseInputFile.C).
IGNORED_OPTION = re.compile(r"Warning: ignoring (\S+) line option '([^']*)'")
ATTENUATION_FIT = re.compile(
    r"Attenuation parameters calculated for (\d+) mechanisms,\s*max freq=(\S+) \[Hz\], min_freq=(\S+) \[Hz\],"
    r" velo_freq=(\S+) \[Hz\]"
)
LOG_CFL = re.compile(r"CFL=(\S+)")
SPREAD_TABLE = re.compile(r"Per-rank spread over (\d+) ranks\n(.*?)\n-{10,}", re.DOTALL)
SPREAD_ROW = re.compile(r"^(\S+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\S+)\s*$", re.MULTILINE)


def _log_metrics(run: _Run, log: str) -> None:
    put = run.put
    grids = [
        {"grid": int(g), "h_m": float(h), "nx": int(nx), "ny": int(ny), "nz": int(nz), "points": int(p), "type": kind}
        for g, h, nx, ny, nz, p, kind in GRID_ROW.findall(log)
    ]
    if grids:
        put("perf.sw4.grids", len(grids), "count", grids)
        put("perf.sw4.finest_h_m", min(grid["h_m"] for grid in grids), "m")
        all_points = sum(grid["points"] for grid in grids)
        curvilinear = sum(grid["points"] for grid in grids if grid["type"] == "Curvilinear")
        put("perf.sw4.curvilinear_points_fraction", _ratio(curvilinear, all_points), "ratio")
    step = TIME_STEP.findall(log)
    if step:
        put("perf.sw4.dt_s", _num(step[-1]), "s")
    preroll = PREROLL.search(log)
    if preroll:
        put("perf.sw4.preroll_steps", float(preroll.group(1)), "steps")
    receivers = RECEIVERS.search(log)
    if receivers:
        put("perf.sw4.receivers", float(receivers.group(2)), "count", {"in_sponge": int(receivers.group(1))})
    ignored = [{"command": command, "option": option} for command, option in IGNORED_OPTION.findall(log)]
    put("perf.sw4.ignored_options", len(ignored), "count", ignored)
    cfl = LOG_CFL.search(log)
    if cfl:
        put("perf.sw4.log_cfl", _num(cfl.group(1)), "-")
    fit = ATTENUATION_FIT.search(log)
    if fit:
        used = {
            "nmech": int(fit.group(1)),
            "maxfreq": _num(fit.group(2)),
            "minfreq": _num(fit.group(3)),
            "phasefreq": _num(fit.group(4)),
        }
        requested = _requested_attenuation(run.run_dir / "realisation.json")
        mismatched = sorted(
            key
            for key in ("nmech", "maxfreq", "phasefreq")
            if requested is not None
            and requested.get(key) is not None
            and not math.isclose(float(requested[key]), float(used[key] or 0), rel_tol=1e-3)
        )
        if requested is None or not {"nmech", "maxfreq", "phasefreq"} <= set(requested):
            # The realisation asks for attenuation but leaves some of it to SW4's defaults.
            mismatched += [f"{key} unset" for key in ("nmech", "maxfreq", "phasefreq") if key not in (requested or {})]
        put(
            "perf.sw4.attenuation_mismatches",
            len(mismatched),
            "count",
            {"used": used, "requested": requested, "mismatched": mismatched},
        )


def _requested_attenuation(realisation: Path) -> dict[str, Any] | None:
    """The realisation's SW4 ``attenuation`` command parameters, or None without one."""
    data = _read_json(realisation) or {}
    commands = (data.get("sw4") or {}).get("commands") or []
    for command in commands:
        if isinstance(command, dict) and command.get("name") == "attenuation":
            return dict(command.get("parameters") or {})
    return None


def _placement_metrics(run: _Run) -> None:
    put = run.put
    log_path = run.file("sw4", "sw4.log")
    log = log_path.read_text(errors="replace") if log_path else ""
    if log:
        _log_metrics(run, log)

    hosts_path = run.file("sw4", "sw4_hosts")
    hosts = hosts_path.read_text().split() if hosts_path else []
    if hosts:
        per_host = Counter(hosts)
        put("perf.placement.hosts", len(per_host), "count", dict(per_host))
        put("perf.placement.max_ranks_per_host", max(per_host.values()), "ranks", dict(per_host))
        put("perf.placement.min_ranks_per_host", min(per_host.values()), "ranks")

    ranks_dir = run.log_dir("sw4", "ranks")
    ranks: list[RankInfo] = []
    if ranks_dir is not None:
        for path in sorted((ranks_dir / "ranks").glob("rank-*.txt")):
            info = parse_rank_file(path.read_text(errors="replace"))
            if info is None:
                _warn(f"{path}: no rank/host line")
            else:
                ranks.append(info)
    if ranks:
        _binding_metrics(run, ranks, log)
    _memory_metrics(run, ranks)

    if log:
        tables = SPREAD_TABLE.findall(log)
        if tables:
            phases: dict[str, dict[str, Any]] = {
                name: {"min": _num(low), "max": _num(high), "max_rank": int(rank), "max_over_mean": _num(ratio)}
                for name, low, high, rank, ratio in SPREAD_ROW.findall(tables[-1][1])
                if name != "Phase"
            }
            div = phases.get("Div-stress")
            if div:
                slowest = int(div["max_rank"])
                where = next((info for info in ranks if info.rank == slowest), None)
                put(
                    "perf.slowest_rank",
                    slowest,
                    "rank",
                    {
                        "phase": "Div-stress",
                        "host": where.host if where else (hosts[slowest] if slowest < len(hosts) else None),
                        "cpus": len(where.cpus) if where else None,
                        "phases": phases,
                        "file": run.relative(log_path) if log_path else None,
                    },
                )


def _binding_metrics(run: _Run, ranks: list[RankInfo], log: str) -> None:
    put = run.put
    threads = run.get("perf.threads")
    widths = Counter(len(info.cpus) for info in ranks if info.cpus)
    if not widths:
        return
    place = OMP_PLACE.search(log)
    modal = widths.most_common(1)[0][0]
    if place:
        per_core = int(place.group(1))
    elif threads:
        per_core = max(1, round(modal / threads))
    else:
        per_core = 1
    expected = int(threads * per_core) if threads else modal
    put(
        "perf.placement.expected_cpu_width",
        expected,
        "cpus",
        {"threads": threads, "hardware_threads_per_core": per_core, "widths": dict(widths)},
    )
    narrow = [
        {"rank": info.rank, "host": info.host, "cpus": len(info.cpus), "cores": len(info.cpus) / per_core}
        for info in ranks
        if info.cpus and len(info.cpus) < expected
    ]
    put("perf.placement.narrow_ranks", len(narrow), "ranks", {"expected_cpus": expected, "ranks": narrow})
    overlapping: set[int] = set()
    by_host: dict[str, list[RankInfo]] = defaultdict(list)
    for info in ranks:
        by_host[info.host].append(info)
    pairs = []
    for infos in by_host.values():
        for i, first in enumerate(infos):
            for second in infos[i + 1 :]:
                if first.cpus & second.cpus:
                    overlapping.update((first.rank, second.rank))
                    pairs.append([first.rank, second.rank, first.host])
    put("perf.placement.overlapping_ranks", len(overlapping), "ranks", {"pairs": pairs[:50]})
    multi = [
        {"rank": info.rank, "host": info.host, "numa_nodes": info.numa_nodes}
        for info in ranks
        if len(info.numa_nodes) > 1
    ]
    put("perf.placement.multi_numa_ranks", len(multi), "ranks", multi)


SACCT_RSS = re.compile(r"^([\d.]+)([KMGT]?)$")
SACCT_UNITS = {"": 1 / 1024**3, "K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}


def _sacct_gb(value: str) -> float | None:
    match = SACCT_RSS.match(value.strip())
    if not match:
        return None
    return float(match.group(1)) * SACCT_UNITS[match.group(2)]


def _memory_metrics(run: _Run, ranks: list[RankInfo]) -> None:
    put = run.put
    peaks_gb = [info.max_rss_kb / 1024**2 for info in ranks if info.max_rss_kb]
    if ranks:
        ranks_dir = run.log_dir("sw4", "ranks")
        put(
            "perf.memory.recorded_fraction",
            len(peaks_gb) / len(ranks),
            "ratio",
            {
                "rank_files": len(ranks),
                "with_max_rss_kb": len(peaks_gb),
                "dir": run.relative(ranks_dir / "ranks") if ranks_dir else None,
            },
        )
    if peaks_gb:
        put("perf.memory.rank_peak_max_gb", max(peaks_gb), "GB")
        put("perf.memory.rank_peak_mean_gb", statistics.fmean(peaks_gb), "GB")

    sacct_peak = None
    path = run.file("record_sw4", "sacct.txt")
    if path is not None:
        lines = [line.split("|") for line in path.read_text(errors="replace").splitlines() if "|" in line]
        if len(lines) > 1:
            header = lines[0]
            rows = [dict(zip(header, line, strict=False)) for line in lines[1:]]
            steps = [row for row in rows if _num(row.get("NTasks")) and row.get("MaxRSS")]
            if steps:
                step = max(steps, key=lambda row: _num(row.get("NTasks")) or 0)
                sacct_peak = _sacct_gb(step["MaxRSS"])
                detail = {
                    "file": run.relative(path),
                    "step": step.get("JobID"),
                    "task": step.get("MaxRSSTask"),
                    "node": step.get("MaxRSSNode"),
                }
                put("perf.memory.sacct_max_rss_gb", sacct_peak, "GB", detail)
                put("perf.memory.sacct_ave_rss_gb", _sacct_gb(step.get("AveRSS", "")), "GB", detail)

    script = run.file("sw4", "job")
    if script is not None:
        text = script.read_text(errors="replace")
        per_cpu = re.search(r"^#SBATCH --mem-per-cpu=(\d+)([MG]?)", text, re.MULTILINE)
        cpus = re.search(r"^#SBATCH --cpus-per-task=(\d+)", text, re.MULTILINE)
        if per_cpu and cpus:
            gb = float(per_cpu.group(1)) / (1 if per_cpu.group(2) == "G" else 1024)
            request = gb * int(cpus.group(1))
            put("perf.memory.request_per_rank_gb", request, "GB", {"file": run.relative(script)})
            peak = max(peaks_gb) if peaks_gb else sacct_peak
            if peak:
                put(
                    "perf.memory.peak_fraction",
                    peak / request,
                    "ratio",
                    {"peak_gb": peak, "from": "rank files" if peaks_gb else "sacct MaxRSS"},
                )


def _clear_run(db: sqlite3.Connection, event_id: str, resolution: str) -> None:
    db.execute(
        "DELETE FROM metrics WHERE event_id = ? AND resolution = ? AND metric LIKE 'perf.%'"
        " AND metric NOT LIKE 'perf.job.%'",
        (event_id, resolution),
    )
    db.execute("DELETE FROM sw4_timers WHERE event_id = ? AND resolution = ?", (event_id, resolution))


@extractor
def extract_run(db: sqlite3.Connection, event_id: str, resolution: str, run_dir: Path) -> None:
    """Record a run's SW4 cost, timers, placement and memory metrics.

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
    _clear_run(db, event_id, resolution)
    run = _Run(db, event_id, resolution, run_dir)
    parts: dict[str, Callable[[_Run], None]] = {
        "sw4_run": _sw4_run_metrics,
        "timers": _timer_metrics,
        "placement": _placement_metrics,
    }
    for name, part in parts.items():
        try:
            part(run)
        except Exception as error:  # noqa: BLE001 - one bad file must not stop the scan
            _warn(f"{event_id} {resolution} {name}: {type(error).__name__}: {error}")


# ------------------------------------------------------------------------------- jobs --


@dataclass
class Job:
    """One ``jobs`` row, with its times parsed.

    Parameters
    ----------
    cycle : str
        The cycle point.
    name : str
        The task's name.
    base : str
        The name without its resolution suffix.
    resolution : str
        The resolution in the name, or ''.
    submit_num : int
        Which submission of the task this is.
    state : str
        The monitor's state for the job.
    submitted : datetime or None
        When it was submitted.
    started : datetime or None
        When it started running.
    ended : datetime or None
        When it exited.
    run_s : float or None
        Seconds running.
    queue_s : float or None
        Seconds queued, from the job's own times.
    cores : float or None
        Cores requested.
    time_limit_s : float or None
        Its time limit.
    core_hours : float or None
        Cores times hours running.
    job_err : str
        The end of its ``job.err``, for a failed job.
    """

    cycle: str
    name: str
    base: str
    resolution: str
    submit_num: int
    state: str
    submitted: datetime | None
    started: datetime | None
    ended: datetime | None
    run_s: float | None
    queue_s: float | None
    cores: float | None
    time_limit_s: float | None
    core_hours: float | None
    job_err: str

    @property
    def interval(self) -> tuple[datetime, datetime] | None:
        """The job's run, start to exit.

        Returns
        -------
        tuple of datetime, or None
            ``(started, ended)``, or None if it never started or hasn't ended.
        """
        if self.started is None:
            return None
        if self.ended is not None:
            return self.started, self.ended
        return None


def read_jobs(db: sqlite3.Connection, cycle: str | None = None) -> list[Job]:
    """``jobs`` rows as ``Job``s, oldest submission first.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    cycle : str, optional
        Only this cycle's jobs.

    Returns
    -------
    list of Job
        The jobs.
    """
    sql = "SELECT * FROM jobs"
    params: tuple[str, ...] = ()
    if cycle is not None:
        sql += " WHERE cycle = ?"
        params = (cycle,)
    jobs = []
    for row in db.execute(sql + " ORDER BY time_submit, submit_num", params):
        base, resolution = split_task(row["name"])
        limit = _num(row["time_limit_min"])
        jobs.append(
            Job(
                cycle=str(row["cycle"]),
                name=row["name"],
                base=base,
                resolution=resolution,
                submit_num=int(row["submit_num"]),
                state=row["state"] or "",
                submitted=parse_time(row["time_submit"]),
                started=parse_time(row["time_run"]),
                ended=parse_time(row["time_run_exit"]),
                run_s=run_seconds(row),
                queue_s=queue_wait_s(row),
                cores=_num(row["cores"]),
                time_limit_s=limit * 60 if limit else None,
                core_hours=_num(row["core_hours"]),
                job_err=row["job_err"] or "",
            )
        )
    return jobs


def _overlap_s(first: tuple[datetime, datetime], second: tuple[datetime, datetime]) -> float:
    return max(0.0, (min(first[1], second[1]) - max(first[0], second[0])).total_seconds())


def _sw4_hosts(event_dir: Path) -> dict[tuple[str, str], set[str]]:
    """Every SW4 job's hosts that any of the event's logs hold, by (cycle, task)."""
    hosts: dict[tuple[str, str], set[str]] = {}
    for path in event_dir.glob("*/logs/log/job/*/sw4_*/*/sw4_hosts"):
        if not path.parent.name.isdigit():
            continue
        try:
            hosts[(path.parents[2].name, path.parents[1].name)] = set(path.read_text().split())
        except OSError as error:
            _warn(f"{path}: {error}")
    return hosts


def _sw4_job_metrics(
    db: sqlite3.Connection,
    event_id: str,
    resolution: str,
    job: Job,
    all_jobs: list[Job],
    hosts: dict[tuple[str, str], set[str]],
) -> None:
    def put(metric: str, value: float | None, unit: str | None = None, detail: Any = None) -> None:
        if value is not None:
            set_metric(db, event_id, resolution, metric, value, unit, detail)

    where = {"cycle": job.cycle, "task": job.name, "submit_num": job.submit_num}
    cores = job.cores
    run_cores = get_metric(db, event_id, resolution, "perf.cores")
    if run_cores and (cores is None or cores < run_cores):
        cores = run_cores
    put("perf.job.run_s", job.run_s, "s", where)
    put("perf.job.queue_s", job.queue_s, "s", where)
    core_hours = cores * job.run_s / 3600 if cores and job.run_s else None
    put("perf.job.core_hours", core_hours, "core-h", where | {"cores": cores})
    estimate = get_metric(db, event_id, resolution, "perf.estimated_core_hours_effective")
    put("perf.job.cost_ratio", _ratio(estimate, core_hours), "ratio", {"estimated_effective": estimate})
    put("perf.job.time_limit_fraction", _ratio(job.run_s, job.time_limit_s), "ratio", where)

    interval = job.interval
    if interval is None or not cores:
        return
    duration = (interval[1] - interval[0]).total_seconds()
    if duration <= 0:
        return
    other_core_s = sw4_core_s = shared_node_s = 0.0
    concurrent = []
    shared: set[str] = set()
    own_hosts = hosts.get((job.cycle, job.name), set())
    for other in all_jobs:
        if other is job or other.interval is None or not other.cores:
            continue
        overlap = _overlap_s(interval, other.interval)
        if overlap <= 0:
            continue
        other_core_s += overlap * other.cores
        if other.base == "sw4":
            sw4_core_s += overlap * other.cores
            other_hosts = hosts.get((other.cycle, other.name), set())
            common = sorted(own_hosts & other_hosts)
            shared.update(common)
            shared_node_s += overlap * len(common)
            concurrent.append(
                {"cycle": other.cycle, "task": other.name, "overlap_s": overlap, "cores": other.cores, "shared": common}
            )
    contention = other_core_s / duration
    put("perf.job.contention_cores", contention, "cores", {"run_s": duration, "scope": "this workflow's jobs only"})
    put("perf.job.contention_ratio", contention / cores, "ratio")
    put("perf.job.concurrent_sw4_cores", sw4_core_s / duration, "cores", concurrent)
    if own_hosts:
        put("perf.job.shared_nodes", len(shared), "nodes", {"hosts": sorted(shared), "own_hosts": sorted(own_hosts)})
        put("perf.job.shared_node_fraction", shared_node_s / (duration * len(own_hosts)), "ratio")


@event_extractor
def extract_event_jobs(db: sqlite3.Connection, event_id: str, event_dir: Path) -> None:
    """Record job, queue, contention and latency metrics of an event from the ``jobs`` table.

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
        "DELETE FROM metrics WHERE event_id = ? AND (metric LIKE 'perf.job.%' OR metric LIKE 'ops.%')", (event_id,)
    )
    try:
        _event_jobs(db, event_id, event_dir)
    except Exception as error:  # noqa: BLE001 - one bad event must not stop the scan
        _warn(f"{event_id} jobs: {type(error).__name__}: {error}")


LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d) ")
NEW_CMT_BROADCAST = re.compile(r"\+ \[(\d+)/resolve_sources\] \[environment\]new_cmt_event_id=(\S+)")


def first_pickup(event_dir: Path, event_id: str) -> tuple[datetime, str] | None:
    """When the workflow first took this event, and in which cycle, from its scheduler logs.

    The ``new_cmt`` xtrigger broadcasts ``new_cmt_event_id`` to the cycle that takes an
    event. A re-run of the event in a later cycle (a person removing the first cycle) is
    not when it was picked up, and the collector keeps only the latest simulation's directory,
    so the earliest broadcast in any uploaded ``log/scheduler/*.log`` is used.

    Parameters
    ----------
    event_dir : Path
        The event's cache directory.
    event_id : str
        The event.

    Returns
    -------
    tuple of (datetime, str) or None
        The broadcast time and cycle, or None if no scheduler log names the event.
    """
    best: tuple[datetime, str] | None = None
    for path in sorted(event_dir.glob("*/logs/log/scheduler/*.log")):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        stamp: datetime | None = None
        for line in lines:
            if match := LOG_TIME.match(line):
                stamp = parse_time(match.group(1))
            found = NEW_CMT_BROADCAST.search(line)
            if found and found.group(2) == event_id and stamp is not None and (best is None or stamp < best[0]):
                best = (stamp, found.group(1))
    return best


def _event_jobs(db: sqlite3.Connection, event_id: str, event_dir: Path) -> None:
    cycle = event_cycle(db, event_id)
    if cycle is None:
        prefix = event_dir.name.split("-", 1)[0]
        cycle = prefix if prefix.isdigit() else None
    if cycle is None or not cycle_matches_event(db, event_id, cycle):
        return
    all_jobs = read_jobs(db)
    jobs = [job for job in all_jobs if job.cycle == cycle]
    if not jobs:
        return

    def put(resolution: str, metric: str, value: float | None, unit: str | None = None, detail: Any = None) -> None:
        if value is not None:
            set_metric(db, event_id, resolution, metric, value, unit, detail)

    by_task: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        by_task[job.name].append(job)
    for name, submissions in by_task.items():
        submissions.sort(key=lambda job: job.submit_num)
        last = submissions[-1]
        base, resolution = split_task(name)
        where = {"cycle": cycle, "task": name, "submit_num": last.submit_num, "state": last.state}
        put(resolution, f"ops.task.{base}.queue_s", last.queue_s, "s", where)
        put(resolution, f"ops.task.{base}.run_s", last.run_s, "s", where)
        put(resolution, f"ops.task.{base}.submits", len(submissions), "count", [job.state for job in submissions])
        fractions = [_ratio(job.run_s, job.time_limit_s) for job in submissions]
        known = [fraction for fraction in fractions if fraction is not None]
        if known:
            put(resolution, f"ops.task.{base}.time_limit_fraction", max(known), "ratio")

    hosts = _sw4_hosts(event_dir)
    first_submit = min((job.submitted for job in jobs if job.submitted), default=None)
    origin_row = db.execute("SELECT origin_time FROM events WHERE event_id = ?", (event_id,)).fetchone()
    origin = parse_time(origin_row["origin_time"]) if origin_row else None
    if origin and origin.tzinfo is None:
        origin = None
    picked = first_pickup(event_dir, event_id)
    if origin and picked and (first_submit is None or picked[0] < first_submit):
        put(
            "",
            "ops.cycle_start_delay_s",
            (picked[0] - origin).total_seconds(),
            "s",
            {"from": "scheduler log new_cmt broadcast", "cycle": picked[1], "at": picked[0].isoformat()},
        )
    elif origin and first_submit:
        put("", "ops.cycle_start_delay_s", (first_submit - origin).total_seconds(), "s")
    resolutions = sorted({job.resolution for job in jobs if job.resolution})
    for resolution in resolutions:
        ran = [job for job in by_task.get(f"sw4_{resolution}", []) if job.started is not None]
        if ran:
            _sw4_job_metrics(db, event_id, resolution, ran[-1], all_jobs, hosts)
        uploads = [
            job for job in by_task.get(f"upload_{resolution}", []) if job.state == "succeeded" and job.ended is not None
        ]
        if uploads:
            done = min(job.ended for job in uploads if job.ended is not None)
            if first_submit:
                put(
                    resolution,
                    "ops.time_to_results_s",
                    (done - first_submit).total_seconds(),
                    "s",
                    {"from": first_submit.isoformat(), "to": done.isoformat(), "reference": "first job submission"},
                )
            if origin:
                put(resolution, "ops.time_to_results_from_origin_s", (done - origin).total_seconds(), "s")

    total = sum(job.core_hours or 0.0 for job in jobs)
    sw4 = sum(job.core_hours or 0.0 for job in jobs if job.base == "sw4")
    put("", "ops.jobs", len(jobs), "count")
    failed = [job for job in jobs if job.state in ("failed", "submit-failed")]
    put("", "ops.jobs_failed", len(failed), "count", [f"{job.name}/{job.submit_num:02d} {job.state}" for job in failed])
    put("", "ops.extra_submits", sum(len(s) - 1 for s in by_task.values()), "count")
    put("", "ops.core_hours", total, "core-h")
    put("", "perf.job.sw4_core_hours_share", _ratio(sw4, total), "ratio", {"sw4_core_hours": sw4})
