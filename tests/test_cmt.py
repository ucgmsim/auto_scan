"""Tests of the CMT review: extraction from a synthetic SeismicNow download, the checks
on planted bugs and on clean runs, and which events the collector takes.

The fixture mirrors the layout of SeismicNow's ``cmt_results?scope=all`` zip for
2026p866048, cut down to the columns the extractor reads.
"""

import csv
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from auto_scan import engine, issues, publish, registry
from auto_scan.cmt import checks, collect, extract, source
from auto_scan.config import Config
from auto_scan.db import connect, set_metric

EVENT = "2026p866048"
# 2026p866048's all_stations tensor (N.m, USE): a strike-slip with ISO -10 %.
TENSOR = {
    "Mrr": 4350972039766950.5,
    "Mtt": 5061798033714388.0,
    "Mpp": -1.456451647015713e16,
    "Mrt": -1695745400012436.5,
    "Mrp": 4958711128514116.0,
    "Mtp": -5740849472565725.0,
}


def tensor_matrix(values: dict[str, float]) -> np.ndarray:
    return np.array(
        [
            [values["Mrr"], values["Mrt"], values["Mrp"]],
            [values["Mrt"], values["Mtt"], values["Mtp"]],
            [values["Mrp"], values["Mtp"], values["Mpp"]],
        ]
    )


def deviatoric(values: dict[str, float]) -> dict[str, float]:
    iso = (values["Mrr"] + values["Mtt"] + values["Mpp"]) / 3
    return {**values, "Mrr": values["Mrr"] - iso, "Mtt": values["Mtt"] - iso, "Mpp": values["Mpp"] - iso}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summary_row(
    values: dict[str, float], *, m0_scale: float = 1.0, mw_constant: float = 9.05, iso_offset: float = 0.0
) -> dict[str, Any]:
    """A solution_summary.csv row consistent with the tensor, unless a bug is planted."""
    matrix = tensor_matrix(values)
    m0 = extract.scalar_moment(matrix) * m0_scale
    return {
        "variance_reduction": 0.55,
        "condition_number": 1.9,
        **values,
        "M0_Nm": m0,
        "Mw": (math.log10(m0) - mw_constant) / 1.5,
        "DC_percent": 35.0,
        "CLVD_percent": -55.0,
        "ISO_percent": extract.iso_percent(matrix) + iso_offset,
    }


def write_run(root: Path, run: str, values: dict[str, float], stations: list[str], **bugs: Any) -> dict[str, Any]:
    """One run's results directory; returns its summary row."""
    summary = summary_row(values, **bugs)
    results = root / run / "results"
    write_csv(results / "solution_summary.csv", [summary])
    write_csv(
        results / "centroid_location.csv",
        [
            {
                "centroid_depth_km": 10.0,
                "centroid_time_shift_s": 1.02,
                "on_grid_edge": False,
                "on_horizontal_boundary": False,
                "on_depth_floor": False,
                "on_depth_ceiling": False,
            }
        ],
    )
    write_csv(results / "grid_edge_diagnostic.csv", [{"realized_radius_km": 10.1, "centroid_edge_reasons": "[]"}])
    write_csv(
        results / "posterior_diagnostics.csv",
        [
            {
                "posterior_effective_cells": 4.0,
                "posterior_mode_probability": 0.4,
                "posterior_active_boundary_probability": 0.01,
                "uncertainty_scale_mode": "chi2",
                "variance_scale": 1.2,
                "sd_scale": 1.1,
                "reduced_chi_square": 1.3,
                "degrees_of_freedom": 21554,
            }
        ],
    )
    write_csv(
        results / "station_df.csv",
        [
            {"network": "NZ", "station": s, "location": "10", "component": c, "used": True, "variance_reduction": 0.6}
            for s in stations
            for c in "ZNE"
        ],
    )
    write_csv(
        results / "station_jackknife.csv",
        [{"network": "NZ", "station": s, "kagan_angle_deg": 5.0, "delta_Mw": 0.01} for s in stations],
    )
    (root / run / "log.txt").write_text(
        "Mouse detection:\n  INZ HHN: MOUSE detected, ignoring component in inversion\n"
    )
    return summary


def write_download(root: Path, *, dev_tensor: dict[str, float] | None = None, **bugs: Any) -> Path:
    """A clean three-run download, with optional bugs in the all_stations run."""
    stations = ["INZ", "LTZ", "WVZ", "GVZ"]
    all_summary = write_run(root, "all_stations", TENSOR, stations, **bugs)
    write_run(root, "azimuth", TENSOR, stations[:3])
    write_run(root, "drop_outliers_dev", dev_tensor or deviatoric(TENSOR), stations)
    write_csv(
        root / "cmt_selection.csv",
        [
            {
                "run": "all_stations",
                "status": "ok",
                "message": "",
                "deviatoric": False,
                "n_stations": 4,
                "variance_reduction": 0.55,
                "Mw": all_summary["Mw"],
                "selected": True,
            },
            {
                "run": "azimuth",
                "status": "ok",
                "message": "",
                "deviatoric": False,
                "n_stations": 3,
                "variance_reduction": 0.4,
                "Mw": all_summary["Mw"],
                "selected": False,
            },
            {
                "run": "drop_outliers_dev",
                "status": "ok",
                "message": "",
                "deviatoric": True,
                "n_stations": 4,
                "variance_reduction": 0.5,
                "Mw": all_summary["Mw"],
                "selected": False,
            },
        ],
    )
    write_csv(
        root / "cmt_solution.csv",
        [
            {
                "PublicID": EVENT,
                "Mw": round(all_summary["Mw"], 2),
                "Mo": all_summary["M0_Nm"] * 1e7,
                "CD": 10.0,
                "Latitude": -43.03,
                "Longitude": 171.75,
            }
        ],
    )
    write_csv(
        root / "event.csv",
        [
            {
                "evid": EVENT,
                "datetime": "2026-11-17T10:30:48.522607Z",
                "mag": 5.66,
                "mag_type": "ML",
                "mag_orig": 5.33,
                "mag_orig_type": "M",
            }
        ],
    )
    write_csv(
        root / "station_vr.csv",
        [
            {"station_id": f"NZ.{s}.10", "outlier_all_stations": False, "outlier_azimuth": False, "dropped": False}
            for s in stations
        ],
    )
    write_csv(
        root / "timings.csv",
        [
            {"run": "pipeline", "stage": "total", "inclusive_s": 962.8},
            {"run": "pipeline", "stage": "run_all_stations", "inclusive_s": 489.5},
            {"run": "pipeline", "stage": "figures", "inclusive_s": 279.5},
            *[
                {"run": run, "stage": stage, "inclusive_s": seconds}
                for run in ("all_stations", "azimuth", "drop_outliers_dev")
                for stage, seconds in (
                    ("mouse_detection", 90.0),
                    ("process_data", 70.0),
                    ("other", 20.0),
                    ("total", 400.0),
                )
            ],
        ],
    )
    (root / "listing.json").write_text(
        json.dumps(
            {
                "event_id": EVENT,
                "origin_time": "2026-11-17T10:30:48+00:00",
                "computed_at": "2026-11-17T10:58:20+00:00",
                "applied_at": "2026-11-17T10:58:38+00:00",
            }
        )
    )
    return root


def ingested(tmp_path: Path, **kwargs: Any) -> sqlite3.Connection:
    """A CMT database holding one event extracted from a synthetic download."""
    root = write_download(tmp_path / EVENT, **kwargs)
    db = connect(":memory:")
    db.execute(
        "INSERT INTO events (event_id, event_dir, origin_time) VALUES (?, ?, ?)",
        (EVENT, EVENT, "2026-11-17T10:30:48+00:00"),
    )
    for run in ("all_stations", "azimuth", "drop_outliers_dev"):
        db.execute(
            "INSERT INTO runs (event_id, resolution, status, written) VALUES (?, ?, 'ok', ?)",
            (EVENT, run, "2026-11-17T10:58:20+00:00"),
        )
    extract.extract_cmt(db, EVENT, root)
    return db


def failing(db: sqlite3.Connection, check: Any) -> dict[str, list[str]]:
    """The subjects a check fails, with the runs (resolutions) it fails on."""
    out: dict[str, list[str]] = {}
    for evaluation in check(db, EVENT):
        if evaluation.failing:
            out.setdefault(evaluation.subject, []).append(evaluation.resolution)
    return out


def test_kagan_angle_and_scalar_moment() -> None:
    m = tensor_matrix(TENSOR)
    assert extract.kagan_angle(m, m) == pytest.approx(0.0, abs=1e-6)
    assert extract.kagan_angle(m, 3.0 * m) == pytest.approx(0.0, abs=1e-6)
    # A vertical strike-slip turned about its null (vertical) axis: 45 degrees is a
    # 45-degree Kagan angle, 90 swaps P and T (90), 180 is the same mechanism (0).
    strike_slip = np.array([[0.0, 0, 0], [0, 0, 1], [0, 1, 0]])

    def turned(degrees: float) -> np.ndarray:
        c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        rotation = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        return rotation @ strike_slip @ rotation.T

    assert extract.kagan_angle(strike_slip, turned(45)) == pytest.approx(45.0, abs=1e-6)
    assert extract.kagan_angle(strike_slip, turned(90)) == pytest.approx(90.0, abs=1e-6)
    assert extract.kagan_angle(strike_slip, turned(180)) == pytest.approx(0.0, abs=1e-6)
    assert extract.kagan_angle(strike_slip, -strike_slip) == pytest.approx(90.0, abs=1e-6)
    # 2026p866048's all_stations M0 (README) from its tensor.
    assert extract.scalar_moment(m) == pytest.approx(1.7018155827283576e16, rel=1e-9)
    assert extract.iso_percent(m) == pytest.approx(-10.090686771940554, rel=1e-9)


def test_extract_clean_download(tmp_path: Path) -> None:
    db = ingested(tmp_path)
    value = lambda run, metric: db.execute(
        "SELECT value FROM metrics WHERE event_id = ? AND resolution = ? AND metric = ?", (EVENT, run, metric)
    ).fetchone()[0]
    assert value("all_stations", "cmt.m0_tensor_ratio") == pytest.approx(1.0)
    assert value("all_stations", "cmt.mw_m0_diff") == pytest.approx(0.0, abs=1e-9)
    assert value("drop_outliers_dev", "cmt.iso_percent") == pytest.approx(0.0, abs=1e-6)
    assert value("all_stations", "cmt.selected") == 1
    assert value("azimuth", "cmt.kagan_to_selected_deg") == pytest.approx(0.0, abs=1e-6)
    assert value("all_stations", "cmt.mouse_components") == 1
    assert value("all_stations", "cmt.timing.process_data_s") == 70.0
    assert value("", "cmt.timing.total_s") == 962.8
    assert value("", "cmt.mw_minus_geonet") == pytest.approx(value("all_stations", "cmt.mw") - 5.33)
    assert value("", "cmt.solution_mo_ratio") == pytest.approx(1.0)
    assert value("", "cmt.outlier.drop_mismatches") == 0
    assert value("", "cmt.time_to_solution_s") == pytest.approx(27 * 60 + 32)
    # Re-extracting replaces rather than duplicates.
    extract.extract_cmt(db, EVENT, tmp_path / EVENT)
    assert db.execute("SELECT count(*) FROM metrics WHERE metric = 'cmt.vr'").fetchone()[0] == 3


def test_extract_without_selection_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / EVENT
    root.mkdir()
    (root / "README.txt").write_text("an older layout")
    db = connect(":memory:")
    extract.extract_cmt(db, EVENT, root)
    assert db.execute("SELECT count(*) FROM metrics").fetchone()[0] == 0


def test_every_check_passes_a_clean_event(tmp_path: Path) -> None:
    db = ingested(tmp_path)
    registry.load_cmt_plugins()
    evaluations = [e for check in registry.CMT_CHECKS for e in check(db, EVENT)]
    assert {e.check_id for e in evaluations} >= {
        "cmt.run_failed",
        "cmt.deviatoric_not_enforced",
        "cmt.moment_inconsistent",
        "cmt.selected_on_grid_edge",
        "cmt.selected_few_stations",
        "cmt.posterior_overconfident",
        "cmt.runs_disagree",
        "cmt.magnitude_offset",
        "cmt.outlier_drop_mismatch",
        "cmt.jackknife_unstable",
        "cmt.negative_vr_stations",
        "cmt.low_variance_reduction",
        "cmt.perf.slow_pipeline",
        "cmt.perf.unattributed_time",
        "cmt.perf.time_to_solution",
    }
    assert [e for e in evaluations if e.failing and e.check_id != "cmt.perf.repeated_preprocessing"] == []
    assert all(e.check_id.startswith("cmt.") for e in evaluations)


def test_repeated_preprocessing_fires_on_the_real_pattern(tmp_path: Path) -> None:
    # Every run of the fixture spends 160 s preprocessing, as 2026p866048's did.
    db = ingested(tmp_path)
    [evaluation] = list(checks.repeated_preprocessing(db, EVENT))
    assert evaluation.failing and evaluation.value == pytest.approx(3.0)


def test_m0_bug_is_caught(tmp_path: Path) -> None:
    db = ingested(tmp_path, m0_scale=10.0)
    assert failing(db, checks.moment_inconsistent) == {"m0_tensor": ["all_stations"]}


def test_mw_convention_change_passes_but_a_wrong_constant_fails(tmp_path: Path) -> None:
    assert failing(ingested(tmp_path / "a", mw_constant=9.1), checks.moment_inconsistent) == {}
    assert "mw_m0" in failing(ingested(tmp_path / "b", mw_constant=9.3), checks.moment_inconsistent)


def test_iso_bug_is_caught(tmp_path: Path) -> None:
    assert failing(ingested(tmp_path, iso_offset=5.0), checks.moment_inconsistent) == {"iso_percent": ["all_stations"]}


def test_deviatoric_constraint_not_applied(tmp_path: Path) -> None:
    db = ingested(tmp_path, dev_tensor=TENSOR)
    assert failing(db, checks.deviatoric_not_enforced) == {"drop_outliers_dev": ["drop_outliers_dev"]}


def test_runs_disagree(tmp_path: Path) -> None:
    flipped = {**TENSOR, "Mrp": -TENSOR["Mrp"], "Mtp": -TENSOR["Mtp"] * 3}
    db = ingested(tmp_path, dev_tensor=deviatoric(flipped))
    [evaluation] = list(checks.runs_disagree(db, EVENT))
    assert evaluation.failing
    assert evaluation.value is not None and evaluation.value > checks.KAGAN_MAX_DEG


def test_selected_on_grid_edge_from_posterior(tmp_path: Path) -> None:
    db = ingested(tmp_path)
    set_metric(db, EVENT, "all_stations", "cmt.posterior.boundary_probability", 0.9999)
    [evaluation] = list(checks.selected_on_grid_edge(db, EVENT))
    assert evaluation.failing


def test_two_station_solution_and_failed_run(tmp_path: Path) -> None:
    db = ingested(tmp_path)
    set_metric(db, EVENT, "all_stations", "cmt.n_stations", 2.0)
    set_metric(
        db, EVENT, "azimuth", "cmt.run_ok", 0.0, "bool", {"status": "failed", "message": "ValueError: 2 sectors"}
    )
    assert failing(db, checks.selected_few_stations) == {"": ["all_stations"]}
    assert failing(db, checks.run_failed) == {"azimuth": ["azimuth"]}
    # A failed run's timings stop where it failed: no perf verdict on them.
    set_metric(db, EVENT, "azimuth", "cmt.timing.other_s", 390.0)
    assert failing(db, checks.unattributed_time) == {}


def test_posterior_not_rescaled(tmp_path: Path) -> None:
    db = ingested(tmp_path)
    set_metric(db, EVENT, "all_stations", "cmt.reduced_chi_square", 36.9)
    set_metric(db, EVENT, "all_stations", "cmt.posterior.variance_scale", 1.0)
    assert failing(db, checks.posterior_overconfident) == {"": ["all_stations"]}


def test_magnitude_offset_absolute_then_history(tmp_path: Path) -> None:
    db = ingested(tmp_path)
    set_metric(db, EVENT, "", "cmt.mw_minus_geonet", 1.42)
    [evaluation] = list(checks.magnitude_offset(db, EVENT))
    assert evaluation.failing and evaluation.evidence["basis"] == "absolute"
    # With history around -0.4, -0.3 passes and +0.6 fails.
    for day, value in enumerate([-0.4, -0.5, -0.3, -0.45, -0.35, -0.4], start=1):
        past = f"2026p00000{day}"
        db.execute(
            "INSERT INTO events (event_id, event_dir, origin_time) VALUES (?, ?, ?)",
            (past, past, f"2026-11-0{day}T00:00:00+00:00"),
        )
        db.execute(
            "INSERT INTO runs (event_id, resolution, written) VALUES (?, 'all_stations', ?)",
            (past, f"2026-11-0{day}T01:00:00+00:00"),
        )
        set_metric(db, past, "", "cmt.mw_minus_geonet", value)
    set_metric(db, EVENT, "", "cmt.mw_minus_geonet", -0.3)
    assert not next(iter(checks.magnitude_offset(db, EVENT))).failing
    set_metric(db, EVENT, "", "cmt.mw_minus_geonet", 0.6)
    [evaluation] = list(checks.magnitude_offset(db, EVENT))
    assert evaluation.failing and evaluation.evidence["basis"] == "history"


def test_outlier_drop_mismatch(tmp_path: Path) -> None:
    root = write_download(tmp_path / EVENT)
    rows = extract.read_rows(root / "station_vr.csv")
    rows[0].update(outlier_all_stations="True", outlier_azimuth="True", dropped="False")
    write_csv(root / "station_vr.csv", rows)
    db = connect(":memory:")
    extract.extract_cmt(db, EVENT, root)
    [evaluation] = list(checks.outlier_drop_mismatch(db, EVENT))
    assert evaluation.failing and evaluation.evidence["stations"][0]["station"] == "NZ.INZ.10"


def test_engine_runs_only_cmt_checks(tmp_path: Path) -> None:
    db = ingested(tmp_path, m0_scale=10.0)
    registry.load_cmt_plugins()
    engine.run_checks(db, checks=registry.CMT_CHECKS)
    finding = db.execute("SELECT * FROM findings WHERE check_id = 'cmt.moment_inconsistent'").fetchone()
    assert finding["subject"] == "m0_tensor" and finding["status"] == "open"
    assert db.execute("SELECT count(*) FROM evaluations WHERE check_id NOT LIKE 'cmt.%'").fetchone()[0] == 0
    text = issues.brief(db, review="cmt", config=Config())
    assert "cmt_results?event_id=2026p866048&scope=all" in text
    assert "ucgmsim/cmt_solutions" in text and "AaronJR474/BayesISOLA" in text
    assert "rclone" not in text


def listed(event_id: str, applied: str | None) -> source.CmtEvent:
    return source.CmtEvent(event_id=event_id, origin_time="2026-11-17T10:30:48+00:00", applied_at=applied)


def test_new_postings_after_the_watermark_and_reruns() -> None:
    db = connect(":memory:")
    db.execute("INSERT INTO meta VALUES ('cmt_applied:old', '2026-11-01T00:00:00+00:00')")
    events = [
        listed("new", "2026-11-17T10:58:38+00:00"),
        listed("before", "2026-11-10T00:00:00+00:00"),  # posted before the watermark: backlog, not taken
        listed("old", "2026-11-01T00:00:00+00:00"),  # unchanged since ingested
        listed("never", None),  # selected run, but SeismicNow never applied it
    ]
    since = "2026-11-15T00:00:00+00:00"
    assert [e.event_id for e in collect.new_postings(db, events, since)] == ["new"]
    rerun = [listed("old", "2026-11-18T00:00:00+00:00")]
    assert [e.event_id for e in collect.new_postings(db, rerun, since)] == ["old"]


def fake_download(tmp_path: Path) -> Any:
    def download(config: Config, event_id: str, dest: Path) -> dict[str, str]:
        write_download(dest)
        return {"cmt_solution.csv": "2026-11-17T10:58:20", "cmt_selection.csv": "2026-11-17T11:03:00"}

    return download


def test_first_run_sets_the_watermark_and_takes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(cache_dir=tmp_path)
    db = connect(":memory:")
    monkeypatch.setattr(source, "list_cmt_events", lambda config: pytest.fail("listed on the first run"))
    collect.collect(config, db)
    assert db.execute("SELECT value FROM meta WHERE key = 'cmt_since'").fetchone() is not None
    assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_named_event_is_ingested_whatever_its_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(cache_dir=tmp_path)
    db = connect(":memory:")
    monkeypatch.setattr(source, "find_event", lambda config, event_id: listed(event_id, "2026-11-17T10:58:38+00:00"))
    monkeypatch.setattr(source, "download_results", fake_download(tmp_path))
    collect.collect(config, db, events=[EVENT])
    runs = db.execute("SELECT resolution, written FROM runs ORDER BY resolution").fetchall()
    assert [r["resolution"] for r in runs] == ["all_stations", "azimuth", "drop_outliers_dev"]
    assert {r["written"] for r in runs} == {"2026-11-17T10:58:20+00:00"}
    assert db.execute("SELECT count(*) FROM metrics WHERE metric = 'cmt.vr'").fetchone()[0] == 3
    # Unchanged: not downloaded again without --force.
    monkeypatch.setattr(source, "download_results", lambda *a: pytest.fail("downloaded an unchanged event"))
    collect.collect(config, db, events=[EVENT])


def test_rerun_forgets_the_old_rows_but_keeps_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(cache_dir=tmp_path)
    db = connect(":memory:")
    monkeypatch.setattr(source, "download_results", fake_download(tmp_path))
    collect.ingest(config, db, EVENT, listed(EVENT, "2026-11-17T10:58:38+00:00"))
    db.execute("INSERT INTO evaluations (check_id, event_id, failing) VALUES ('cmt.x', ?, 1)", (EVENT,))
    db.execute(
        "INSERT INTO findings (check_id, kind, severity, status, title) VALUES ('cmt.x', 'science', 'low', 'open', 't')"
    )
    set_metric(db, EVENT, "all_stations", "cmt.stale", 1.0)
    collect.ingest(config, db, EVENT, listed(EVENT, "2026-11-18T00:00:00+00:00"))
    assert db.execute("SELECT count(*) FROM metrics WHERE metric = 'cmt.stale'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM findings").fetchone()[0] == 1
    assert (
        db.execute("SELECT value FROM meta WHERE key = ?", (f"cmt_applied:{EVENT}",)).fetchone()[0]
        == "2026-11-18T00:00:00+00:00"
    )


def test_listing_row_parsing() -> None:
    event = source.parse_event(
        {
            "event_id": EVENT,
            "origin_time_iso": "2026-11-17T10:30:48.522607Z",
            "cmt_applied_at": "Tue, 17 Nov 2026 10:58:38 GMT",
            "magnitude": 4.79,
            "depth": 10.0,
            "locality": "15 km south-east of Arthur's Pass",
            "geonet_magnitude": 5.33,
        }
    )
    assert event.applied_at == "2026-11-17T10:58:38+00:00"
    assert event.origin_time == "2026-11-17T10:30:48+00:00"
    assert event.magnitude == 4.79 and event.depth_km == 10.0


def test_cmt_store_has_its_own_name_and_history(tmp_path: Path) -> None:
    db_path = tmp_path / "cmt.db"
    connect(db_path).close()
    calls: list[list[str]] = []

    def runner(args: Any) -> Any:
        calls.append(list(args))
        import subprocess

        return subprocess.CompletedProcess(args, 3 if args[1] == "lsjson" else 0, "", "directory not found")

    publish.push(
        Config(state_url="remote:/state"), db_path, datasette=False, runner=runner, store=publish.CMT_STORE_NAME
    )
    targets = [call[-1] for call in calls if call[1] == "copyto"]
    assert targets[0] == "remote:/state/cmt.db"
    assert targets[1].startswith("remote:/state/history/cmt-")


def test_event_row_keeps_a_listed_zero(tmp_path: Path) -> None:
    root = write_download(tmp_path / EVENT)
    zero = source.CmtEvent(event_id=EVENT, depth_km=0.0, magnitude=4.79)
    row = collect._event_row(EVENT, zero, root)
    assert row["depth_km"] == 0.0  # not cmt_solution.csv's 10.0
    assert collect._event_row(EVENT, None, root)["depth_km"] == 10.0
