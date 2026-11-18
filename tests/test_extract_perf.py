"""Tests for auto_scan.extract.perf, on small synthetic runs."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from auto_scan.db import connect, get_metric
from auto_scan.extract import perf

EVENT = "2026p000001"
CYCLE = "7"
RES = "0_5hz"

TIMER_HEADER = (
    "region,depth,calls_max,calls_total,ranks,min_s,avg_s,max_s,wall_avg_s,mpi_min_s,mpi_avg_s,mpi_max_s,"
    "mpi_bytes_avg,mpi_msgs_avg,mpi_calls_avg,omp_parallel_avg_s,omp_regions_avg,omp_threads_max,omp_wait_frac,"
    "omp_imb_avg,omp_imb_max"
)
TS = "solve/time_stepping"
# region: (calls a rank, min, avg, max, mpi_min, mpi_avg, mpi_max, mpi_calls_avg)
TIMERS = {
    "parse_input": (1, 5, 5, 5, 0, 0, 0, 0),
    "setupRun": (1, 2, 2, 2, 0, 0, 0, 0),
    "solve": (1, 100, 100, 100, 20, 50, 60, 0),
    TS: (1, 90, 90, 90, 20, 45, 55, 0),
    f"{TS}/predictor": (120, 40, 40, 40, 0, 0, 0, 0),
    f"{TS}/predictor/evalRHS": (120, 10, 20, 40, 0, 0, 0, 0),
    f"{TS}/predictor/evalRHS/curvilinear": (120, 8, 15, 30, 0, 0, 0, 0),
    f"{TS}/predictor/evalRHS/cartesian": (120, 2, 5, 10, 0, 0, 0, 0),
    f"{TS}/predictor/enforceIC": (120, 9, 9, 9, 0, 0, 0, 0),
    f"{TS}/predictor/enforceIC/consintp/jacobi": (1500, 1, 1, 1, 0, 0, 0, 0),
    f"{TS}/corrector": (120, 40, 40, 40, 0, 0, 0, 0),
    f"{TS}/corrector/evalRHS": (120, 10, 20, 40, 0, 0, 0, 0),
    f"{TS}/corrector/enforceIC": (120, 9, 9, 9, 0, 0, 0, 0),
    f"{TS}/corrector/enforceIC/consintp/jacobi": (1500, 1, 1, 1, 0, 0, 0, 0),
    f"{TS}/output": (100, 3, 3, 3, 0, 0, 0, 0),
    "write_timeseries": (1, 4, 4, 4, 0, 0, 0, 0),
    "MPI_Barrier": (0, 0, 0, 0, 10, 30, 40, 240),
    "MPI_Allreduce": (0, 0, 0, 0, 5, 10, 12, 3600),
}
RANKS = 4


def timers_csv() -> str:
    """A small sw4_timers.csv."""
    lines = [TIMER_HEADER]
    for region, (calls, low, mean, high, mpi_low, mpi_mean, mpi_high, mpi_calls) in TIMERS.items():
        depth = -1 if region.startswith("MPI_") else region.count("/")
        lines.append(
            f"{region},{depth},{calls},{calls * RANKS},{RANKS},{low},{mean},{high},110,{mpi_low},{mpi_mean},"
            f"{mpi_high},0,0,{mpi_calls},0,0,4,0,0,0"
        )
    return "\n".join(lines) + "\n"


SW4_LOG = """\
  [host] OMP_PLACES = '{0:2},{2:2},{4:2},{6:2}'
Global grid sizes (without ghost points)
Grid         h        Nx        Ny        Nz       Points      Type
   0       400       10       10        10      1000     Cartesian
   1       200       20       20         3      1200     Curvilinear
Total number of grid points (without ghost points): 2.2e3
  time step = 2.5e-02
Time stepping starts at t=-2.5 s with 20 pre-roll steps;
Supergrid receiver check: 1 of 50 receivers are inside the supergrid absorbing layer.
          Per-rank spread over 4 ranks
Phase        min        max        max@rank   max/mean
Total        1.0e+02  1.1e+02  0          1.01e+00
Div-stress   1.0e+01  4.0e+01  3          2.0e+00
----------------------------------------
program sw4 finished!
"""

SACCT = """\
JobID|JobName|State|ExitCode|NTasks|AllocCPUS|Elapsed|MaxRSS|MaxRSSTask|MaxRSSNode|AveRSS
9|sw4|COMPLETED|0:0||16|00:02:00||||
9.batch|batch|COMPLETED|0:0|1|8|00:02:00|600M|0|n01|600M
9.0|numa_bind.sh|COMPLETED|0:0|4|16|00:01:58|2048.00M|2|n02|1024.00M
"""

SIZE_DOMAIN = """\
reach 100 km: 200 x 300 km, 60 s, 1.0e+07 points, 1,000 core-hours: does not fit 16 cores (20 core-hours)
reach 50 km: 100 x 100 km, 30 s, 2.0e+06 points, 40 core-hours: does not fit 16 cores (20 core-hours)
reach 30 km: 60 x 60 km, 20 s, 5.0e+05 points, 10 core-hours: fits 16 cores (20 core-hours)
"""


def rank_file(rank: int, host: str, cpus: str, nodebind: str, rss_kb: int | None) -> str:
    text = f"rank {rank} on {host}\ncpus {cpus}\npolicy: default\nnodebind: {nodebind}\nmembind: 0 1\nthp never\n"
    if rss_kb is not None:
        text += f"max_rss_kb {rss_kb}\nelapsed_s 10\n"
    return text


def write_run(run_dir: Path) -> None:
    """A run directory in the cache layout, with every file the extractor reads."""
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"status": "succeeded", "cycle_point": CYCLE}))
    (run_dir / "sw4_run.json").write_text(
        json.dumps(
            {
                "points": 1.0e6,
                "steps": 100,
                "cores": 16,
                "ranks": 4,
                "threads_per_rank": 4,
                "nodes": 2,
                "completed": True,
                "timestep_wall_s": 90.0,
                "updates_per_core_s": 1.0e6 * 100 / (16 * 90.0),
                "cfl": 1.3,
                "sw4_commit": "abc",
            }
        )
    )
    (run_dir / "sw4_estimate.json").write_text(
        json.dumps(
            {
                "gridpoints": 1.2e6,
                "padded_gridpoints": 1.0e6,
                "time_steps": 110,
                "cfl": 1.3,
                "duration_s": 10.0,
                "area_km2": 3600.0,
                "extent_x_km": 60.0,
                "extent_y_km": 60.0,
                "core_hours": 10.0,
                "reach_km": 30.0,
                "nodes": 1,
                "nodes_up_to": 4,
                "budget_core_hours": 20.0,
                "point_steps_per_core_second": 2.0e5,
                "multinode_efficiency": 0.8,
                "cost_model": {
                    "points_factor": {"value": 1.1, "spread": 0.1, "runs": 0},
                    "steps_per_second": {"value": 10.0, "spread": 0.1, "runs": 3},
                    "speed": {"value": 5.0e5, "spread": 0.9, "runs": 1.5},
                    "multinode_efficiency": {"value": 0.9, "spread": 0.1, "runs": 0},
                    "z": 1.0,
                    "runs": 5,
                },
                "full_domain": {"area_km2": 60000.0, "core_hours": 1000.0, "reach_km": 100.0, "fits": False},
            }
        )
    )
    jobs = run_dir / "logs" / "log" / "job" / CYCLE
    sw4 = jobs / f"sw4_{RES}" / "01"
    (sw4 / "ranks").mkdir(parents=True)
    (sw4 / "sw4_timers.csv").write_text(timers_csv())
    (sw4 / "sw4.log").write_text(SW4_LOG)
    (sw4 / "sw4_hosts").write_text("n01\nn01\nn02\nn03\n")
    (sw4 / "job").write_text("#SBATCH --cpus-per-task=4\n#SBATCH --mem-per-cpu=1024M\n")
    ranks = [
        rank_file(0, "n01", "0-7", "0", 1024**2),
        rank_file(1, "n01", "8-15", "0 1", None),
        rank_file(2, "n02", "0-3", "0", None),  # half the CPUs its 4 threads need
        rank_file(3, "n03", "0-7", "0", 2 * 1024**2),
    ]
    for rank, text in enumerate(ranks):
        (sw4 / "ranks" / f"rank-{rank}.txt").write_text(text)
    record = jobs / f"record_sw4_{RES}" / "01"
    record.mkdir(parents=True)
    (record / "sacct.txt").write_text(SACCT)
    size = jobs / f"size_domain_{RES}" / "01"
    size.mkdir(parents=True)
    (size / "job.out").write_text(SIZE_DOMAIN)


@pytest.fixture
def scan() -> Iterator[sqlite3.Connection]:
    """An in-memory scan database with one event at cycle 7."""
    db = connect(":memory:")
    db.execute(
        "INSERT INTO events (event_id, cycle, event_dir, origin_time) VALUES (?, ?, ?, ?)",
        (EVENT, int(CYCLE), f"{CYCLE}-{EVENT}", "2026-11-01T00:00:00+00:00"),
    )
    yield db
    db.close()


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A synthetic run's cache directory."""
    path = tmp_path / f"{CYCLE}-{EVENT}" / RES
    write_run(path)
    return path


def metric(db: sqlite3.Connection, name: str, resolution: str = RES) -> float | None:
    return get_metric(db, EVENT, resolution, name)


def test_cost_metrics(scan: sqlite3.Connection, run_dir: Path) -> None:
    perf.extract_run(scan, EVENT, RES, run_dir)
    actual = 90.0 * 16 / 3600
    assert metric(scan, "perf.actual_core_hours") == pytest.approx(actual)
    assert metric(scan, "perf.estimated_core_hours_effective") == pytest.approx(10.0 / 0.8)
    assert metric(scan, "perf.cost_ratio") == pytest.approx(12.5 / actual)
    assert metric(scan, "perf.points_ratio") == pytest.approx(1.2)
    assert metric(scan, "perf.steps_ratio") == pytest.approx(1.1)
    assert metric(scan, "perf.domain_fraction") == pytest.approx(3600 / 60000)
    assert metric(scan, "perf.domain_fits") == 0.0
    # central: 1.1 x 1e6 points x 10 steps/s x 10 s x 0.9 / 1.3 / 5e5 / 0.9 efficiency, in hours
    central = 1.1e6 * 10 * 10 * 0.9 / 1.3 / 5.0e5 / 0.9 / 3600
    assert metric(scan, "perf.central_core_hours") == pytest.approx(central)
    assert metric(scan, "perf.model.speed_spread") == pytest.approx(0.9)
    # At the measured cost (ratio 31.25) the 100 x 100 km domain (40 / 0.8 / 31.25 = 1.6 core-hours)
    # fits the 20 core-hour budget, and the full one (1000 / 0.8 / 31.25 = 40) doesn't.
    assert metric(scan, "perf.domain_fraction_measured") == pytest.approx(10000 / 60000)


def test_no_cost_ratio_for_a_run_that_stopped_part_way(scan: sqlite3.Connection, run_dir: Path) -> None:
    record = json.loads((run_dir / "sw4_run.json").read_text())
    (run_dir / "sw4_run.json").write_text(json.dumps({**record, "completed": False}))
    perf.extract_run(scan, EVENT, RES, run_dir)
    # its speed and the estimate are still recorded; the totals aren't compared
    assert metric(scan, "perf.updates_per_core_s") is not None
    assert metric(scan, "perf.estimated_core_hours_effective") == pytest.approx(10.0 / 0.8)
    for name in ("perf.cost_ratio", "perf.central_cost_ratio", "perf.steps_ratio", "perf.domain_fraction_measured"):
        assert metric(scan, name) is None, name


def test_timer_metrics(scan: sqlite3.Connection, run_dir: Path) -> None:
    perf.extract_run(scan, EVENT, RES, run_dir)
    rows = scan.execute("SELECT count(*) FROM sw4_timers WHERE event_id = ?", (EVENT,)).fetchone()[0]
    assert rows == len(TIMERS)
    assert metric(scan, "perf.mpi_fraction") == pytest.approx(0.5)
    assert metric(scan, "perf.mpi_max_min") == pytest.approx(3.0)
    assert metric(scan, "perf.mpi_share.barrier") == pytest.approx(0.3)
    assert metric(scan, "perf.timers.steps") == 120
    assert metric(scan, "perf.allreduce_per_step") == pytest.approx(30.0)
    assert metric(scan, "perf.rank_imbalance") == pytest.approx(2.0)
    assert metric(scan, "perf.imbalance.curvilinear.max_min") == pytest.approx(30 / 8)
    assert metric(scan, "perf.share.enforce_ic") == pytest.approx(18 / 90)
    assert metric(scan, "perf.interface_jacobi_per_step") == pytest.approx(3000 / 120)
    assert metric(scan, "perf.io_share") == pytest.approx((3 + 4 + 5 + 2) / 110)
    assert metric(scan, "perf.steps_uncounted_fraction") == pytest.approx(20 / 120)
    assert metric(scan, "perf.omp.wait_frac") is None


def test_placement_and_memory(scan: sqlite3.Connection, run_dir: Path) -> None:
    perf.extract_run(scan, EVENT, RES, run_dir)
    assert metric(scan, "perf.placement.hosts") == 3
    assert metric(scan, "perf.placement.max_ranks_per_host") == 2
    assert metric(scan, "perf.placement.expected_cpu_width") == 8
    assert metric(scan, "perf.placement.narrow_ranks") == 1
    assert metric(scan, "perf.placement.overlapping_ranks") == 0
    assert metric(scan, "perf.placement.multi_numa_ranks") == 1
    assert metric(scan, "perf.slowest_rank") == 3
    assert metric(scan, "perf.memory.recorded_fraction") == pytest.approx(0.5)
    assert metric(scan, "perf.memory.rank_peak_max_gb") == pytest.approx(2.0)
    assert metric(scan, "perf.memory.sacct_max_rss_gb") == pytest.approx(2.0)
    assert metric(scan, "perf.memory.request_per_rank_gb") == pytest.approx(4.0)
    assert metric(scan, "perf.memory.peak_fraction") == pytest.approx(0.5)
    assert metric(scan, "perf.sw4.preroll_steps") == 20
    assert metric(scan, "perf.sw4.grids") == 2
    assert metric(scan, "perf.sw4.curvilinear_points_fraction") == pytest.approx(1200 / 2200)
    assert metric(scan, "perf.sw4.receivers") == 50


def test_overlapping_ranks(scan: sqlite3.Connection, run_dir: Path) -> None:
    ranks = next(run_dir.glob("logs/log/job/*/sw4_*/01/ranks"))
    (ranks / "rank-1.txt").write_text(rank_file(1, "n01", "4-11", "0", None))
    perf.extract_run(scan, EVENT, RES, run_dir)
    assert metric(scan, "perf.placement.overlapping_ranks") == 2


def test_idempotent(scan: sqlite3.Connection, run_dir: Path) -> None:
    perf.extract_run(scan, EVENT, RES, run_dir)
    first = scan.execute("SELECT count(*) FROM metrics").fetchone()[0]
    perf.extract_run(scan, EVENT, RES, run_dir)
    assert scan.execute("SELECT count(*) FROM metrics").fetchone()[0] == first
    assert scan.execute("SELECT count(*) FROM sw4_timers").fetchone()[0] == len(TIMERS)


def test_missing_and_malformed_files(scan: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    perf.extract_run(scan, EVENT, RES, empty)
    assert scan.execute("SELECT count(*) FROM metrics").fetchone()[0] == 0
    (empty / "sw4_run.json").write_text("{not json")
    perf.extract_run(scan, EVENT, RES, empty)
    assert scan.execute("SELECT count(*) FROM metrics").fetchone()[0] == 0
    assert "sw4_run.json" in capsys.readouterr().err


def test_job_log_dir_takes_newest_submission_with_the_file(tmp_path: Path) -> None:
    base = tmp_path / "logs" / "log" / "job" / "3" / "sw4_1hz"
    for number, has in (("01", True), ("02", True), ("03", False)):
        (base / number).mkdir(parents=True)
        if has:
            (base / number / "sw4.log").write_text("")
    (base / "NN").symlink_to(base / "03")
    found = perf.job_log_dir(tmp_path, "3", "sw4_1hz", "sw4.log")
    assert found is not None and found.name == "02"
    assert perf.job_log_dir(tmp_path, "4", "sw4_1hz", "sw4.log") is None


def test_small_parsers() -> None:
    assert perf.parse_cpu_list("98,99,108-113") == {98, 99, *range(108, 114)}
    assert perf.parse_cpu_list("x-y") == set()
    assert perf.split_task("sw4_0_5hz") == ("sw4", "0_5hz")
    assert perf.split_task("generate_station_coordinates_1hz") == ("generate_station_coordinates", "1hz")
    assert perf.split_task("build_container") == ("build_container", "")


def job_row(**values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "workflow": "realtime/run1",
        "cycle": CYCLE,
        "submit_num": 1,
        "try_num": 1,
        "state": "succeeded",
        "time_submit": None,
        "time_run": None,
        "time_run_exit": None,
        "queue_s": None,
        "run_s": None,
        "cores": 1,
        "time_limit_min": 30,
        "core_hours": None,
        "job_err": None,
    }
    row.update(values)
    return row


def insert_jobs(db: sqlite3.Connection, rows: list[dict[str, object]]) -> None:
    for row in rows:
        columns = ", ".join(row)
        db.execute(f"INSERT INTO jobs ({columns}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def test_queue_wait() -> None:
    started = job_row(time_submit="2026-11-03T10:00:00+13:00", time_run="2026-11-03T10:30:00+13:00", queue_s=1)
    assert perf.queue_wait_s(started) == 1800
    killed = job_row(
        state="submit-failed",
        time_submit="2026-11-03T17:32:19+13:00",
        queue_s=1340680,
        job_err="[jobs-kill ret_code] 0\n[jobs-kill out] 2026-11-03T21:35:32+13:00|4/sw4_1hz/01|0\n",
    )
    assert perf.queue_wait_s(killed) == pytest.approx(4 * 3600 + 3 * 60 + 13)
    assert perf.queue_wait_s(job_row(state="submit-failed", time_submit="2026-11-03T17:32:19+13:00")) is None
    assert perf.queue_wait_s(job_row(state="submitted", time_submit="2026-11-03T17:32:19+13:00", queue_s=60)) == 60


def test_event_jobs(scan: sqlite3.Connection, run_dir: Path) -> None:
    perf.extract_run(scan, EVENT, RES, run_dir)
    other_hosts = run_dir.parent / "1hz" / "logs" / "log" / "job" / CYCLE / "sw4_1hz" / "01"
    other_hosts.mkdir(parents=True)
    (other_hosts / "sw4_hosts").write_text("n03\nn04\n")
    insert_jobs(
        scan,
        [
            job_row(
                name="resolve_sources",
                time_submit="2026-11-01T01:00:00+00:00",
                time_run="2026-11-01T01:00:10+00:00",
                time_run_exit="2026-11-01T01:00:20+00:00",
                core_hours=0.0,
            ),
            job_row(
                name=f"sw4_{RES}",
                time_submit="2026-11-01T01:10:00+00:00",
                time_run="2026-11-01T01:20:00+00:00",
                time_run_exit="2026-11-01T02:20:00+00:00",
                cores=4,
                time_limit_min=90,
                core_hours=4.0,
            ),
            job_row(
                name="sw4_1hz",
                time_submit="2026-11-01T01:10:00+00:00",
                time_run="2026-11-01T01:50:00+00:00",
                time_run_exit="2026-11-01T05:00:00+00:00",
                cores=32,
                time_limit_min=1440,
                core_hours=101.3,
            ),
            job_row(
                name=f"upload_{RES}",
                time_submit="2026-11-01T02:30:00+00:00",
                time_run="2026-11-01T02:30:10+00:00",
                time_run_exit="2026-11-01T03:00:00+00:00",
                core_hours=0.0,
            ),
            job_row(
                name=f"shake_movie_{RES}",
                state="failed",
                time_submit="2026-11-01T02:20:00+00:00",
                time_run="2026-11-01T02:20:10+00:00",
                time_run_exit="2026-11-01T02:21:00+00:00",
                job_err="RuntimeError: ffmpeg failed to encode x",
            ),
            job_row(
                name=f"shake_movie_{RES}",
                submit_num=2,
                time_submit="2026-11-01T02:22:00+00:00",
                time_run="2026-11-01T02:22:10+00:00",
                time_run_exit="2026-11-01T02:25:00+00:00",
            ),
        ],
    )
    perf.extract_event_jobs(scan, EVENT, run_dir.parent)
    # The monitor's cores (4, its --cpus-per-task) is less than sw4_run.json's 16: 16 is used.
    assert metric(scan, "perf.job.core_hours") == pytest.approx(16.0)
    assert metric(scan, "perf.job.cost_ratio") == pytest.approx(12.5 / 16.0)
    assert metric(scan, "perf.job.queue_s") == 600
    assert metric(scan, "perf.job.time_limit_fraction") == pytest.approx(60 / 90)
    # the 1 Hz job (32 cores) overlapped the last 30 of its 60 minutes, on 1 of its 3 hosts
    assert metric(scan, "perf.job.concurrent_sw4_cores") == pytest.approx(16.0)
    assert metric(scan, "perf.job.shared_nodes") == 1
    assert metric(scan, "perf.job.shared_node_fraction") == pytest.approx(0.5 / 3)
    assert metric(scan, "ops.time_to_results_s") == 2 * 3600
    assert metric(scan, "ops.time_to_results_from_origin_s") == 3 * 3600
    assert metric(scan, "ops.cycle_start_delay_s", "") == 3600
    assert metric(scan, "ops.task.shake_movie.submits") == 2
    assert metric(scan, "ops.jobs_failed", "") == 1
    assert metric(scan, "ops.extra_submits", "") == 1
    assert metric(scan, "perf.job.sw4_core_hours_share", "") == pytest.approx(105.3 / 105.3)
    # the per-run extractor keeps the event extractor's metrics, and re-running replaces them
    perf.extract_run(scan, EVENT, RES, run_dir)
    assert metric(scan, "perf.job.core_hours") == pytest.approx(16.0)
    perf.extract_event_jobs(scan, EVENT, run_dir.parent)
    assert metric(scan, "ops.jobs", "") == 6


def test_event_jobs_without_jobs(scan: sqlite3.Connection, run_dir: Path) -> None:
    perf.extract_event_jobs(scan, EVENT, run_dir.parent)
    assert scan.execute("SELECT count(*) FROM metrics").fetchone()[0] == 0


def test_sw4_input_metrics(scan: sqlite3.Connection, run_dir: Path) -> None:
    # 2026p866048's 1 Hz run: the attenuation command carried the developer command's
    # options, so SW4 warned, ignored them and fitted its defaults.
    (run_dir / "realisation.json").write_text(
        json.dumps({"sw4": {"commands": [{"name": "attenuation", "parameters": {"cfl": 1.3, "reporttiming": True}}]}})
    )
    log = next(run_dir.glob("logs/log/job/*/sw4_*/01/sw4.log"))
    log.write_text(
        log.read_text()
        + "\tWarning: ignoring attenuation line option 'cfl=1.3'\n"
        + "\tWarning: ignoring attenuation line option 'reporttiming=1'\n"
        + "*** Attenuation parameters calculated for 3 mechanisms,\n"
        + "      max freq=2.000000e+00 [Hz], min_freq=2.000000e-02 [Hz], velo_freq=1.000000e+00 [Hz]\n"
        + "TIME accuracy order=4 CFL=1.29999995e+00 prel. time step=1.31334411e-02\n"
    )
    perf.extract_run(scan, EVENT, RES, run_dir)
    assert metric(scan, "perf.sw4.ignored_options") == 2
    assert metric(scan, "perf.sw4.log_cfl") == pytest.approx(1.3)
    assert metric(scan, "perf.sw4.attenuation_mismatches") == 3


def test_sw4_input_metrics_match(scan: sqlite3.Connection, run_dir: Path) -> None:
    (run_dir / "realisation.json").write_text(
        json.dumps(
            {
                "sw4": {
                    "commands": [
                        {"name": "attenuation", "parameters": {"maxfreq": 0.5, "phasefreq": 0.5, "nmech": 2}},
                        {"name": "developer", "parameters": {"cfl": 1.3}},
                    ]
                }
            }
        )
    )
    log = next(run_dir.glob("logs/log/job/*/sw4_*/01/sw4.log"))
    log.write_text(
        log.read_text()
        + "*** Attenuation parameters calculated for 2 mechanisms,\n"
        + "      max freq=5.000000e-01 [Hz], min_freq=4.166667e-02 [Hz], velo_freq=5.000000e-01 [Hz]\n"
    )
    perf.extract_run(scan, EVENT, RES, run_dir)
    assert metric(scan, "perf.sw4.ignored_options") == 0
    assert metric(scan, "perf.sw4.attenuation_mismatches") == 0


def test_event_cycle_ignores_another_installs_cycle() -> None:
    """Cycle numbers restart with each install: an event directory 1-<event> from an earlier
    install must not take the current install's cycle 1 jobs."""
    event_cycle = perf.event_cycle

    db = connect(":memory:")
    for event, written in (("old", "2026-10-06T05:57:33+00:00"), ("new", "2026-10-09T03:06:07+00:00")):
        db.execute("INSERT INTO events (event_id, cycle, event_dir) VALUES (?, 1, ?)", (event, f"1-{event}"))
        db.execute(
            "INSERT INTO runs (event_id, resolution, status, written) VALUES (?, '0_5hz', 'succeeded', ?)",
            (event, written),
        )
    db.execute(
        "INSERT INTO jobs (workflow, cycle, name, submit_num, state, time_submit, time_run, time_run_exit)"
        " VALUES ('realtime/run1', '1', 'resolve_sources', 1, 'succeeded', '2026-10-09T03:14:35+13:00',"
        " '2026-10-09T03:14:50+13:00', '2026-10-09T03:15:20+13:00')"
    )
    assert event_cycle(db, "new") == "1"
    assert event_cycle(db, "old") is None


def test_first_pickup_reads_the_earliest_broadcast(tmp_path: Path) -> None:
    first_pickup = perf.first_pickup

    log = tmp_path / "1hz" / "logs" / "log" / "scheduler" / "09-restart-09.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "2026-11-18T00:03:01+13:00 INFO - Broadcast set:\n"
        "    + [11/resolve_sources] [environment]new_cmt_event_id=2026p866048\n"
        "2026-11-18T07:38:58+13:00 INFO - Broadcast cancelled:\n"
        "    - [10/resolve_sources] [environment]new_cmt_event_id=2026p862161\n"
        "2026-11-18T09:11:25+13:00 INFO - Broadcast set:\n"
        "    + [12/resolve_sources] [environment]new_cmt_event_id=2026p866048\n"
    )
    picked = first_pickup(tmp_path, "2026p866048")
    assert picked is not None and picked[1] == "11" and picked[0].isoformat() == "2026-11-18T00:03:01+13:00"
    assert first_pickup(tmp_path, "2026p862161") is None
