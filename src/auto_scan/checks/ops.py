"""Checks on the workflow's operation: failed runs, recurring job failures, queueing,
retries, missing resolutions and late pick-up, from the ``runs`` and ``jobs`` tables.

================================ =============================================================================
check                            fails when
================================ =============================================================================
ops.run_failed                   a resolution's manifest status is not ``succeeded``, unless a person cancelled
                                 its last attempt (subject: resolution)
ops.task_failure_class           a job of this event failed with a known failure class other than ``cancelled``
                                 (subject: the class)
ops.queue_starvation             a job waited in the queue longer than its task's limit (subject: task)
ops.retries_high                 a task needed RETRIES_LIMIT or more submissions and one of its failures has no
                                 known class (subject: task)
ops.missing_resolution           an event lacks a resolution that recent events have, later events exist, and
                                 its SW4 jobs were not all cancelled by a person
ops.pickup_delay                 the cycle's first job came more than PICKUP_DELAY_S after the origin
================================ =============================================================================

Failure classes come from the last lines of ``job_err`` (the monitor keeps 20) through
``FAILURE_CLASSES``, a table of regular expressions, first match wins; anything else is
``other:<task>`` (the task without its resolution).
"""

import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from auto_scan.db import run_resolutions
from auto_scan.extract.perf import Job, event_cycle, read_jobs, split_task
from auto_scan.registry import Evaluation, Severity, check

# (class, pattern, suspect sources, severity): first match wins, so the specific ones come
# before "cancelled", which every killed job's tail also matches.
FAILURE_CLASSES: list[tuple[str, re.Pattern[str], list[str], Severity]] = [
    # runner.def is realtime_simulation_workflow's container/runner.def.
    (
        "apptainer-build",
        re.compile(r"while running %post section|FATAL:\s+While performing build"),
        ["realtime"],
        "high",
    ),
    ("time-limit", re.compile(r"DUE TO TIME LIMIT|failed/XCPU"), ["realtime"], "high"),
    ("out-of-memory", re.compile(r"oom[-_ ]kill|Out Of Memory|OUT_OF_MEMORY|MemoryError"), ["realtime"], "high"),
    ("disk-full", re.compile(r"No space left on device|Disk quota exceeded"), ["realtime"], "high"),
    ("cloudflare-1010", re.compile(r"[Ee]rror[ -]?1010"), ["realtime"], "medium"),
    ("no-stations-in-domain", re.compile(r"No stations in domain"), ["realtime", "workflow"], "high"),
    ("cost-model-cannot-fit", re.compile(r"Cannot fit \d+ cores even at the minimum reach"), ["realtime"], "high"),
    ("patch-failed", re.compile(r"has no SW4 developer command|/patches/\S+\.py"), ["realtime"], "high"),
    ("derive-realisation", re.compile(r"can't derive one from the other"), ["realtime"], "high"),
    ("generate-domain", re.compile(r"'generate-domain'.*returned non-zero"), ["workflow", "realtime"], "high"),
    ("simdb-ingest", re.compile(r"records must all be kind|publish_simdb: job \d+ failed"), ["realtime"], "medium"),
    ("ffmpeg", re.compile(r"ffmpeg failed"), ["shake-movie"], "low"),
    ("rclone-link", re.compile(r"'rclone', 'link'|rclone link \S+ failed"), ["realtime"], "low"),
    ("cleanup-refused", re.compile(r"Not cleaning "), ["realtime"], "low"),
    ("sw4-crash", re.compile(r"MPI_ABORT|Segmentation fault|signal 11|SIGSEGV"), ["sw4"], "high"),
    # A person (or Cylc on their command) cancelled or killed the job; SIGTERM short of the
    # time limit (_at_time_limit is tested first) is a cancellation whose Slurm line has
    # scrolled out of the tail.
    ("cancelled", re.compile(r"CANCELLED AT|\[jobs-kill out\]|failed/TERM"), ["realtime"], "low"),
]
CLASS_INFO = {name: (suspects, severity) for name, _, suspects, severity in FAILURE_CLASSES}
# Failure classes that are a person's decision, not a fault: a cancelled job is reported
# as evidence but never fails a check on its own (a person cancels jobs to free the queue,
# rerun a cycle or stop a run the workflow should not have started).
DELIBERATE_CLASSES = {"cancelled"}
# A job that ran to within 2% of its limit and ended on TERM was killed by the limit even
# when Slurm's message is cut off.
TIME_LIMIT_SLACK = 0.98

# README: 0.5 Hz results are meant to arrive within about an hour, so an hour in the queue
# already misses it; 1 Hz runs 24 h jobs, and the README calls its ~10 h waits on
# 2026-11-03 the problem the smaller jobs solve: 6 h.
QUEUE_LIMIT_S = 3600.0
QUEUE_LIMITS_S = {"sw4_1hz": 6 * 3600.0}
# flow.cylc's root `execution retry delays = PT5M` gives each task two tries; a third
# submission means a person retriggered it.
RETRIES_LIMIT = 3
# A resolution is expected when at least 2 of the 5 previous events have it.
EXPECTED_WINDOW = 5
EXPECTED_MIN = 2
# CMT_WAIT_HOURS (6) in flow.cylc: an earthquake waits at most that for its CMT before the
# cycle starts; an hour more for SeismicNow's poll and the first job's queue.
PICKUP_DELAY_S = 7 * 3600.0


def classify_failure(job: Job) -> str:
    """A failed job's class, from its ``job_err`` tail.

    Parameters
    ----------
    job : Job
        A failed or submit-failed job.

    Returns
    -------
    str
        A class name from ``FAILURE_CLASSES``, ``time-limit`` for a job killed at its
        limit, or ``other:<task>``.
    """
    text = job.job_err
    for name, pattern, _, _ in FAILURE_CLASSES:
        if name == "cancelled" and _at_time_limit(job):
            return "time-limit"
        if pattern.search(text):
            return name
    if _at_time_limit(job):
        return "time-limit"
    return f"other:{job.base}"


def _at_time_limit(job: Job) -> bool:
    return bool(
        job.run_s and job.time_limit_s and job.run_s >= TIME_LIMIT_SLACK * job.time_limit_s and "TERM" in job.job_err
    )


def _failed(jobs: list[Job]) -> list[Job]:
    return [job for job in jobs if job.state in ("failed", "submit-failed")]


def _cycle_key(cycle: str) -> tuple[int, str]:
    return (int(cycle), "") if cycle.isdigit() else (1 << 30, cycle)


def _tail(text: str, lines: int = 8) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "cycle": job.cycle,
        "task": job.name,
        "submit_num": job.submit_num,
        "state": job.state,
        "submitted": job.submitted.isoformat() if job.submitted else None,
        "started": job.started.isoformat() if job.started else None,
        "run_s": job.run_s,
        "queue_s": job.queue_s,
        "job_err_tail": _tail(job.job_err),
    }


def _evaluation(
    check_id: str,
    event_id: str,
    subject: str,
    failing: bool,
    title: str,
    summary: str,
    value: float | None,
    threshold: float | None,
    evidence: dict[str, Any],
    suspects: list[str],
    severity: Severity = "medium",
    resolution: str = "",
) -> Evaluation:
    return Evaluation(
        check_id=check_id,
        event_id=event_id,
        failing=failing,
        title=title,
        summary=summary,
        kind="ops",
        severity=severity,
        resolution=resolution,
        subject=subject,
        value=value,
        threshold=threshold,
        evidence=evidence,
        suspect_sources=suspects,
    )


@check
def run_failed(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A resolution's run did not succeed.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a run with a status.
    """
    cycle = event_cycle(db, event_id)
    jobs = read_jobs(db, cycle) if cycle is not None else []
    for row in db.execute("SELECT resolution, status FROM runs WHERE event_id = ? AND status IS NOT NULL", (event_id,)):
        resolution, status = row["resolution"], row["status"]
        task = status.partition(":")[2] if ":" in status else ""
        attempts = [job for job in jobs if job.name == task]
        last = attempts[-1] if attempts else None
        failure = classify_failure(last) if last is not None and last.state != "succeeded" else None
        failing = status != "succeeded" and failure not in DELIBERATE_CLASSES
        suspects, severity = CLASS_INFO.get(failure or "", (["realtime"], "high"))
        yield _evaluation(
            "ops.run_failed",
            event_id,
            resolution,
            failing,
            "Realtime runs fail before finishing",
            f"The {resolution} run's status is {status}" + (f" ({failure})." if failure else "."),
            None,
            None,
            {
                "status": status,
                "task": task,
                "failure_class": failure,
                "attempts": [_job_summary(job) for job in attempts],
            },
            suspects if failing else ["realtime"],
            "high" if failing and severity != "low" else severity,
            resolution,
        )


@check
def task_failure_class(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A known kind of job failure happened on this event.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a failure class seen on this event or an earlier one.
    """
    cycle = event_cycle(db, event_id)
    if cycle is None:
        return
    all_jobs = read_jobs(db)
    if not all_jobs:
        return
    seen: dict[str, set[str]] = defaultdict(set)
    here: dict[str, list[Job]] = defaultdict(list)
    for job in _failed(all_jobs):
        if _cycle_key(job.cycle) > _cycle_key(cycle):
            continue
        name = classify_failure(job)
        seen[name].add(job.cycle)
        if job.cycle == cycle:
            here[name].append(job)
    cycle_jobs = [job for job in all_jobs if job.cycle == cycle]
    for name in sorted(seen):
        failures = here.get(name, [])
        suspects, severity = CLASS_INFO.get(name, (["realtime"], "medium"))
        recovered = all(
            any(
                other.name == job.name and other.submit_num > job.submit_num and other.state == "succeeded"
                for other in cycle_jobs
            )
            for job in failures
        )
        cycles = sorted(seen[name], key=_cycle_key)
        yield _evaluation(
            "ops.task_failure_class",
            event_id,
            name,
            bool(failures) and name not in DELIBERATE_CLASSES,
            f"Realtime jobs fail: {name}",
            (
                f"{len(failures)} job(s) failed with {name} on this cycle"
                f" ({', '.join(sorted({job.name for job in failures}))}); cycles with it so far: {', '.join(cycles)}."
                + (" Each was retried successfully." if recovered else "")
            )
            if failures
            else f"No {name} failures on this cycle; cycles with it so far: {', '.join(cycles)}.",
            float(len(failures)),
            0.0,
            {
                "class": name,
                "cycles_with_class": cycles,
                "recovered_by_retry": recovered if failures else None,
                "failures": [_job_summary(job) for job in failures[:6]],
                "pattern": next((pattern.pattern for n, pattern, _, _ in FAILURE_CLASSES if n == name), None),
            },
            suspects,
            "low" if failures and recovered and severity != "high" else severity,
        )


@check
def queue_starvation(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A job waited in the queue far longer than its task can afford.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a task of the event with a known queue wait.
    """
    cycle = event_cycle(db, event_id)
    if cycle is None:
        return
    by_task: dict[str, list[Job]] = defaultdict(list)
    for job in read_jobs(db, cycle):
        by_task[job.name].append(job)
    for name, jobs in sorted(by_task.items()):
        waits = [(job.queue_s, job) for job in jobs if job.queue_s is not None]
        if not waits:
            continue
        wait, worst = max(waits, key=lambda item: item[0])
        limit = QUEUE_LIMITS_S.get(name, QUEUE_LIMIT_S)
        never_ran = worst.started is None
        yield _evaluation(
            "ops.queue_starvation",
            event_id,
            name,
            wait > limit,
            f"Jobs wait too long in the Slurm queue: {name}",
            f"{name} waited {wait / 3600:.1f} h in the queue (limit {limit / 3600:.1f} h)"
            + (f" and never started ({worst.state})." if never_ran else "."),
            wait,
            limit,
            {
                "submissions": [_job_summary(job) | {"cores": job.cores} for job in jobs],
                "note": "queue_s is computed from time_submit and time_run, or the [jobs-kill out] time for a job killed"
                " in the queue; the monitor's queue_s for such a job runs on to its latest upload",
                "code": ["realtime_simulation_workflow flow.cylc: the task's [[[directives]]] and RESOLUTION_SETTINGS"],
            },
            ["realtime"],
            "high" if never_ran and wait > limit else "medium",
        )


@check
def retries_high(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """A task needed more submissions than its automatic retry gives.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a task of the event.
    """
    cycle = event_cycle(db, event_id)
    if cycle is None:
        return
    by_task: dict[str, list[Job]] = defaultdict(list)
    for job in read_jobs(db, cycle):
        by_task[job.name].append(job)
    for name, jobs in sorted(by_task.items()):
        count = len(jobs)
        classes = sorted({classify_failure(job) for job in _failed(jobs)})
        # A known failure class is reported (and filed) by ops.task_failure_class, and a
        # task with no failure was rerun by a person on purpose: only an unclassified
        # failure that needed a person is this check's own finding.
        unexplained = any(name.startswith("other:") for name in classes)
        yield _evaluation(
            "ops.retries_high",
            event_id,
            name,
            count >= RETRIES_LIMIT and unexplained,
            f"Realtime tasks need manual retriggers: {name}",
            f"{name} was submitted {count} times ({', '.join(job.state for job in jobs)})"
            + (f"; failures: {', '.join(classes)}." if classes else "."),
            float(count),
            float(RETRIES_LIMIT),
            {"submissions": [_job_summary(job) for job in jobs], "failure_classes": classes},
            ["realtime"],
            "low",
        )


@check
def missing_resolution(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """An event lacks a resolution recent events have, though later events have run.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One an expected resolution.
    """
    row = db.execute("SELECT origin_time FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if row is None or row["origin_time"] is None:
        return
    origin = row["origin_time"]
    earlier = [
        r["event_id"]
        for r in db.execute(
            "SELECT event_id FROM events WHERE origin_time < ? AND event_id IN (SELECT event_id FROM runs)"
            " ORDER BY origin_time DESC LIMIT ?",
            (origin, EXPECTED_WINDOW),
        )
    ]
    counts: dict[str, int] = defaultdict(int)
    for other in earlier:
        for resolution in run_resolutions(db, other):
            counts[resolution] += 1
    expected = sorted(res for res, n in counts.items() if n >= EXPECTED_MIN)
    have = set(run_resolutions(db, event_id))
    cycle = event_cycle(db, event_id)
    jobs = read_jobs(db, cycle) if cycle is not None else []
    for resolution in expected:
        later = db.execute(
            "SELECT count(*) AS n FROM events JOIN runs USING (event_id) WHERE origin_time > ? AND resolution = ?",
            (origin, resolution),
        ).fetchone()["n"]
        missing = resolution not in have
        own = [job for job in jobs if split_task(job.name)[1] == resolution]
        tasks = [_job_summary(job) for job in own]
        # Its SW4 jobs all cancelled by a person: left out on purpose.
        sw4 = [job for job in own if job.base == "sw4"]
        cancelled = bool(sw4) and all(
            job.state in ("failed", "submit-failed") and classify_failure(job) in DELIBERATE_CLASSES for job in sw4
        )
        yield _evaluation(
            "ops.missing_resolution",
            event_id,
            resolution,
            missing and later > 0 and not cancelled,
            "Events are missing a resolution's results",
            f"No {resolution} run was uploaded for this event, which {counts[resolution]} of the previous"
            f" {len(earlier)} events have, and {later} later events have one."
            if missing
            else f"The {resolution} run is present.",
            None,
            None,
            {
                "expected_from": earlier,
                "later_events_with_it": later,
                "jobs": [job | {"job_err_tail": job["job_err_tail"][-400:]} for job in tasks][-12:],
            },
            ["realtime"],
            "high",
            resolution,
        )


@check
def pickup_delay(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """The workflow started the event's cycle long after the earthquake.

    Parameters
    ----------
    db : sqlite3.Connection
        The scan database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event, when its origin and jobs are known.
    """
    delay = db.execute(
        "SELECT value FROM metrics WHERE event_id = ? AND resolution = '' AND metric = 'ops.cycle_start_delay_s'",
        (event_id,),
    ).fetchone()
    cycle = event_cycle(db, event_id)
    if delay is None or delay["value"] is None or cycle is None:
        return
    value = float(delay["value"])
    all_jobs = read_jobs(db)
    own = [job for job in all_jobs if job.cycle == cycle and job.submitted]
    start = min(job.submitted for job in own if job.submitted) if own else None
    # The last job of an earlier cycle to finish (or be submitted) before this cycle started.
    before = [
        (moment, job)
        for job in all_jobs
        if _cycle_key(job.cycle) < _cycle_key(cycle)
        and (moment := job.ended or job.submitted) is not None
        and start is not None
        and moment <= start
    ]
    last_before = max(before, key=lambda item: item[0])[1] if before else None
    yield _evaluation(
        "ops.pickup_delay",
        event_id,
        "",
        value > PICKUP_DELAY_S,
        "Earthquakes are picked up late",
        f"The cycle's first job was submitted {value / 3600:.1f} h after the earthquake"
        f" (limit {PICKUP_DELAY_S / 3600:.0f} h: CMT_WAIT_HOURS plus an hour).",
        value,
        PICKUP_DELAY_S,
        {
            "cycle": cycle,
            "first_submission": start.isoformat() if start else None,
            "previous_job_before_it": _job_summary(last_before) if last_before else None,
            "note": "a long gap with no jobs of other cycles before it suggests the scheduler was down or the"
            " new_cmt xtrigger held; other cycles' jobs running up to it suggest MAX_CONCURRENT_EVENTS",
            "code": [
                "realtime_simulation_workflow lib/python/new_cmt.py",
                "realtime_simulation_workflow flow.cylc: CMT_WAIT_HOURS, MAX_CONCURRENT_EVENTS",
                "realtime_simulation_workflow etc/deploy.sh",
            ],
        },
        ["realtime"],
        "medium",
    )
