"""Tests for auto_scan.checks.ops, on a few synthetic ``jobs`` rows."""

import sqlite3
from collections.abc import Iterable, Iterator

import pytest

from auto_scan.checks import ops
from auto_scan.db import connect, set_metric
from auto_scan.extract.perf import Job, parse_time
from auto_scan.registry import Evaluation


@pytest.fixture
def scan() -> Iterator[sqlite3.Connection]:
    """Events ev1..ev4 at cycles 1..4, a day apart."""
    db = connect(":memory:")
    for cycle in range(1, 5):
        db.execute(
            "INSERT INTO events (event_id, cycle, event_dir, origin_time) VALUES (?, ?, ?, ?)",
            (f"ev{cycle}", cycle, f"{cycle}-ev{cycle}", f"2026-11-{cycle:02d}T00:00:00+00:00"),
        )
    yield db
    db.close()


def add_job(
    db: sqlite3.Connection,
    cycle: int,
    name: str,
    state: str = "succeeded",
    submit_num: int = 1,
    submit: str = "01:00:00",
    run: str | None = "01:00:10",
    end: str | None = "01:05:00",
    job_err: str | None = None,
    time_limit_min: float = 30,
) -> None:
    day = f"2026-11-{cycle:02d}T"
    db.execute(
        "INSERT INTO jobs (workflow, cycle, name, submit_num, state, time_submit, time_run, time_run_exit,"
        " cores, time_limit_min, job_err) VALUES ('realtime/run1', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            str(cycle),
            name,
            submit_num,
            state,
            f"{day}{submit}+00:00",
            f"{day}{run}+00:00" if run else None,
            f"{day}{end}+00:00" if end else None,
            time_limit_min,
            job_err,
        ),
    )


def by_subject(evaluations: Iterable[Evaluation]) -> dict[str, Evaluation]:
    return {evaluation.subject: evaluation for evaluation in evaluations}


def make_job(job_err: str, run_s: float | None = 100, limit_s: float | None = 5400) -> Job:
    return Job(
        cycle="1",
        name="sw4_0_5hz",
        base="sw4",
        resolution="0_5hz",
        submit_num=1,
        state="failed",
        submitted=None,
        started=None,
        ended=None,
        run_s=run_s,
        queue_s=None,
        cores=96,
        time_limit_s=limit_s,
        core_hours=None,
        job_err=job_err,
    )


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (
            "FATAL:   While performing build: while running engine: while running %post section: exit status 1",
            "apptainer-build",
        ),
        (
            'RuntimeError: POST datasets/realtime/events: HTTP 403 b\'{"title":"Error 1010: Access denied"',
            "cloudflare-1010",
        ),
        ("slurmstepd: error: *** JOB 1 ON n01 CANCELLED AT 2026-11-03T21:50:25 DUE TO TIME LIMIT ***", "time-limit"),
        ("slurmstepd: error: *** JOB 1 ON n01 CANCELLED AT 2026-11-03T21:50:25 ***", "cancelled"),
        ("ValueError: No stations in domain.", "no-stations-in-domain"),
        ("RuntimeError: ffmpeg failed to encode /tmp/x/segment002.mp4", "ffmpeg"),
        (
            "CalledProcessError: Command '['rclone', 'link', 'dropbox:/x.mp4']' returned non-zero exit status 1.",
            "rclone-link",
        ),
        ("Cannot fit 96 cores even at the minimum reach, 50 km", "cost-model-cannot-fit"),
        ('  "updates_per_core_s": 34335.7\n}\n2026-11-03T21:50:25+13:00 CRITICAL - failed/TERM', "cancelled"),
        ("KeyError: 'x'", "other:sw4"),
    ],
)
def test_classify_failure(tail: str, expected: str) -> None:
    assert ops.classify_failure(make_job(tail)) == expected


def test_classify_term_at_the_time_limit() -> None:
    job = make_job("2026-11-03T21:50:25+13:00 CRITICAL - failed/TERM", run_s=5390, limit_s=5400)
    assert ops.classify_failure(job) == "time-limit"


def test_run_failed(scan: sqlite3.Connection) -> None:
    scan.execute("INSERT INTO runs (event_id, resolution, status) VALUES ('ev2', '0_5hz', 'failed:size_domain_0_5hz')")
    scan.execute("INSERT INTO runs (event_id, resolution, status) VALUES ('ev2', '1hz', 'succeeded')")
    add_job(scan, 2, "size_domain_0_5hz", "failed", job_err="Cannot fit 96 cores even at the minimum reach, 50 km")
    found = by_subject(ops.run_failed(scan, "ev2"))
    assert found["0_5hz"].failing and found["0_5hz"].evidence["failure_class"] == "cost-model-cannot-fit"
    assert found["0_5hz"].severity == "high"
    assert not found["1hz"].failing


def test_task_failure_class(scan: sqlite3.Connection) -> None:
    add_job(scan, 1, "shake_movie_1hz", "failed", job_err="ffmpeg failed to encode a")
    add_job(scan, 1, "shake_movie_1hz", "succeeded", submit_num=2, submit="02:00:00", run="02:00:10", end="02:05:00")
    add_job(scan, 2, "publish_simdb_0_5hz", "failed", job_err="HTTP 403 Error 1010: Access denied")
    add_job(scan, 3, "build_container", "succeeded")
    first = by_subject(ops.task_failure_class(scan, "ev1"))
    assert set(first) == {"ffmpeg"}
    assert first["ffmpeg"].failing and first["ffmpeg"].evidence["recovered_by_retry"]
    assert first["ffmpeg"].suspect_sources == ["shake-movie"]
    second = by_subject(ops.task_failure_class(scan, "ev2"))
    assert second["cloudflare-1010"].failing and not second["cloudflare-1010"].evidence["recovered_by_retry"]
    # a class seen before but not on this event passes, so a fix can be verified
    assert not second["ffmpeg"].failing
    third = by_subject(ops.task_failure_class(scan, "ev3"))
    assert not any(evaluation.failing for evaluation in third.values())
    assert third["ffmpeg"].evidence["cycles_with_class"] == ["1"]


def test_queue_starvation(scan: sqlite3.Connection) -> None:
    add_job(scan, 1, "sw4_0_5hz", submit="01:00:00", run="03:00:00", end="03:20:00")
    add_job(scan, 1, "sw4_1hz", submit="01:00:00", run="03:00:00", end="09:00:00")
    add_job(
        scan,
        1,
        "hf_sim_1hz",
        "submit-failed",
        run=None,
        end=None,
        job_err="[jobs-kill out] 2026-11-01T05:00:00+00:00|1/hf_sim_1hz/01|0",
    )
    add_job(scan, 1, "im_calc_1hz", "submit-failed", run=None, end=None, job_err="nothing useful")
    found = by_subject(ops.queue_starvation(scan, "ev1"))
    assert found["sw4_0_5hz"].failing and found["sw4_0_5hz"].value == 7200
    assert not found["sw4_1hz"].failing  # 2 h is within the 1 Hz job's 6 h
    assert found["hf_sim_1hz"].failing and found["hf_sim_1hz"].severity == "high"
    assert "im_calc_1hz" not in found  # its wait is unknown


def test_retries_high(scan: sqlite3.Connection) -> None:
    for number in (1, 2):
        add_job(scan, 1, "build_container", "failed", submit_num=number, job_err="KeyError: 'x'")
    add_job(scan, 1, "build_container", "succeeded", submit_num=3)
    add_job(scan, 1, "make_realisation")
    found = by_subject(ops.retries_high(scan, "ev1"))
    assert found["build_container"].failing and "other:build_container" in found["build_container"].summary
    assert not found["make_realisation"].failing


def test_retries_high_leaves_known_classes_to_task_failure_class(scan: sqlite3.Connection) -> None:
    # Cycle 12's shake_movie_1hz: ffmpeg twice, then a manual retrigger after the fix.
    for number in (1, 2):
        add_job(
            scan, 1, "shake_movie_1hz", "failed", submit_num=number, job_err="RuntimeError: ffmpeg failed to encode a"
        )
    add_job(scan, 1, "shake_movie_1hz", "succeeded", submit_num=3)
    # A succeeded task a person reran twice.
    for number in (1, 2, 3):
        add_job(scan, 1, "upload_1hz", "succeeded", submit_num=number)
    found = by_subject(ops.retries_high(scan, "ev1"))
    assert not found["shake_movie_1hz"].failing and "ffmpeg" in found["shake_movie_1hz"].summary
    assert not found["upload_1hz"].failing


def test_cancelled_jobs_are_not_findings(scan: sqlite3.Connection) -> None:
    scan.execute("INSERT INTO runs (event_id, resolution, status) VALUES ('ev2', '1hz', 'failed:sw4_1hz')")
    add_job(
        scan, 2, "sw4_1hz", "failed", job_err="slurmstepd: error: *** JOB 1 ON n01 CANCELLED AT 2026-11-04T15:12:33 ***"
    )
    assert not by_subject(ops.run_failed(scan, "ev2"))["1hz"].failing
    cancelled = by_subject(ops.task_failure_class(scan, "ev2"))["cancelled"]
    assert not cancelled.failing and cancelled.evidence["failures"][0]["task"] == "sw4_1hz"


def test_missing_resolution_cancelled_on_purpose(scan: sqlite3.Connection) -> None:
    for event in ("ev1", "ev2", "ev4"):
        for resolution in ("0_5hz", "1hz"):
            scan.execute(
                "INSERT INTO runs (event_id, resolution, status) VALUES (?, ?, 'succeeded')", (event, resolution)
            )
    scan.execute("INSERT INTO runs (event_id, resolution, status) VALUES ('ev3', '0_5hz', 'succeeded')")
    add_job(
        scan,
        3,
        "sw4_1hz",
        "submit-failed",
        run=None,
        end=None,
        job_err="[jobs-kill out] 2026-11-03T21:35:32+00:00|3/sw4_1hz/01|0",
    )
    assert not by_subject(ops.missing_resolution(scan, "ev3"))["1hz"].failing


def test_missing_resolution(scan: sqlite3.Connection) -> None:
    for event in ("ev1", "ev2", "ev4"):
        for resolution in ("0_5hz", "1hz"):
            scan.execute(
                "INSERT INTO runs (event_id, resolution, status) VALUES (?, ?, 'succeeded')", (event, resolution)
            )
    scan.execute("INSERT INTO runs (event_id, resolution, status) VALUES ('ev3', '0_5hz', 'succeeded')")
    add_job(scan, 3, "sw4_1hz", "submit-failed", run=None, end=None)
    found = by_subject(ops.missing_resolution(scan, "ev3"))
    assert found["1hz"].failing and not found["0_5hz"].failing
    assert found["1hz"].evidence["jobs"][0]["task"] == "sw4_1hz"
    # the newest event: no later event has run yet, so nothing is missing for long
    assert not any(evaluation.failing for evaluation in ops.missing_resolution(scan, "ev4"))
    # too few earlier events to expect anything
    assert list(ops.missing_resolution(scan, "ev1")) == []


def test_pickup_delay(scan: sqlite3.Connection) -> None:
    add_job(scan, 1, "cleanup", "failed", submit="00:30:00", run="00:30:10", end="00:30:20", job_err="Not cleaning x")
    add_job(scan, 2, "resolve_sources", submit="10:00:00", run="10:00:10", end="10:00:20")
    set_metric(scan, "ev2", "", "ops.cycle_start_delay_s", 10 * 3600.0)
    evaluation = next(iter(ops.pickup_delay(scan, "ev2")))
    assert evaluation.failing
    assert evaluation.evidence["previous_job_before_it"]["task"] == "cleanup"
    set_metric(scan, "ev2", "", "ops.cycle_start_delay_s", 600.0)
    assert not next(iter(ops.pickup_delay(scan, "ev2"))).failing
    assert list(ops.pickup_delay(scan, "ev3")) == []


def test_pickup_delay_skips_a_backlog_event(scan: sqlite3.Connection) -> None:
    """An old earthquake run after a later one was not waiting to be picked up."""
    add_job(scan, 2, "resolve_sources", submit="10:00:00", run="10:00:10", end="10:00:20")
    set_metric(scan, "ev2", "", "ops.cycle_start_delay_s", 400 * 3600.0)
    assert next(iter(ops.pickup_delay(scan, "ev2"))).failing
    # ev1's run came first, but ev2's earthquake is the older one.
    for cycle in (1, 2):
        scan.execute(
            "INSERT INTO runs (event_id, resolution, written) VALUES (?, '1hz', ?)",
            (f"ev{cycle}", f"2026-11-{cycle:02d}T12:00:00+00:00"),
        )
    assert next(iter(ops.pickup_delay(scan, "ev2"))).failing
    scan.execute("UPDATE events SET origin_time = '2020-01-01T00:00:00+00:00' WHERE event_id = 'ev2'")
    assert list(ops.pickup_delay(scan, "ev2")) == []


def test_parse_time() -> None:
    assert parse_time("2026-11-18T09:15:48+13:00") is not None
    assert parse_time("garbage") is None
