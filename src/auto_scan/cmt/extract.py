"""Metrics of one event's automatic CMT inversion, from SeismicNow's ``scope=all`` download.

Inputs, under the unpacked download (``<cache_dir>/cmt/<event_id>/``): ``event.csv``
(the GeoNet origin inverted), ``cmt_selection.csv`` (one row a run: status, VR, Mw,
whether selected), ``cmt_solution.csv`` (the main solution, GeoNet catalogue format,
Mo in dyne.cm), ``station_vr.csv`` (station VR, robust z and outlier flags),
``timings.csv`` (stage timings of the pipeline and of each run) and, per run,
``<run>/results/{solution_summary,centroid_location,posterior_diagnostics,
station_df,station_jackknife}.csv`` and ``<run>/log.txt``. Moment tensors are in
N.m, USE convention; VR is a fraction. The collector adds ``listing.json``:
SeismicNow's listing row and ``computed_at``.

A run is a directory named in ``cmt_selection.csv``. An event without that file
(nothing to review, e.g. an older layout) gets no rows.

Per-run metrics (resolution = the run's name)

================================== ======= ====================================================================
metric                             unit    meaning
================================== ======= ====================================================================
cmt.run_ok                         bool    1 when the run's status is ``ok``; detail: status, message
cmt.selected                       bool    1 for the main solution's run
cmt.deviatoric                     bool    1 when the run was constrained to a deviatoric MT
cmt.n_stations                     count   stations in the run (cmt_selection.csv)
cmt.vr                             ratio   variance reduction of the run
cmt.condition_number               -       of the inversion at the centroid
cmt.mw / cmt.m0_nm                 - / N.m solution_summary.csv
cmt.dc_percent / clvd / iso        %       decomposition as reported
cmt.m0_tensor_ratio                ratio   (abs(iso) + max abs deviatoric eigenvalue) / M0_Nm, iso = tr(M)/3: 1
                                           when M0 is computed from the tensor (Bowers and Hudson 1999), as
                                           BayesISOLA does
cmt.mw_m0_diff                     -       min over the GCMT (9.05) and IASPEI (9.1) constants of
                                           abs(Mw - (log10 M0 - c) / 1.5); detail: the closest constant
cmt.iso_recomputed_diff            %       abs(ISO% - 100 tr(M)/3 / (abs(tr(M)/3) + max abs deviatoric eigenvalue))
cmt.centroid_depth_km              km      centroid depth
cmt.time_shift_s                   s       centroid time - origin time
cmt.on_grid_edge                   bool    centroid on the search grid's edge; detail: which edge and why
cmt.posterior.effective_cells      count   effective number of space-time cells in the posterior
cmt.posterior.mode_probability     ratio   posterior probability of the best cell
cmt.posterior.boundary_probability ratio   posterior probability on the active search boundary (space or time)
cmt.posterior.variance_scale       ratio   the data variance scale used; detail: uncertainty_scale_mode
cmt.reduced_chi_square             -       misfit / degrees of freedom at the preferred solution
cmt.jackknife.max_kagan_deg        deg     largest Kagan angle when one station is left out; detail: station
cmt.jackknife.max_abs_delta_mw     -       largest abs Mw change when one station is left out
cmt.components_used_fraction       ratio   station_df components used / listed
cmt.station.n_negative_vr          count   used stations whose VR (mean of their used components) is < 0
cmt.station.negative_vr_fraction   ratio   that / stations used; detail: the stations and their VR
cmt.station.min_vr                 ratio   the lowest station VR; detail: station
cmt.mouse_components               count   components log.txt reports as MOUSE (instrument glitch) detections
cmt.kagan_to_selected_deg          deg     Kagan angle to the main solution's tensor
cmt.kagan_to_all_stations_deg      deg     Kagan angle to the all_stations run's tensor
cmt.timing.<stage>_s               s       the run's stage time (timings.csv), e.g. greens_functions, plots
cmt.timing.total_s                 s       the run's total
================================== ======= ====================================================================

Event metrics (resolution ``''``)

================================== ======= ====================================================================
cmt.n_runs / cmt.n_runs_ok         count   runs in cmt_selection.csv, and those ``ok``
cmt.mw                             -       the main solution's Mw; detail: the selected run
cmt.geonet_magnitude               -       event.csv's ``mag_orig`` (GeoNet's magnitude; ``mag``, NZGMDB's ML,
                                           when absent); detail: both and their types
cmt.mw_minus_geonet                -       cmt.mw - cmt.geonet_magnitude
cmt.solution_mo_ratio              ratio   cmt_solution.csv Mo (dyne.cm) / (selected M0_Nm x 1e7)
cmt.solution_mw_diff               -       abs(cmt_solution.csv Mw - selected run's Mw)
cmt.outlier.drop_mismatches        count   stations whose ``dropped`` flag disagrees with being an outlier in
                                           both runs, or whose presence in drop_outliers_dev disagrees with
                                           ``dropped``; detail: the stations
cmt.timing.<stage>_s               s       the pipeline's stage times (timings.csv run ``pipeline``)
cmt.timing.total_s                 s       the pipeline's total
cmt.time_to_solution_s             s       origin time to ``cmt_solution.csv`` written (listing.json
                                           ``computed_at``, from the zip); detail: the times
cmt.time_to_posted_s               s       origin time to SeismicNow applying it (``applied_at``)
================================== ======= ====================================================================
"""

import csv
import datetime as dt
import functools
import json
import math
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from auto_scan.db import set_metric
from auto_scan.registry import cmt_extractor

ALL_STATIONS = "all_stations"
AZIMUTH = "azimuth"
DROP_OUTLIERS_DEV = "drop_outliers_dev"
# Mw = (log10 M0[N.m] - c) / 1.5: GCMT's (Kanamori's 10.7 in dyne.cm) and IASPEI's constants.
MW_CONSTANTS = {"gcmt": 9.05, "iaspei": 9.1}
MOUSE_LINE = re.compile(r"^\s*(\S+)\s+(\S+):\s+MOUSE detected", re.MULTILINE)


def _warn(message: str) -> None:
    """One line on stderr."""
    print(f"auto-scan: cmt extract: {message}", file=sys.stderr)


def read_rows(path: Path) -> list[dict[str, str]]:
    """A CSV's rows, or ``[]`` when it is missing or unreadable.

    Parameters
    ----------
    path : Path
        The CSV.

    Returns
    -------
    list of dict
        One dict a row, by column name.
    """
    if not path.is_file():
        return []
    try:
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeDecodeError) as error:
        _warn(f"{path}: {error}")
        return []


def number(value: Any) -> float | None:
    """A CSV cell as a float; empty, NaN or unparsable as None.

    Parameters
    ----------
    value : Any
        The cell.

    Returns
    -------
    float or None
        The number.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(parsed) else parsed


def flag(value: Any) -> bool:
    """A CSV boolean cell (``True``/``False``, ``1``/``0``) as a bool.

    Parameters
    ----------
    value : Any
        The cell.

    Returns
    -------
    bool
        True for ``true``, ``1``, ``yes`` in any case.
    """
    return str(value).strip().lower() in {"true", "1", "yes"}


def moment_tensor(row: dict[str, str]) -> np.ndarray | None:
    """The symmetric 3x3 tensor (USE) of a solution_summary row, or None if incomplete.

    Parameters
    ----------
    row : dict
        A row with ``Mrr``, ``Mtt``, ``Mpp``, ``Mrt``, ``Mrp``, ``Mtp``.

    Returns
    -------
    numpy.ndarray or None
        The tensor.
    """
    values = [number(row.get(key)) for key in ("Mrr", "Mtt", "Mpp", "Mrt", "Mrp", "Mtp")]
    if any(v is None for v in values):
        return None
    rr, tt, pp, rt, rp, tp = values
    return np.array([[rr, rt, rp], [rt, tt, tp], [rp, tp, pp]], dtype=float)


def _frame(tensor: np.ndarray) -> np.ndarray:
    """Eigenvectors as columns, eigenvalues ascending, made a proper rotation."""
    _, vectors = np.linalg.eigh(tensor)
    if np.linalg.det(vectors) < 0:
        vectors[:, 2] = -vectors[:, 2]
    return vectors


def kagan_angle(a: np.ndarray, b: np.ndarray) -> float:
    """The Kagan angle between two moment tensors' principal axes, in degrees.

    The smallest rotation taking one tensor's eigenvector frame to the other's,
    over the four symmetries of the frame (Kagan 1991), as SeismicNow computes it.

    Parameters
    ----------
    a, b : numpy.ndarray
        Symmetric 3x3 tensors in the same coordinate system.

    Returns
    -------
    float
        The angle, 0 to 120 degrees.
    """

    va, vb = _frame(a), _frame(b)
    best = math.pi
    for signs in ((1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1)):
        rotation = vb @ np.diag(signs) @ va.T
        cosine = max(-1.0, min(1.0, (np.trace(rotation) - 1.0) / 2.0))
        best = min(best, math.acos(cosine))
    return math.degrees(best)


def scalar_moment(tensor: np.ndarray) -> float:
    """The scalar moment of a full tensor: abs(iso) + the largest abs deviatoric eigenvalue.

    Bowers and Hudson (1999); for a deviatoric tensor, the largest abs eigenvalue.

    Parameters
    ----------
    tensor : numpy.ndarray
        A moment tensor.

    Returns
    -------
    float
        M0 in the tensor's units.
    """
    iso = float(np.trace(tensor)) / 3.0
    return abs(iso) + float(np.abs(np.linalg.eigvalsh(tensor) - iso).max())


def iso_percent(tensor: np.ndarray) -> float:
    """The isotropic percentage as BayesISOLA reports it.

    Parameters
    ----------
    tensor : numpy.ndarray
        A moment tensor.

    Returns
    -------
    float
        ``100 iso / (abs(iso) + max abs deviatoric eigenvalue)``, iso = tr/3.
    """
    iso = float(np.trace(tensor)) / 3.0
    deviatoric = np.linalg.eigvalsh(tensor) - iso
    denominator = abs(iso) + float(np.abs(deviatoric).max())
    return 0.0 if denominator == 0 else 100.0 * iso / denominator


def parse_timings(path: Path) -> dict[str, dict[str, float]]:
    """``timings.csv`` as ``{run: {stage: seconds}}``.

    The top-level file has a ``run`` column (``pipeline`` and each run); a run's
    own ``results/timings.csv`` has none, and is returned under ``""``. The time
    is ``inclusive_s`` (``seconds`` when absent).

    Parameters
    ----------
    path : Path
        The file.

    Returns
    -------
    dict
        Seconds by run and stage; stages with no time are left out.
    """
    timings: dict[str, dict[str, float]] = {}
    for row in read_rows(path):
        stage = row.get("stage")
        seconds = number(row.get("inclusive_s"))
        if seconds is None:
            seconds = number(row.get("seconds"))
        if not stage or seconds is None:
            continue
        timings.setdefault(row.get("run", ""), {})[stage] = seconds
    return timings


def station_vrs(rows: list[dict[str, str]]) -> dict[str, float]:
    """Each used station's VR: the mean over its used components.

    Parameters
    ----------
    rows : list of dict
        ``station_df.csv``'s rows.

    Returns
    -------
    dict
        ``NET.STA.LOC`` to VR, for stations with at least one used component.
    """
    by_station: dict[str, list[float]] = {}
    for row in rows:
        vr = number(row.get("variance_reduction"))
        if not flag(row.get("used")) or vr is None:
            continue
        name = f"{row.get('network', '')}.{row.get('station', '')}.{row.get('location', '')}"
        by_station.setdefault(name, []).append(vr)
    return {name: sum(values) / len(values) for name, values in by_station.items()}


def first_row(rows: list[dict[str, str]]) -> dict[str, str]:
    """The first row of a CSV, or an empty one.

    Parameters
    ----------
    rows : list of dict
        ``read_rows``' result.

    Returns
    -------
    dict
        Its first row, or ``{}``.
    """
    return rows[0] if rows else {}


def _extract_run(
    db: sqlite3.Connection,
    event_id: str,
    run: str,
    selection: dict[str, str],
    run_dir: Path,
    timings: dict[str, float],
    tensors: dict[str, np.ndarray],
) -> None:
    """The per-run metrics of one run."""

    def put(metric: str, value: float | None, unit: str | None = None, detail: Any = None) -> None:
        """Record one of this run's metrics."""
        set_metric(db, event_id, run, metric, value, unit, detail)

    status = selection.get("status") or "unknown"
    put("cmt.run_ok", float(status == "ok"), "bool", {"status": status, "message": selection.get("message") or None})
    put("cmt.selected", float(flag(selection.get("selected"))), "bool")
    put("cmt.deviatoric", float(flag(selection.get("deviatoric"))), "bool")
    put("cmt.n_stations", number(selection.get("n_stations")), "count")

    summary = first_row(read_rows(run_dir / "results" / "solution_summary.csv"))
    if summary:
        put("cmt.vr", number(summary.get("variance_reduction")), "ratio")
        put("cmt.condition_number", number(summary.get("condition_number")))
        mw = number(summary.get("Mw"))
        m0 = number(summary.get("M0_Nm"))
        put("cmt.mw", mw)
        put("cmt.m0_nm", m0, "N.m")
        put("cmt.dc_percent", number(summary.get("DC_percent")), "%")
        put("cmt.clvd_percent", number(summary.get("CLVD_percent")), "%")
        reported_iso = number(summary.get("ISO_percent"))
        put("cmt.iso_percent", reported_iso, "%")
        tensor = moment_tensor(summary)
        if tensor is not None:
            tensors[run] = tensor
            if m0:
                put("cmt.m0_tensor_ratio", scalar_moment(tensor) / m0, "ratio")
            if reported_iso is not None:
                recomputed = iso_percent(tensor)
                put("cmt.iso_recomputed_diff", abs(reported_iso - recomputed), "%", {"recomputed": recomputed})
        if mw is not None and m0 and m0 > 0:
            diffs = {name: abs(mw - (math.log10(m0) - c) / 1.5) for name, c in MW_CONSTANTS.items()}
            closest = min(diffs, key=diffs.__getitem__)
            put("cmt.mw_m0_diff", diffs[closest], None, {"closest": closest, "constant": MW_CONSTANTS[closest]})

    centroid = first_row(read_rows(run_dir / "results" / "centroid_location.csv"))
    if centroid:
        put("cmt.centroid_depth_km", number(centroid.get("centroid_depth_km")), "km")
        put("cmt.time_shift_s", number(centroid.get("centroid_time_shift_s")), "s")
        edges = {
            key: flag(centroid.get(key))
            for key in ("on_horizontal_boundary", "on_depth_floor", "on_depth_ceiling", "horizontal_search_fixed")
            if key in centroid
        }
        grid = first_row(read_rows(run_dir / "results" / "grid_edge_diagnostic.csv"))
        put(
            "cmt.on_grid_edge",
            float(flag(centroid.get("on_grid_edge"))),
            "bool",
            {
                **edges,
                "reasons": grid.get("centroid_edge_reasons"),
                "depth_range_km": grid.get("realized_depth_range_km"),
                "radius_km": number(grid.get("realized_radius_km")),
                "centroid_depth_km": number(centroid.get("centroid_depth_km")),
            },
        )

    posterior = first_row(read_rows(run_dir / "results" / "posterior_diagnostics.csv"))
    if posterior:
        put("cmt.posterior.effective_cells", number(posterior.get("posterior_effective_cells")), "count")
        put("cmt.posterior.mode_probability", number(posterior.get("posterior_mode_probability")), "ratio")
        put(
            "cmt.posterior.boundary_probability",
            number(posterior.get("posterior_active_boundary_probability")),
            "ratio",
            {
                key: number(posterior.get(f"posterior_{key}_probability"))
                for key in ("horizontal_boundary", "depth_floor", "depth_ceiling", "time_floor", "time_ceiling")
            },
        )
        put(
            "cmt.posterior.variance_scale",
            number(posterior.get("variance_scale")),
            "ratio",
            {
                "uncertainty_scale_mode": posterior.get("uncertainty_scale_mode"),
                "sd_scale": number(posterior.get("sd_scale")),
            },
        )
        put(
            "cmt.reduced_chi_square",
            number(posterior.get("reduced_chi_square")),
            None,
            {
                "degrees_of_freedom": number(posterior.get("degrees_of_freedom")),
                "n_space_time_cells": number(posterior.get("n_space_time_cells")),
            },
        )

    jackknife = read_rows(run_dir / "results" / "station_jackknife.csv")
    kagans = [(number(r.get("kagan_angle_deg")), r) for r in jackknife]
    kagans = [(k, r) for k, r in kagans if k is not None]
    if kagans:
        worst, row = max(kagans, key=lambda item: item[0])
        put("cmt.jackknife.max_kagan_deg", worst, "deg", {"station": f"{row.get('network')}.{row.get('station')}"})
    deltas = [abs(d) for d in (number(r.get("delta_Mw")) for r in jackknife) if d is not None]
    if deltas:
        put("cmt.jackknife.max_abs_delta_mw", max(deltas))

    components = read_rows(run_dir / "results" / "station_df.csv")
    if components:
        used = sum(flag(row.get("used")) for row in components)
        put("cmt.components_used_fraction", used / len(components), "ratio", {"used": used, "listed": len(components)})
        vrs = station_vrs(components)
        if vrs:
            negative = {name: round(vr, 3) for name, vr in sorted(vrs.items(), key=lambda kv: kv[1]) if vr < 0}
            put("cmt.station.n_negative_vr", float(len(negative)), "count")
            put(
                "cmt.station.negative_vr_fraction",
                len(negative) / len(vrs),
                "ratio",
                {"stations": negative, "n_used": len(vrs)},
            )
            lowest = min(vrs, key=vrs.__getitem__)
            put("cmt.station.min_vr", vrs[lowest], "ratio", {"station": lowest})

    log = run_dir / "log.txt"
    if log.is_file():
        put("cmt.mouse_components", float(len(MOUSE_LINE.findall(log.read_text(errors="replace")))), "count")

    for stage, seconds in timings.items():
        put(f"cmt.timing.{stage}_s", seconds, "s")


def _listed_stations(path: Path) -> set[str]:
    """The station codes a run's station_df.csv lists, used or not."""
    return {row["station"] for row in read_rows(path) if row.get("station")}


def _outlier_mismatches(event_dir: Path, runs: list[str]) -> list[dict[str, Any]]:
    """Stations whose drop decision disagrees with the outlier flags or with the drop_outliers_dev run."""
    rows = read_rows(event_dir / "station_vr.csv")
    if not rows or DROP_OUTLIERS_DEV not in runs:
        return []
    outlier_columns = [f"outlier_{ALL_STATIONS}", f"outlier_{AZIMUTH}"]
    if any(column not in rows[0] for column in [*outlier_columns, "dropped"]):
        return []
    in_dev = _listed_stations(event_dir / DROP_OUTLIERS_DEV / "results" / "station_df.csv")
    in_all = _listed_stations(event_dir / ALL_STATIONS / "results" / "station_df.csv")
    mismatches = []
    for row in rows:
        station = str(row.get("station_id", ""))
        code = station.split(".")[1] if station.count(".") >= 1 else station
        dropped = flag(row.get("dropped"))
        rule = all(flag(row.get(column)) for column in outlier_columns)
        reasons = []
        if dropped != rule:
            reasons.append(f"dropped={dropped} but outlier in both runs={rule}")
        if in_dev and in_all and code in in_all:
            if dropped and code in in_dev:
                reasons.append("dropped but still used in drop_outliers_dev")
            if not dropped and code not in in_dev:
                reasons.append("not dropped but missing from drop_outliers_dev")
        if reasons:
            mismatches.append({"station": station, "reasons": reasons})
    return mismatches


@cmt_extractor
def extract_cmt(db: sqlite3.Connection, event_id: str, event_dir: Path) -> None:
    """Write every ``cmt.*`` metric of one event's CMT inversion.

    Parameters
    ----------
    db : sqlite3.Connection
        The CMT database; not committed.
    event_id : str
        The event.
    event_dir : Path
        The unpacked ``scope=all`` download.
    """
    db.execute("DELETE FROM metrics WHERE event_id = ? AND metric LIKE 'cmt.%'", (event_id,))
    selection = read_rows(event_dir / "cmt_selection.csv")
    if not selection:
        _warn(f"{event_id}: no cmt_selection.csv in {event_dir}; nothing to review")
        return
    runs = [row["run"] for row in selection if row.get("run")]
    timings = parse_timings(event_dir / "timings.csv")
    tensors: dict[str, np.ndarray] = {}
    for row in selection:
        run = row.get("run")
        if not run:
            continue
        _extract_run(db, event_id, run, row, event_dir / run, timings.get(run, {}), tensors)

    put = functools.partial(set_metric, db, event_id, "")

    put("cmt.n_runs", float(len(runs)), "count")
    put("cmt.n_runs_ok", float(sum(row.get("status") == "ok" for row in selection)), "count")
    selected = next((row for row in selection if flag(row.get("selected"))), None)
    if selected is not None:
        selected_run = selected["run"]
        mw = number(selected.get("Mw"))
        put("cmt.mw", mw, None, {"run": selected_run})
        origin = first_row(read_rows(event_dir / "event.csv"))
        # mag_orig is GeoNet's own magnitude; mag is NZGMDB's recomputed (uncorrected) ML.
        geonet = number(origin.get("mag_orig"))
        geonet_type = origin.get("mag_orig_type")
        if geonet is None:
            geonet, geonet_type = number(origin.get("mag")), origin.get("mag_type")
        put(
            "cmt.geonet_magnitude",
            geonet,
            None,
            {
                "type": geonet_type,
                "mag": number(origin.get("mag")),
                "mag_type": origin.get("mag_type"),
                "mag_orig": number(origin.get("mag_orig")),
                "mag_orig_type": origin.get("mag_orig_type"),
            },
        )
        if mw is not None and geonet is not None:
            put("cmt.mw_minus_geonet", mw - geonet, None, {"type": geonet_type})
        catalogue = first_row(read_rows(event_dir / "cmt_solution.csv"))
        summary = first_row(read_rows(event_dir / selected_run / "results" / "solution_summary.csv"))
        m0 = number(summary.get("M0_Nm"))
        mo = number(catalogue.get("Mo"))
        if m0 and mo is not None:
            put("cmt.solution_mo_ratio", mo / (m0 * 1e7), "ratio", {"Mo_dyne_cm": mo, "M0_Nm": m0})
        catalogue_mw = number(catalogue.get("Mw"))
        run_mw = number(summary.get("Mw"))
        if catalogue_mw is not None and run_mw is not None:
            put("cmt.solution_mw_diff", abs(catalogue_mw - run_mw), None, {"catalogue": catalogue_mw, "run": run_mw})
        if selected_run in tensors:
            for run, tensor in tensors.items():
                set_metric(
                    db, event_id, run, "cmt.kagan_to_selected_deg", kagan_angle(tensors[selected_run], tensor), "deg"
                )
    if ALL_STATIONS in tensors:
        for run, tensor in tensors.items():
            set_metric(
                db, event_id, run, "cmt.kagan_to_all_stations_deg", kagan_angle(tensors[ALL_STATIONS], tensor), "deg"
            )
    if (event_dir / "station_vr.csv").is_file() and DROP_OUTLIERS_DEV in runs:
        mismatches = _outlier_mismatches(event_dir, runs)
        put("cmt.outlier.drop_mismatches", float(len(mismatches)), "count", mismatches)
    for stage, seconds in timings.get("pipeline", {}).items():
        put(f"cmt.timing.{stage}_s", seconds, "s")
    _extract_latency(event_dir, put)


def _seconds_between(start: str | None, end: str | None) -> float | None:
    """Seconds from one ISO 8601 time to another (naive ones are UTC), or None."""
    times = []
    for value in (start, end):
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace(" ", "T"))
        except ValueError:
            return None
        times.append(parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC))
    return (times[1] - times[0]).total_seconds()


def _extract_latency(event_dir: Path, put: Callable[..., None]) -> None:
    """Origin to computed and to posted, from the collector's listing.json."""
    path = event_dir / "listing.json"
    if not path.is_file():
        return
    try:
        listing = json.loads(path.read_text())
    except ValueError as error:
        _warn(f"{path}: {error}")
        return
    origin = listing.get("origin_time") or first_row(read_rows(event_dir / "event.csv")).get("datetime")
    if not origin:
        return
    detail = {"origin_time": origin, "computed_at": listing.get("computed_at"), "applied_at": listing.get("applied_at")}
    if listing.get("computed_at"):
        put("cmt.time_to_solution_s", _seconds_between(origin, listing["computed_at"]), "s", detail)
    if listing.get("applied_at"):
        put("cmt.time_to_posted_s", _seconds_between(origin, listing["applied_at"]), "s", detail)
