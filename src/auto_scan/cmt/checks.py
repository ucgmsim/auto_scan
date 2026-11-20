"""Checks of each automatic CMT inversion: is the solution internally consistent and
believable, and did the pipeline spend its time where it should?

Each check reads only ``cmt.db`` (the metrics ``auto_scan.cmt.extract`` writes) and
aims at one kind of bug. A check with history compares with the median of the last
``HISTORY_WINDOW`` earlier events once there are ``MIN_HISTORY``, and with an absolute
bound before that. Check ids start ``cmt.``; a finding's suspect sources are
``cmt_solutions`` (``auto_cmt``: the three runs, the outlier rule, the selection, the
grid set-up, sharing work between runs) and ``BayesISOLA`` (the inversion itself:
Green's functions, the tensor and its decomposition, the posterior, the figures).

Science

- ``cmt.run_failed`` [run]: a run did not finish ``ok``.
- ``cmt.deviatoric_not_enforced`` [run]: a run constrained to be deviatoric has an
  isotropic part.
- ``cmt.moment_inconsistent`` [m0_tensor, mw_m0, iso_percent, catalogue_mo,
  catalogue_mw]: the reported M0, Mw and ISO% disagree with the tensor they come
  from, or the posted catalogue row with the run it came from (a unit or convention
  bug).
- ``cmt.selected_on_grid_edge``: the main solution's centroid is on the edge of the
  search grid, or most of its posterior is, so the best fit may lie outside it.
- ``cmt.selected_few_stations``: the main solution rests on too few stations.
- ``cmt.posterior_overconfident``: the misfit is many times what the data covariance
  expects but the posterior is not rescaled, so the reported uncertainty is too small.
- ``cmt.runs_disagree``: the event's runs give different mechanisms.
- ``cmt.magnitude_offset``: Mw is outside the range GeoNet's magnitude allows.
- ``cmt.selected_isotropic``: the main solution, a full tensor, is largely isotropic.
- ``cmt.no_signal``: the main solution fits noise.
- ``cmt.outlier_drop_mismatch``: the stations dropped for drop_outliers_dev are not
  the ones the outlier flags name.
- ``cmt.jackknife_unstable``: leaving one station out turns the main solution.
- ``cmt.negative_vr_stations``: a large share of the main solution's stations fit
  worse than no prediction at all.
- ``cmt.low_variance_reduction``: the main solution barely fits the data.

Performance

- ``cmt.perf.slow_pipeline``: the whole pipeline took too long.
- ``cmt.perf.stage_regression`` [<run>:<stage>]: one stage slowed down against its
  history.
- ``cmt.perf.repeated_preprocessing``: every run repeats the waveform preprocessing
  instead of sharing it, as the waveforms and Green's functions are shared.
- ``cmt.perf.unattributed_time`` [run]: much of a run's time is in no timed stage.
- ``cmt.perf.time_to_solution``: the CMT was computed long after the earthquake.
"""

import json
import math
import sqlite3
import statistics
from collections.abc import Iterator
from typing import Any

from auto_scan.db import get_metric, is_backlog, metric_history
from auto_scan.registry import Evaluation, Kind, Severity, cmt_check

CMT_SOLUTIONS = "cmt_solutions"
BAYESISOLA = "BayesISOLA"
ALL_STATIONS = "all_stations"
DROP_OUTLIERS_DEV = "drop_outliers_dev"

# DESIGN.md: history once 5 earlier events exist, the median of the last 10.
MIN_HISTORY = 5
HISTORY_WINDOW = 10

# A deviatoric run's ISO% is zero up to rounding (4e-15 % on 2026p866048); 0.5 % is
# far above rounding and far below any real isotropic part.
DEVIATORIC_ISO_MAX = 0.5
# M0 and Mo are the same number in different units; 1 % allows for CSV rounding.
MOMENT_REL_TOL = 0.01
# Mw from M0 matches to 1e-15 on 2026p866048; 0.01 is far below a convention change
# (9.05 vs 9.1 gives 0.033).
MW_TOL = 0.01
# cmt_solution.csv rounds Mw to 2 decimals: at most 0.005 off, plus float noise.
CATALOGUE_MW_TOL = 0.006
# ISO% recomputed from the tensor matches to 1e-14 on 2026p866048.
ISO_TOL = 0.5
# More than half the posterior on the search boundary: the best cell is likely outside
# the grid even when the centroid flag says it isn't (0.9999 on 2026p862161, whose
# on_grid_edge was False).
BOUNDARY_PROBABILITY_MAX = 0.5
# A full moment tensor has 6 unknowns, plus the centroid's position and time. Three
# 3-component stations is the least that constrains it; the azimuth run refuses fewer
# than 3 occupied 45-degree sectors for the same reason.
MIN_STATIONS = 3
# With the variance scale fixed at 1, a reduced chi-square of 3 already means the
# reported standard deviations are sqrt(3) = 1.7 times too small.
REDUCED_CHI_SQUARE_MAX = 3.0
# Kagan angles under about 20-30 degrees are the scatter between routine moment tensor
# catalogues for the same earthquakes (Kagan 2003, Pondrelli et al. 2007); 30 degrees
# between two runs on the same waveforms is a different mechanism.
KAGAN_MAX_DEG = 30.0
# Leaving one of ~30 stations out should not turn the solution further than two
# catalogues disagree; 40 degrees leaves room for the few-station events.
JACKKNIFE_KAGAN_MAX_DEG = 40.0
# Between the main solution and any other run of the event: 60 degrees is a different
# mechanism (2026p866048's good solution is 33 degrees from its 10-station azimuth run).
RUN_KAGAN_MAX_DEG = 60.0
# Mw - GeoNet M. NZ's ML runs about 0.2-0.5 above Mw for small events (Ristau 2009), and
# the two well-fitted events here are at -0.39 and -0.55. Mw a half unit above GeoNet's
# magnitude, or a whole unit below, is outside that; fixed, so a run of poor solutions
# can't move it.
MAGNITUDE_OFFSET_RANGE = (-1.0, 0.5)
# A tectonic earthquake is a shear source. Regional full-tensor inversions through a 1-D
# model carry spurious isotropic parts of 10-20 % (-9 and -10 % on the two well-fitted
# events here); 30 % is beyond that.
ISO_MAX = 30.0
# VR under 0.1 with a reduced chi-square under 2: the synthetics explain almost nothing
# and the noise covariance explains the rest (2026p830300: VR 0.011, chi-square 0.53).
NO_SIGNAL_VR = 0.1
NO_SIGNAL_CHI_SQUARE = 2.0
# A third of the stations fitting worse than zero points at polarity, response or
# Green's functions, not at a few noisy stations (8 of 34 on 2026p866048).
NEGATIVE_VR_FRACTION_MAX = 1.0 / 3.0
# The selection keeps the highest-VR run; below 0.2 the main solution explains a fifth
# of the waveform energy (0.55 on 2026p866048).
VR_MIN = 0.2
# The pipeline posts in 16 min on 2026p866048 (34 stations); an hour is far beyond
# what more stations explain and too slow for a near-realtime CMT.
PIPELINE_MAX_S = 3600.0
# A stage regressed when it takes twice its median and at least a minute more.
STAGE_RATIO_MAX = 2.0
STAGE_MIN_EXTRA_S = 60.0
# Stages under 5 s are timer noise.
STAGE_MIN_S = 5.0
# Stages whose work grows with the stations: compared per station, so a 10-station
# event is not a regression against 34-station ones.
PER_STATION_STAGES = {
    "download",
    "load_waveforms",
    "mouse_detection",
    "process_data",
    "trim_filter",
    "greens_functions",
    "shift_data",
    "jackknife",
    "plots",
}
# Waveform preprocessing, done again by every run (2026p866048: 162 s in all_stations
# and 162 s again in drop_outliers_dev on the same 34 stations).
PREPROCESSING_STAGES = ("mouse_detection", "process_data")
# Shared work would cost about the largest run's; 1.5 times that is a run's worth repeated.
PREPROCESSING_RATIO_MAX = 1.5
# The "other" stage is time no timer covers: 11-21 % on 2026p866048. A quarter of a
# run unaccounted hides where the time goes.
UNATTRIBUTED_FRACTION_MAX = 0.25
# Origin to cmt_solution.csv: 28 min on 2026p866048. Two hours means the pipeline waited or
# queued, not that the inversion was slow.
TIME_TO_SOLUTION_MAX_S = 2 * 3600.0


def _runs(db: sqlite3.Connection, event_id: str) -> list[str]:
    """The event's CMT runs, by name."""
    return [
        row["resolution"]
        for row in db.execute("SELECT resolution FROM runs WHERE event_id = ? ORDER BY resolution", (event_id,))
    ]


def _ok(db: sqlite3.Connection, event_id: str, run: str) -> bool:
    """Whether a run finished ``ok``: a failed run's timings stop where it failed."""
    return get_metric(db, event_id, run, "cmt.run_ok") == 1


def _selected_run(db: sqlite3.Connection, event_id: str) -> str | None:
    """The run of the main solution."""
    row = db.execute(
        "SELECT resolution FROM metrics WHERE event_id = ? AND metric = 'cmt.selected' AND value = 1", (event_id,)
    ).fetchone()
    return None if row is None else row["resolution"]


def _detail(db: sqlite3.Connection, event_id: str, resolution: str, metric: str) -> Any:
    """A metric's detail, decoded."""
    row = db.execute(
        "SELECT detail FROM metrics WHERE event_id = ? AND resolution = ? AND metric = ?",
        (event_id, resolution, metric),
    ).fetchone()
    if row is None or row["detail"] is None:
        return None
    try:
        return json.loads(row["detail"])
    except ValueError:
        return row["detail"]


def _baseline(
    db: sqlite3.Connection, metric: str, resolution: str, event_id: str
) -> tuple[float | None, list[tuple[str, float]]]:
    """The median of the last HISTORY_WINDOW earlier values, if there are MIN_HISTORY."""
    history = metric_history(db, metric, resolution, before_event=event_id)[-HISTORY_WINDOW:]
    if len(history) < MIN_HISTORY:
        return None, history
    return statistics.median(value for _, value in history), history


def _evaluation(
    check_id: str,
    event_id: str,
    failing: bool,
    title: str,
    summary: str,
    *,
    kind: Kind = "science",
    severity: Severity = "medium",
    resolution: str = "",
    subject: str = "",
    value: float | None = None,
    threshold: float | None = None,
    evidence: dict[str, Any] | None = None,
    suspects: list[str] | None = None,
) -> Evaluation:
    """An evaluation with the CMT review's defaults."""
    return Evaluation(
        check_id=check_id,
        event_id=event_id,
        failing=failing,
        title=title,
        summary=summary,
        kind=kind,
        severity=severity,
        resolution=resolution,
        subject=subject,
        value=value,
        threshold=threshold,
        evidence=evidence or {},
        suspect_sources=suspects or [CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def run_failed(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.run_failed``: a run's status is not ``ok``.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a run.
    """
    for run in _runs(db, event_id):
        ok = get_metric(db, event_id, run, "cmt.run_ok")
        if ok is None:
            continue
        detail = _detail(db, event_id, run, "cmt.run_ok") or {}
        yield _evaluation(
            "cmt.run_failed",
            event_id,
            ok < 1,
            "An automatic CMT run did not finish",
            f"{run}: status {detail.get('status')!r}" + (f", {detail.get('message')}" if detail.get("message") else ""),
            severity="high",
            resolution=run,
            subject=run,
            value=ok,
            threshold=1.0,
            evidence={
                "run": run,
                "metric": "cmt.run_ok",
                **detail,
                "files": ["cmt_selection.csv", f"{run}/cmt_run.log", "cmt_1d_run.log"],
            },
        )


@cmt_check
def deviatoric_not_enforced(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.deviatoric_not_enforced``: a deviatoric run with an isotropic part.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a deviatoric run.
    """
    for run in _runs(db, event_id):
        if get_metric(db, event_id, run, "cmt.deviatoric") != 1:
            continue
        iso = get_metric(db, event_id, run, "cmt.iso_percent")
        if iso is None:
            continue
        yield _evaluation(
            "cmt.deviatoric_not_enforced",
            event_id,
            abs(iso) > DEVIATORIC_ISO_MAX,
            "A deviatoric CMT run has an isotropic component",
            f"{run} is constrained deviatoric but reports ISO {iso:.3g} %.",
            severity="high",
            resolution=run,
            subject=run,
            value=abs(iso),
            threshold=DEVIATORIC_ISO_MAX,
            evidence={
                "run": run,
                "metrics": {"cmt.iso_percent": iso, "cmt.deviatoric": 1},
                "file": f"{run}/results/solution_summary.csv",
            },
            suspects=[BAYESISOLA, CMT_SOLUTIONS],
        )


@cmt_check
def moment_inconsistent(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.moment_inconsistent``: M0, Mw, ISO% or the posted row disagree with their source.

    Subjects: ``m0_tensor`` (M0_Nm against the tensor's scalar moment,
    ``extract.scalar_moment``), ``mw_m0`` (Mw against M0 by either standard constant),
    ``iso_percent`` (against the tensor), ``catalogue_mo`` and ``catalogue_mw``
    (cmt_solution.csv against the selected run). Each evaluated on every run where
    it applies; the event fails a subject when any run does.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a run and subject, and one a catalogue subject.
    """
    title = "CMT moment, magnitude or decomposition disagree with the moment tensor"
    for run in _runs(db, event_id):
        file = f"{run}/results/solution_summary.csv"
        ratio = get_metric(db, event_id, run, "cmt.m0_tensor_ratio")
        if ratio is not None:
            yield _evaluation(
                "cmt.moment_inconsistent",
                event_id,
                abs(ratio - 1) > MOMENT_REL_TOL,
                title,
                f"{run}: the tensor's scalar moment is {ratio:.4g} times M0_Nm.",
                severity="high",
                resolution=run,
                subject="m0_tensor",
                value=abs(ratio - 1),
                threshold=MOMENT_REL_TOL,
                evidence={
                    "run": run,
                    "metric": "cmt.m0_tensor_ratio",
                    "value": ratio,
                    "definition": "abs(tr(M)/3) + max abs deviatoric eigenvalue (Bowers and Hudson 1999)",
                    "file": file,
                },
                suspects=[BAYESISOLA],
            )
        diff = get_metric(db, event_id, run, "cmt.mw_m0_diff")
        if diff is not None:
            yield _evaluation(
                "cmt.moment_inconsistent",
                event_id,
                diff > MW_TOL,
                title,
                f"{run}: Mw is {diff:.3g} from (log10 M0 - c) / 1.5 for both c = 9.05 and c = 9.1.",
                severity="high",
                resolution=run,
                subject="mw_m0",
                value=diff,
                threshold=MW_TOL,
                evidence={
                    "run": run,
                    "metric": "cmt.mw_m0_diff",
                    "value": diff,
                    "detail": _detail(db, event_id, run, "cmt.mw_m0_diff"),
                    "file": file,
                },
                suspects=[BAYESISOLA],
            )
        iso = get_metric(db, event_id, run, "cmt.iso_recomputed_diff")
        if iso is not None:
            yield _evaluation(
                "cmt.moment_inconsistent",
                event_id,
                iso > ISO_TOL,
                title,
                f"{run}: ISO% is {iso:.3g} points from the tensor's.",
                severity="high",
                resolution=run,
                subject="iso_percent",
                value=iso,
                threshold=ISO_TOL,
                evidence={
                    "run": run,
                    "metric": "cmt.iso_recomputed_diff",
                    "value": iso,
                    "reported": get_metric(db, event_id, run, "cmt.iso_percent"),
                    "detail": _detail(db, event_id, run, "cmt.iso_recomputed_diff"),
                    "file": file,
                },
                suspects=[BAYESISOLA],
            )
    mo = get_metric(db, event_id, "", "cmt.solution_mo_ratio")
    if mo is not None:
        yield _evaluation(
            "cmt.moment_inconsistent",
            event_id,
            abs(mo - 1) > MOMENT_REL_TOL,
            title,
            f"cmt_solution.csv's Mo is {mo:.4g} times the selected run's M0 in dyne.cm.",
            severity="high",
            subject="catalogue_mo",
            value=abs(mo - 1),
            threshold=MOMENT_REL_TOL,
            evidence={
                "metric": "cmt.solution_mo_ratio",
                "value": mo,
                "detail": _detail(db, event_id, "", "cmt.solution_mo_ratio"),
                "files": ["cmt_solution.csv", "<selected run>/results/solution_summary.csv"],
            },
            suspects=[CMT_SOLUTIONS],
        )
    mw = get_metric(db, event_id, "", "cmt.solution_mw_diff")
    if mw is not None:
        yield _evaluation(
            "cmt.moment_inconsistent",
            event_id,
            mw > CATALOGUE_MW_TOL,
            title,
            f"cmt_solution.csv's Mw is {mw:.3g} from the selected run's.",
            severity="high",
            subject="catalogue_mw",
            value=mw,
            threshold=CATALOGUE_MW_TOL,
            evidence={
                "metric": "cmt.solution_mw_diff",
                "detail": _detail(db, event_id, "", "cmt.solution_mw_diff"),
                "files": ["cmt_solution.csv", "<selected run>/results/solution_summary.csv"],
            },
            suspects=[CMT_SOLUTIONS],
        )


@cmt_check
def selected_on_grid_edge(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.selected_on_grid_edge``: the main solution's centroid is on the grid's edge.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run.
    """
    run = _selected_run(db, event_id)
    if run is None:
        return
    edge = get_metric(db, event_id, run, "cmt.on_grid_edge")
    boundary = get_metric(db, event_id, run, "cmt.posterior.boundary_probability")
    if edge is None and boundary is None:
        return
    detail = _detail(db, event_id, run, "cmt.on_grid_edge") or {}
    others = {r: get_metric(db, event_id, r, "cmt.on_grid_edge") for r in _runs(db, event_id) if r != run}
    # The posterior's mass on the boundary, or 1 when the centroid itself is on it.
    value = max(edge or 0.0, boundary or 0.0)
    yield _evaluation(
        "cmt.selected_on_grid_edge",
        event_id,
        value > BOUNDARY_PROBABILITY_MAX,
        "The main CMT solution's centroid is on the edge of the search grid",
        f"{run} (selected): centroid at {detail.get('centroid_depth_km')} km, on_grid_edge {bool(edge)}"
        f" ({detail.get('reasons')}), posterior probability on the search boundary {boundary}.",
        resolution=run,
        value=value,
        threshold=BOUNDARY_PROBABILITY_MAX,
        evidence={
            "selected_run": run,
            "edge": detail,
            "posterior_boundary_probability": boundary,
            "posterior_boundary": _detail(db, event_id, run, "cmt.posterior.boundary_probability"),
            "other_runs_on_edge": others,
            "files": [
                f"{run}/results/centroid_location.csv",
                f"{run}/results/grid_edge_diagnostic.csv",
                f"{run}/results/adaptive_grid_history.csv",
            ],
        },
        suspects=[CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def selected_few_stations(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.selected_few_stations``: the main solution rests on too few stations.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run.
    """
    run = _selected_run(db, event_id)
    if run is None:
        return
    stations = get_metric(db, event_id, run, "cmt.n_stations")
    if stations is None:
        return
    failed = {
        r: (_detail(db, event_id, r, "cmt.run_ok") or {}).get("message")
        for r in _runs(db, event_id)
        if get_metric(db, event_id, r, "cmt.run_ok") == 0
    }
    mw = get_metric(db, event_id, "", "cmt.mw")
    yield _evaluation(
        "cmt.selected_few_stations",
        event_id,
        stations < MIN_STATIONS,
        "The main CMT solution rests on too few stations",
        f"{run} (selected) inverted {stations:.0f} station(s) and was posted"
        + (f" as Mw {mw:.2f}." if mw is not None else "."),
        severity="high",
        resolution=run,
        value=stations,
        threshold=MIN_STATIONS,
        evidence={
            "run": run,
            "n_stations": stations,
            "vr": get_metric(db, event_id, run, "cmt.vr"),
            "failed_runs": failed,
            "geonet": _detail(db, event_id, "", "cmt.geonet_magnitude"),
            "files": ["cmt_selection.csv", f"{run}/results/station_df.csv"],
        },
        suspects=[CMT_SOLUTIONS],
    )


@cmt_check
def posterior_overconfident(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.posterior_overconfident``: misfit far above the data covariance, posterior not rescaled.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a run.
    """
    for run in _runs(db, event_id):
        chi2 = get_metric(db, event_id, run, "cmt.reduced_chi_square")
        scale = get_metric(db, event_id, run, "cmt.posterior.variance_scale")
        if chi2 is None or scale is None:
            continue
        # A rescaled posterior multiplies the variance by about chi2: then chi2 / scale is near 1.
        effective = chi2 / scale if scale > 0 else chi2
        cells = get_metric(db, event_id, run, "cmt.posterior.effective_cells")
        yield _evaluation(
            "cmt.posterior_overconfident",
            event_id,
            effective > REDUCED_CHI_SQUARE_MAX,
            "CMT posterior uncertainty is not scaled to the misfit",
            f"{run}: reduced chi-square {chi2:.3g} with variance scale {scale:.3g} "
            f"({_detail(db, event_id, run, 'cmt.posterior.variance_scale') or {}}); "
            f"posterior effective cells {cells}: standard deviations about {math.sqrt(max(effective, 0)):.2g} times too small.",
            resolution=run,
            value=effective,
            threshold=REDUCED_CHI_SQUARE_MAX,
            evidence={
                "run": run,
                "metrics": {
                    "cmt.reduced_chi_square": chi2,
                    "cmt.posterior.variance_scale": scale,
                    "cmt.posterior.effective_cells": cells,
                    "cmt.posterior.mode_probability": get_metric(db, event_id, run, "cmt.posterior.mode_probability"),
                },
                "detail": _detail(db, event_id, run, "cmt.reduced_chi_square"),
                "files": [f"{run}/results/posterior_diagnostics.csv", f"{run}/results/solution_unc_diagnostics.csv"],
            },
            suspects=[BAYESISOLA, CMT_SOLUTIONS],
        )


@cmt_check
def runs_disagree(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.runs_disagree``: the event's runs give different mechanisms or depths.

    Two tests, on the runs that finished ``ok``. The main solution against every
    other run (``RUN_KAGAN_MAX_DEG``): the runs invert the same earthquake, so a
    different mechanism in any of them means the answer depends on the station
    set or the constraint. And all_stations against drop_outliers_dev
    (``KAGAN_MAX_DEG``, tighter): these two share their stations unless some
    were dropped.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event, when at least two runs succeeded.
    """
    selected = _selected_run(db, event_id)
    ok = [run for run in _runs(db, event_id) if _ok(db, event_id, run)]
    if selected is None or len(ok) < 2:
        return
    to_selected = {run: get_metric(db, event_id, run, "cmt.kagan_to_selected_deg") for run in ok if run != selected}
    to_selected = {run: kagan for run, kagan in to_selected.items() if kagan is not None}
    same_stations = None
    if ALL_STATIONS in ok and DROP_OUTLIERS_DEV in ok:
        same_stations = get_metric(db, event_id, DROP_OUTLIERS_DEV, "cmt.kagan_to_all_stations_deg")
    if not to_selected and same_stations is None:
        return
    worst_run = max(to_selected, key=to_selected.__getitem__) if to_selected else None
    worst = to_selected[worst_run] if worst_run else 0.0
    # The value is the larger of the two tests, each over its own threshold.
    ratios = [worst / RUN_KAGAN_MAX_DEG]
    if same_stations is not None:
        ratios.append(same_stations / KAGAN_MAX_DEG)
    depths = {run: get_metric(db, event_id, run, "cmt.centroid_depth_km") for run in ok}
    yield _evaluation(
        "cmt.runs_disagree",
        event_id,
        max(ratios) > 1.0,
        "CMT runs of one event give different mechanisms",
        f"Kagan angle to the main solution ({selected}): "
        + ", ".join(f"{run} {kagan:.1f} deg" for run, kagan in to_selected.items())
        + (f"; all_stations to drop_outliers_dev {same_stations:.1f} deg" if same_stations is not None else "")
        + f"; centroid depths {', '.join(f'{run} {depth:g} km' for run, depth in depths.items() if depth is not None)}.",
        value=max(ratios),
        threshold=1.0,
        evidence={
            "selected_run": selected,
            "kagan_to_selected_deg": to_selected,
            "kagan_all_stations_to_drop_outliers_dev_deg": same_stations,
            "thresholds_deg": {"any_run": RUN_KAGAN_MAX_DEG, "same_stations": KAGAN_MAX_DEG},
            "centroid_depth_km": depths,
            "vr": {run: get_metric(db, event_id, run, "cmt.vr") for run in ok},
            "n_stations": {run: get_metric(db, event_id, run, "cmt.n_stations") for run in ok},
            "iso_percent": {run: get_metric(db, event_id, run, "cmt.iso_percent") for run in ok},
            "files": [f"{run}/results/solution_summary.csv" for run in ok],
        },
    )


@cmt_check
def magnitude_offset(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.magnitude_offset``: Mw outside the range GeoNet's magnitude allows.

    Against fixed bounds, not the history: a history of poor solutions would
    make a poor one look usual.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event.
    """
    offset = get_metric(db, event_id, "", "cmt.mw_minus_geonet")
    mw = get_metric(db, event_id, "", "cmt.mw")
    geonet = get_metric(db, event_id, "", "cmt.geonet_magnitude")
    if offset is None or mw is None or geonet is None:
        return
    low, high = MAGNITUDE_OFFSET_RANGE
    excess = max(low - offset, offset - high)
    yield _evaluation(
        "cmt.magnitude_offset",
        event_id,
        excess > 0,
        "CMT Mw is far from GeoNet's magnitude",
        f"Mw {mw:.2f} - GeoNet M {geonet:.2f} = {offset:+.2f}, outside {low:+.1f} to {high:+.1f}."
        if excess > 0
        else f"Mw {mw:.2f} - GeoNet M {geonet:.2f} = {offset:+.2f}.",
        severity="high" if excess > 0.5 else "medium",
        value=offset,
        threshold=high if offset > high else low,
        evidence={
            "metric": "cmt.mw_minus_geonet",
            "offset": offset,
            "allowed_range": [low, high],
            "geonet": _detail(db, event_id, "", "cmt.geonet_magnitude"),
            "selected": _detail(db, event_id, "", "cmt.mw"),
            "history": metric_history(db, "cmt.mw_minus_geonet", "", before_event=event_id)[-HISTORY_WINDOW:],
            "files": ["event.csv", "cmt_solution.csv"],
        },
        suspects=[BAYESISOLA, CMT_SOLUTIONS],
    )


@cmt_check
def selected_isotropic(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.selected_isotropic``: the main solution is largely isotropic.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run, when it is a full tensor.
    """
    run = _selected_run(db, event_id)
    if run is None or get_metric(db, event_id, run, "cmt.deviatoric") == 1:
        return
    iso = get_metric(db, event_id, run, "cmt.iso_percent")
    if iso is None:
        return
    runs = [r for r in _runs(db, event_id) if _ok(db, event_id, r)]
    yield _evaluation(
        "cmt.selected_isotropic",
        event_id,
        abs(iso) > ISO_MAX,
        "The main CMT solution is largely isotropic",
        f"{run} (selected, full tensor): ISO {iso:+.0f} %, DC {get_metric(db, event_id, run, 'cmt.dc_percent') or float('nan'):.0f} %.",
        resolution=run,
        value=abs(iso),
        threshold=ISO_MAX,
        evidence={
            "run": run,
            "iso_percent": {r: get_metric(db, event_id, r, "cmt.iso_percent") for r in runs},
            "dc_percent": {r: get_metric(db, event_id, r, "cmt.dc_percent") for r in runs},
            "vr": {r: get_metric(db, event_id, r, "cmt.vr") for r in runs},
            "deviatoric": {r: get_metric(db, event_id, r, "cmt.deviatoric") for r in runs},
            "n_stations": get_metric(db, event_id, run, "cmt.n_stations"),
            "file": f"{run}/results/solution_summary.csv",
        },
        suspects=[CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def no_signal(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.no_signal``: the main solution explains almost none of the data, which the noise explains.

    VR under ``NO_SIGNAL_VR`` while the misfit is within ``NO_SIGNAL_CHI_SQUARE``
    of what the noise covariance expects: the band holds no signal above the
    noise, and the tensor (and its Mw) is a fit to noise.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run.
    """
    run = _selected_run(db, event_id)
    if run is None:
        return
    vr = get_metric(db, event_id, run, "cmt.vr")
    chi2 = get_metric(db, event_id, run, "cmt.reduced_chi_square")
    if vr is None or chi2 is None:
        return
    yield _evaluation(
        "cmt.no_signal",
        event_id,
        vr < NO_SIGNAL_VR and chi2 < NO_SIGNAL_CHI_SQUARE,
        "A CMT was posted for waveforms with no signal above the noise",
        f"{run} (selected): VR {vr:.3f} with reduced chi-square {chi2:.2f}; posted as Mw"
        f" {get_metric(db, event_id, '', 'cmt.mw') or float('nan'):.2f}.",
        severity="high",
        resolution=run,
        value=vr,
        threshold=NO_SIGNAL_VR,
        evidence={
            "run": run,
            "vr": vr,
            "reduced_chi_square": chi2,
            "n_stations": get_metric(db, event_id, run, "cmt.n_stations"),
            "geonet": _detail(db, event_id, "", "cmt.geonet_magnitude"),
            "mw_minus_geonet": get_metric(db, event_id, "", "cmt.mw_minus_geonet"),
            "files": [f"{run}/results/posterior_diagnostics.csv", f"{run}/figures/waveform_record_section.png"],
        },
        suspects=[CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def outlier_drop_mismatch(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.outlier_drop_mismatch``: dropped stations differ from the outlier flags.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event.
    """
    count = get_metric(db, event_id, "", "cmt.outlier.drop_mismatches")
    if count is None:
        return
    yield _evaluation(
        "cmt.outlier_drop_mismatch",
        event_id,
        count > 0,
        "Stations dropped for drop_outliers_dev don't match the outlier flags",
        f"{count:.0f} station(s) whose drop disagrees with station_vr.csv or with the drop_outliers_dev run.",
        value=count,
        threshold=0.0,
        evidence={
            "stations": _detail(db, event_id, "", "cmt.outlier.drop_mismatches"),
            "files": ["station_vr.csv", "cmt_1d_run.log", f"{DROP_OUTLIERS_DEV}/results/station_df.csv"],
        },
        suspects=[CMT_SOLUTIONS],
    )


@cmt_check
def jackknife_unstable(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.jackknife_unstable``: one station left out turns the main solution.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run.
    """
    run = _selected_run(db, event_id)
    if run is None:
        return
    kagan = get_metric(db, event_id, run, "cmt.jackknife.max_kagan_deg")
    if kagan is None:
        return
    detail = _detail(db, event_id, run, "cmt.jackknife.max_kagan_deg") or {}
    yield _evaluation(
        "cmt.jackknife_unstable",
        event_id,
        kagan > JACKKNIFE_KAGAN_MAX_DEG,
        "The main CMT solution depends on a single station",
        f"{run} (selected): leaving {detail.get('station')} out turns the mechanism {kagan:.1f} deg.",
        severity="low",
        resolution=run,
        value=kagan,
        threshold=JACKKNIFE_KAGAN_MAX_DEG,
        evidence={
            "run": run,
            "station": detail.get("station"),
            "n_stations": get_metric(db, event_id, run, "cmt.n_stations"),
            "max_abs_delta_mw": get_metric(db, event_id, run, "cmt.jackknife.max_abs_delta_mw"),
            "file": f"{run}/results/station_jackknife.csv",
        },
        suspects=[CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def negative_vr_stations(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.negative_vr_stations``: many of the main solution's stations fit worse than zero.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run.
    """
    run = _selected_run(db, event_id)
    if run is None:
        return
    fraction = get_metric(db, event_id, run, "cmt.station.negative_vr_fraction")
    if fraction is None:
        return
    detail = _detail(db, event_id, run, "cmt.station.negative_vr_fraction") or {}
    yield _evaluation(
        "cmt.negative_vr_stations",
        event_id,
        fraction > NEGATIVE_VR_FRACTION_MAX,
        "Many stations fit the main CMT solution worse than no prediction",
        f"{run} (selected): {len(detail.get('stations') or {})} of {detail.get('n_used')} stations have negative VR.",
        resolution=run,
        value=fraction,
        threshold=NEGATIVE_VR_FRACTION_MAX,
        evidence={
            "run": run,
            **detail,
            "mouse_components": get_metric(db, event_id, run, "cmt.mouse_components"),
            "files": [f"{run}/results/station_df.csv", "station_vr.csv", f"{run}/figures/station_fit_summary.png"],
        },
        suspects=[BAYESISOLA, CMT_SOLUTIONS],
    )


@cmt_check
def low_variance_reduction(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.low_variance_reduction``: the main solution barely fits.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the selected run.
    """
    run = _selected_run(db, event_id)
    if run is None:
        return
    vr = get_metric(db, event_id, run, "cmt.vr")
    if vr is None:
        return
    yield _evaluation(
        "cmt.low_variance_reduction",
        event_id,
        vr < VR_MIN,
        "The main CMT solution barely fits the waveforms",
        f"{run} (selected): variance reduction {vr:.2f}.",
        resolution=run,
        value=vr,
        threshold=VR_MIN,
        evidence={
            "run": run,
            "vr_by_run": {r: get_metric(db, event_id, r, "cmt.vr") for r in _runs(db, event_id)},
            "mw": get_metric(db, event_id, "", "cmt.mw"),
            "file": "cmt_selection.csv",
        },
    )


@cmt_check
def slow_pipeline(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.perf.slow_pipeline``: the whole pipeline took too long.

    Above ``PIPELINE_MAX_S``, or twice the history's median.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event.
    """
    total = get_metric(db, event_id, "", "cmt.timing.total_s")
    if total is None:
        return
    baseline, history = _baseline(db, "cmt.timing.total_s", "", event_id)
    threshold = PIPELINE_MAX_S if baseline is None else min(PIPELINE_MAX_S, STAGE_RATIO_MAX * baseline)
    stages = {
        row["metric"].removeprefix("cmt.timing.").removesuffix("_s"): row["value"]
        for row in db.execute(
            "SELECT metric, value FROM metrics WHERE event_id = ? AND resolution = '' AND metric LIKE 'cmt.timing.%'",
            (event_id,),
        )
    }
    yield _evaluation(
        "cmt.perf.slow_pipeline",
        event_id,
        total > threshold,
        "The automatic CMT pipeline is slow",
        f"Pipeline took {total / 60:.1f} min" + (f" against a usual {baseline / 60:.1f} min" if baseline else "") + ".",
        kind="perf",
        value=total,
        threshold=threshold,
        evidence={
            "metric": "cmt.timing.total_s",
            "stages_s": stages,
            "baseline_median": baseline,
            "history": history,
            "n_stations": get_metric(db, event_id, ALL_STATIONS, "cmt.n_stations"),
            "file": "timings.csv",
        },
        suspects=[CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def stage_regression(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.perf.stage_regression``: a stage slowed down against its history.

    Per run and stage (subject ``<run>:<stage>``, ``pipeline:<stage>`` for the
    pipeline's own); per station for ``PER_STATION_STAGES``. Only once there is
    history, and only for stages whose median is at least ``STAGE_MIN_S``.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a run and stage with history.
    """
    rows = db.execute(
        "SELECT resolution, metric, value FROM metrics WHERE event_id = ? AND metric LIKE 'cmt.timing.%'"
        " AND metric != 'cmt.timing.total_s' AND value IS NOT NULL",
        (event_id,),
    ).fetchall()
    for row in rows:
        run, metric, seconds = row["resolution"], row["metric"], row["value"]
        if run and not _ok(db, event_id, run):
            continue
        stage = metric.removeprefix("cmt.timing.").removesuffix("_s")
        baseline, history = _baseline(db, metric, run, event_id)
        if baseline is None or baseline < STAGE_MIN_S:
            continue
        stations = get_metric(db, event_id, run, "cmt.n_stations") if run else None
        per_station = stage in PER_STATION_STAGES and bool(stations)
        if per_station:
            earlier = []
            for past, value in history:
                n = get_metric(db, past, run, "cmt.n_stations")
                if n:
                    earlier.append(value / n)
            if len(earlier) < MIN_HISTORY:
                continue
            reference = statistics.median(earlier) * stations
        else:
            reference = baseline
        ratio = seconds / reference if reference > 0 else math.inf
        yield _evaluation(
            "cmt.perf.stage_regression",
            event_id,
            ratio > STAGE_RATIO_MAX and seconds - reference > STAGE_MIN_EXTRA_S,
            "An automatic CMT stage slowed down",
            f"{run or 'pipeline'} {stage}: {seconds:.0f} s, {ratio:.2g} times the usual {reference:.0f} s"
            + (f" for {stations:.0f} stations" if per_station else "")
            + ".",
            kind="perf",
            resolution=run,
            subject=f"{run or 'pipeline'}:{stage}",
            value=ratio,
            threshold=STAGE_RATIO_MAX,
            evidence={
                "metric": metric,
                "seconds": seconds,
                "expected_s": reference,
                "per_station": per_station,
                "n_stations": stations,
                "history": history,
                "file": "timings.csv",
            },
            suspects=[BAYESISOLA, CMT_SOLUTIONS],
        )


@cmt_check
def repeated_preprocessing(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.perf.repeated_preprocessing``: every run redoes the waveform preprocessing.

    The runs invert the same waveforms (drop_outliers_dev all of all_stations'
    stations but the dropped ones, azimuth a subset), and already share the
    download and the Green's functions. The value is the preprocessing time summed
    over the runs over the largest run's: about 1 when shared.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event, when at least two runs have timings.
    """
    by_run: dict[str, dict[str, float]] = {}
    for run in _runs(db, event_id):
        if not _ok(db, event_id, run):
            continue
        stages = {stage: get_metric(db, event_id, run, f"cmt.timing.{stage}_s") for stage in PREPROCESSING_STAGES}
        known = {stage: value for stage, value in stages.items() if value is not None}
        if len(known) == len(PREPROCESSING_STAGES):
            by_run[run] = known
    if len(by_run) < 2:
        return
    totals = {run: sum(stages.values()) for run, stages in by_run.items()}
    largest = max(totals.values())
    if largest < STAGE_MIN_S:
        return
    ratio = sum(totals.values()) / largest
    yield _evaluation(
        "cmt.perf.repeated_preprocessing",
        event_id,
        ratio > PREPROCESSING_RATIO_MAX,
        "Each CMT run repeats the waveform preprocessing",
        f"{' + '.join(f'{run} {t:.0f} s' for run, t in totals.items())} of {', '.join(PREPROCESSING_STAGES)}:"
        f" {ratio:.2g} times the largest run's.",
        kind="perf",
        severity="low",
        value=ratio,
        threshold=PREPROCESSING_RATIO_MAX,
        evidence={
            "stages_s": by_run,
            "n_stations": {run: get_metric(db, event_id, run, "cmt.n_stations") for run in by_run},
            "pipeline_total_s": get_metric(db, event_id, "", "cmt.timing.total_s"),
            "files": ["timings.csv", "<run>/log.txt"],
        },
        suspects=[CMT_SOLUTIONS, BAYESISOLA],
    )


@cmt_check
def unattributed_time(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.perf.unattributed_time``: much of a run's time is in no timed stage.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One a run with timings.
    """
    for run in _runs(db, event_id):
        if not _ok(db, event_id, run):
            continue
        other = get_metric(db, event_id, run, "cmt.timing.other_s")
        total = get_metric(db, event_id, run, "cmt.timing.total_s")
        if other is None or not total:
            continue
        fraction = other / total
        yield _evaluation(
            "cmt.perf.unattributed_time",
            event_id,
            fraction > UNATTRIBUTED_FRACTION_MAX,
            "Much of an automatic CMT run's time is in no timed stage",
            f"{run}: {other:.0f} s of {total:.0f} s ({fraction:.0%}) is in the 'other' stage.",
            kind="perf",
            severity="low",
            resolution=run,
            value=fraction,
            threshold=UNATTRIBUTED_FRACTION_MAX,
            evidence={"run": run, "other_s": other, "total_s": total, "file": "timings.csv"},
            suspects=[BAYESISOLA, CMT_SOLUTIONS],
        )


@cmt_check
def time_to_solution(db: sqlite3.Connection, event_id: str) -> Iterator[Evaluation]:
    """``cmt.perf.time_to_solution``: the CMT was computed long after the earthquake.

    From the origin time to ``cmt_solution.csv`` being written. A backlog event
    (computed after a later earthquake's) is skipped: it was not run as it happened.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database.
    event_id : str
        The event.

    Yields
    ------
    Evaluation
        One for the event.
    """
    seconds = get_metric(db, event_id, "", "cmt.time_to_solution_s")
    if seconds is None or is_backlog(db, event_id):
        return
    pipeline = get_metric(db, event_id, "", "cmt.timing.total_s")
    yield _evaluation(
        "cmt.perf.time_to_solution",
        event_id,
        seconds > TIME_TO_SOLUTION_MAX_S,
        "The automatic CMT is computed long after the earthquake",
        f"Computed {seconds / 60:.0f} min after the origin; the pipeline itself took "
        + (f"{pipeline / 60:.0f} min." if pipeline else "an unknown time."),
        kind="perf",
        value=seconds,
        threshold=TIME_TO_SOLUTION_MAX_S,
        evidence={
            "metric": "cmt.time_to_solution_s",
            "times": _detail(db, event_id, "", "cmt.time_to_solution_s"),
            "time_to_posted_s": get_metric(db, event_id, "", "cmt.time_to_posted_s"),
            "pipeline_total_s": pipeline,
        },
        suspects=[CMT_SOLUTIONS],
    )
