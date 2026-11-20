"""A verdict on each posted CMT, and what the verdicts say taken together.

The checks in ``auto_scan.cmt.checks`` each follow one problem across events: one
finding, however many events show it. That suits a code bug, and hides the product:
which of the solutions SeismicNow posted can be believed. So, per event:

- ``cmt.verdict`` [<event id>]: the main solution graded ``good``, ``doubtful`` or
  ``reject`` from the product criteria below. A rejected solution that was posted is
  a finding of its own, named by its event, so one event's dismissal or issue never
  speaks for another's.

Criteria are the product checks themselves, so a verdict and the checks can't
disagree. A ``REJECT`` criterion failing rejects the solution: it rests on too few
stations, fits almost nothing, fits noise, or its magnitude is outside what GeoNet's
allows. A ``DOUBT`` criterion failing makes it doubtful: on the grid's edge, hanging
on one station, many stations misfit, the runs disagreeing, a large isotropic part.
``DOUBTS_TO_REJECT`` of those together also reject it.

And over the last ``WINDOW`` events (at least ``MIN_EVENTS`` graded), evaluated on
each event so a fix shows on later ones:

- ``cmt.population.rejected_share``: too many of the posted solutions are rejected.
- ``cmt.population.isotropic_selected``: too many of the posted full tensors are
  largely isotropic; the evidence says how often the deviatoric run was selected,
  and how often its VR could have beaten the same stations' full tensor.
- ``cmt.population.moment_inflation``: the poorly fitted solutions' Mw sits above
  GeoNet's magnitude and the well fitted ones' below, so a poor fit inflates Mw.

These compare with fixed references, never with the history: a history of poor
solutions must not make the next one look usual.
"""

import sqlite3
import statistics
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from auto_scan.cmt import checks
from auto_scan.db import SIMULATED_AT, get_metric
from auto_scan.registry import Check, Evaluation, cmt_check

# A failure of any of these rejects the posted solution.
REJECT: list[Check] = [
    checks.selected_few_stations,
    checks.low_variance_reduction,
    checks.no_signal,
    checks.magnitude_offset,
]
# A failure of any of these makes it doubtful.
DOUBT: list[Check] = [
    checks.selected_on_grid_edge,
    checks.jackknife_unstable,
    checks.negative_vr_stations,
    checks.runs_disagree,
    checks.selected_isotropic,
]
# Three independent doubts leave nothing to trust (2026p829637: VR 0.25, yet ISO +66 %,
# the posterior on the grid's edge, 75 % of stations misfit and an 84-degree jackknife).
DOUBTS_TO_REJECT = 3
# The population checks look at the last 20 events and need 5 of them graded.
WINDOW = 20
MIN_EVENTS = 5
# One posted solution in four rejected is no longer the odd poorly recorded event.
REJECTED_SHARE_MAX = 0.25
# One posted full tensor in five largely isotropic, for shear sources.
ISOTROPIC_SHARE_MAX = 0.2
# Well fitted: the two credible events here are at VR 0.55 and 0.64.
WELL_FITTED_VR = 0.4
# Median Mw - GeoNet M of the poorly fitted minus the well fitted. Half a unit is more
# than a factor of 5 in moment.
MOMENT_INFLATION_MAX = 0.5


@dataclass
class Verdict:
    """The grade of one event's posted solution.

    Attributes
    ----------
    event_id : str
        The event.
    grade : {"good", "doubtful", "reject"}
        The grade.
    failed : list of Evaluation
        The failing criteria's evaluations, reject criteria first.
    passed : list of str
        The check ids of the criteria that passed.
    """

    event_id: str
    grade: str
    failed: list[Evaluation]
    passed: list[str]


def grade(db: sqlite3.Connection, event_id: str) -> Verdict | None:
    """Grade an event's posted solution from the product criteria.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Returns
    -------
    Verdict or None
        The verdict, or None when the event has no main solution or no criterion
        could be evaluated (nothing was posted, or nothing was extracted).
    """
    selected = db.execute(
        "SELECT 1 FROM metrics WHERE event_id = ? AND metric = 'cmt.selected' AND value = 1", (event_id,)
    ).fetchone()
    if selected is None:
        return None
    failed: list[Evaluation] = []
    passed: list[str] = []
    rejecting = doubting = evaluated = 0
    for criteria, rejects in ((REJECT, True), (DOUBT, False)):
        for criterion in criteria:
            evaluations = list(criterion(db, event_id))
            evaluated += bool(evaluations)
            failing = [evaluation for evaluation in evaluations if evaluation.failing]
            if failing:
                failed += failing
                rejecting += rejects
                doubting += not rejects
            else:
                passed += sorted({evaluation.check_id for evaluation in evaluations})
    if not evaluated:
        return None
    if rejecting or doubting >= DOUBTS_TO_REJECT:
        result = "reject"
    elif doubting:
        result = "doubtful"
    else:
        result = "good"
    return Verdict(event_id, result, failed, passed)


def _names(criteria: list[Check]) -> list[str]:
    """The criteria's function names."""
    return [getattr(criterion, "__name__", str(criterion)) for criterion in criteria]


def _facts(db: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    """The posted solution's headline numbers."""
    row = db.execute(
        "SELECT resolution FROM metrics WHERE event_id = ? AND metric = 'cmt.selected' AND value = 1", (event_id,)
    ).fetchone()
    run = None if row is None else row["resolution"]
    return {
        "selected_run": run,
        "mw": get_metric(db, event_id, "", "cmt.mw"),
        "geonet_magnitude": get_metric(db, event_id, "", "cmt.geonet_magnitude"),
        "mw_minus_geonet": get_metric(db, event_id, "", "cmt.mw_minus_geonet"),
        "vr": get_metric(db, event_id, run, "cmt.vr") if run else None,
        "n_stations": get_metric(db, event_id, run, "cmt.n_stations") if run else None,
        "iso_percent": get_metric(db, event_id, run, "cmt.iso_percent") if run else None,
        "deviatoric": get_metric(db, event_id, run, "cmt.deviatoric") if run else None,
        "centroid_depth_km": get_metric(db, event_id, run, "cmt.centroid_depth_km") if run else None,
    }


@cmt_check
def verdict(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.verdict``: the event's posted solution is rejected by the product criteria.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event, with the event id as its subject; failing when the grade
        is ``reject``.
    """
    result = grade(db, event_id)
    if result is None:
        return
    facts = _facts(db, event_id)
    reasons = "; ".join(f"{e.check_id.removeprefix('cmt.')}: {e.summary}" for e in result.failed)
    yield Evaluation(
        check_id="cmt.verdict",
        event_id=event_id,
        failing=result.grade == "reject",
        title="A posted CMT solution is not credible",
        summary=f"{result.grade}: {reasons}" if reasons else result.grade,
        kind="science",
        severity="high",
        subject=event_id,
        value=float(len(result.failed)),
        threshold=float(DOUBTS_TO_REJECT),
        evidence={
            "grade": result.grade,
            "posted": facts,
            "failed_criteria": [
                {"check_id": e.check_id, "summary": e.summary, "value": e.value, "threshold": e.threshold}
                for e in result.failed
            ],
            "passed_criteria": result.passed,
            "rule": f"reject: any of {_names(REJECT)} fails, or {DOUBTS_TO_REJECT} of {_names(DOUBT)};"
            " doubtful: any of the latter",
        },
        suspect_sources=[checks.CMT_SOLUTIONS, checks.BAYESISOLA],
    )


def _window(db: sqlite3.Connection, event_id: str) -> list[str]:
    """The last WINDOW events computed up to and including this one, oldest first."""
    rows = db.execute(
        f"SELECT e.event_id, {SIMULATED_AT} AS at FROM events e"
        f" WHERE at <= (SELECT {SIMULATED_AT} FROM events e WHERE e.event_id = ?)"
        " ORDER BY at DESC, e.event_id DESC LIMIT ?",
        (event_id, WINDOW),
    ).fetchall()
    return [row["event_id"] for row in reversed(rows)]


def _population(
    check_id: str,
    event_id: str,
    failing: bool,
    title: str,
    summary: str,
    value: float,
    threshold: float,
    evidence: dict[str, Any],
) -> Evaluation:
    """A population check's evaluation."""
    return Evaluation(
        check_id=check_id,
        event_id=event_id,
        failing=failing,
        title=title,
        summary=summary,
        kind="science",
        severity="high",
        value=value,
        threshold=threshold,
        evidence={"window_events": WINDOW, **evidence},
        suspect_sources=[checks.CMT_SOLUTIONS, checks.BAYESISOLA],
    )


@cmt_check
def population(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.population.*``: what the last ``WINDOW`` posted solutions say together.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event; the window ends with it.

    Yields
    ------
    Evaluation
        One a population check that has enough events.
    """
    verdicts = [v for v in (grade(db, e) for e in _window(db, event_id)) if v is not None]
    if len(verdicts) < MIN_EVENTS:
        return
    facts = {v.event_id: _facts(db, v.event_id) for v in verdicts}

    rejected = [v for v in verdicts if v.grade == "reject"]
    share = len(rejected) / len(verdicts)
    yield _population(
        "cmt.population.rejected_share",
        event_id,
        share > REJECTED_SHARE_MAX,
        "Many of the posted CMT solutions are not credible",
        f"{len(rejected)} of the last {len(verdicts)} posted solutions are rejected"
        f" ({sum(v.grade == 'doubtful' for v in verdicts)} doubtful, {sum(v.grade == 'good' for v in verdicts)} good).",
        share,
        REJECTED_SHARE_MAX,
        {
            "grades": {v.event_id: v.grade for v in verdicts},
            "rejected": {
                v.event_id: {
                    "failed": [e.check_id for e in v.failed],
                    "vr": facts[v.event_id]["vr"],
                    "n_stations": facts[v.event_id]["n_stations"],
                    "mw": facts[v.event_id]["mw"],
                    "geonet_magnitude": facts[v.event_id]["geonet_magnitude"],
                }
                for v in rejected
            },
            "criteria_failures": _tally(verdicts),
        },
    )

    full = {e: f for e, f in facts.items() if f["deviatoric"] == 0 and f["iso_percent"] is not None}
    if len(full) >= MIN_EVENTS:
        isotropic = {e: f["iso_percent"] for e, f in full.items() if abs(f["iso_percent"]) > checks.ISO_MAX}
        share = len(isotropic) / len(full)
        nested = _nested_vr(db, list(facts))
        yield _population(
            "cmt.population.isotropic_selected",
            event_id,
            share > ISOTROPIC_SHARE_MAX,
            "Many posted CMT solutions are largely isotropic full tensors",
            f"{len(isotropic)} of the last {len(full)} posted full tensors have |ISO| over {checks.ISO_MAX:.0f} %;"
            f" the deviatoric run was selected for {sum(f['deviatoric'] == 1 for f in facts.values())} of {len(facts)}"
            f" events, and its VR exceeded all_stations' in {nested['deviatoric_higher']} of {nested['compared']}.",
            share,
            ISOTROPIC_SHARE_MAX,
            {
                "iso_percent_selected": {e: f["iso_percent"] for e, f in full.items()},
                "over_threshold": isotropic,
                "selected_run": {e: f["selected_run"] for e, f in facts.items()},
                "vr_all_stations_vs_deviatoric": nested,
                "note": "the deviatoric tensor is the full tensor with one parameter fixed, so on the same stations"
                " its VR cannot exceed the full tensor's: a selection by VR alone cannot prefer it",
            },
        )

    offsets = {
        e: f["mw_minus_geonet"] for e, f in facts.items() if f["mw_minus_geonet"] is not None and f["vr"] is not None
    }
    poor = {e: d for e, d in offsets.items() if facts[e]["vr"] < checks.VR_MIN}
    well = {e: d for e, d in offsets.items() if facts[e]["vr"] >= WELL_FITTED_VR}
    if len(poor) >= 2 and len(well) >= 2:
        gap = statistics.median(poor.values()) - statistics.median(well.values())
        yield _population(
            "cmt.population.moment_inflation",
            event_id,
            gap > MOMENT_INFLATION_MAX,
            "Poorly fitted CMT solutions overestimate Mw",
            f"Median Mw - GeoNet M is {statistics.median(poor.values()):+.2f} over {len(poor)} solutions with VR under"
            f" {checks.VR_MIN} and {statistics.median(well.values()):+.2f} over {len(well)} with VR of {WELL_FITTED_VR} or more.",
            gap,
            MOMENT_INFLATION_MAX,
            {
                "poorly_fitted": {e: {"mw_minus_geonet": d, "vr": facts[e]["vr"]} for e, d in poor.items()},
                "well_fitted": {e: {"mw_minus_geonet": d, "vr": facts[e]["vr"]} for e, d in well.items()},
            },
        )


def _tally(verdicts: list[Verdict]) -> dict[str, int]:
    """How many events fail each criterion, most first."""
    counts: dict[str, int] = {}
    for item in verdicts:
        for check_id in {e.check_id for e in item.failed}:
            counts[check_id] = counts.get(check_id, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _nested_vr(db: sqlite3.Connection, events: list[str]) -> dict[str, Any]:
    """All_stations' VR against drop_outliers_dev's, where both ran on the same number of stations."""
    compared = higher = 0
    pairs = {}
    for event in events:
        full = get_metric(db, event, checks.ALL_STATIONS, "cmt.vr")
        deviatoric = get_metric(db, event, checks.DROP_OUTLIERS_DEV, "cmt.vr")
        same = get_metric(db, event, checks.ALL_STATIONS, "cmt.n_stations") == get_metric(
            db, event, checks.DROP_OUTLIERS_DEV, "cmt.n_stations"
        )
        if full is None or deviatoric is None or not same:
            continue
        compared += 1
        higher += deviatoric > full
        pairs[event] = {checks.ALL_STATIONS: full, checks.DROP_OUTLIERS_DEV: deviatoric}
    return {"compared": compared, "deviatoric_higher": higher, "vr": pairs}
