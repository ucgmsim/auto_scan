"""Tests of the Datasette side (server/): the upload's merge, the views and the dashboard's queries.

Datasette isn't a dependency, so the plugin is imported from its file and only its pure
functions are used. The dashboard test needs PyYAML and is skipped without it
(``uv run --with pyyaml pytest tests/test_server_plugin.py`` runs it).
"""

import gzip
import importlib.util
import json
import re
import sqlite3
import statistics
import sys
from pathlib import Path
from types import ModuleType

import pytest

from auto_scan.db import connect, set_metric

SERVER = Path(__file__).resolve().parent.parent / "server"


def load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_upload", SERVER / "plugins" / "scan_upload.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_upload"] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


def make_snapshot(path: Path, n_events: int = 6) -> None:
    """A small scan.db with a row in every table the views read."""
    db = connect(path)
    for i in range(n_events):
        event = f"2026p{i:03d}"
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, -43.5, 172.6, 10.0, 'near Christchurch')",
            (event, i + 1, f"{i + 1}-{event}", f"2026-11-{i + 1:02d}T00:00:00+00:00", 4.5 + i / 10),
        )
        for resolution in ("0_25hz", "1hz"):
            db.execute(
                "INSERT INTO runs VALUES (?, ?, 'succeeded', ?, 'v1', 'abc', ?)",
                (event, resolution, f"2026-11-{i + 1:02d}T05:00:00+00:00", f"2026-11-{i + 1:02d}T06:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO run_sources VALUES (?, ?, 'sw4', 'https://github.com/ucgmsim/sw4.git', 'main', ?)",
                (event, resolution, f"sha{i}"),
            )
            for im in ("PGA", "PGV", "pSA_1.0"):
                set_metric(db, event, resolution, f"science.residual.median.{im}", 0.1 * i)
                set_metric(db, event, resolution, f"science.residual.mean.{im}", 0.1 * i + 0.01)
                set_metric(db, event, resolution, f"science.residual.mad_sigma.{im}", 0.6)
                set_metric(db, event, resolution, f"science.residual.n.{im}", 20)
                for s in range(4):
                    db.execute(
                        "INSERT INTO station_residuals VALUES (?, ?, ?, ?, 1.0, 1.0, NULL, ?, 30.0, 400.0, 1)",
                        (event, resolution, f"ST{s}", im, 0.1 * s + 0.05 * i),
                    )
            set_metric(db, event, resolution, "science.residual.band_median.lf", -0.2)
            set_metric(db, event, resolution, "science.residual.n_stations", 25)
            set_metric(db, event, resolution, "perf.updates_per_core_s", 2.0e6 - i * 1e4)
            set_metric(db, event, resolution, "perf.cores", 1024)
            set_metric(db, event, resolution, "perf.actual_core_hours", 100.0 + i)
            set_metric(db, event, resolution, "perf.estimated_core_hours_effective", 150.0)
            set_metric(db, event, resolution, "perf.cost_ratio", 150.0 / (100.0 + i))
            set_metric(db, event, resolution, "perf.mpi_fraction", 0.2)
            set_metric(db, event, resolution, "ops.time_to_results_from_origin_s", 5 * 3600.0)
            db.execute(
                "INSERT INTO sw4_timers (event_id, resolution, region, depth, calls_total, ranks, min_s, avg_s, max_s)"
                " VALUES (?, ?, 'solve', 0, 1, 64, 90, 100, 110), (?, ?, 'solve/time_stepping', 1, 1, 64, 80, 90, 99)",
                (event, resolution, event, resolution),
            )
        set_metric(db, event, "", "science.xres.0_25hz-1hz.median", 0.05)
        set_metric(db, event, "", "science.xres.0_25hz-1hz.mad_sigma", 0.1)
        set_metric(db, event, "", "science.xres.0_25hz-1hz.n", 300)
        set_metric(db, event, "", "ops.core_hours", 250.0)
        db.execute(
            "INSERT INTO evaluations VALUES ('perf.speed', '1hz', ?, '1hz', ?, 1.0, 1.5, 's', '2026-11-19')",
            (event, int(i == n_events - 1)),
        )
    db.execute(
        "INSERT INTO jobs (workflow, cycle, name, submit_num, state, time_run_exit, job_err) VALUES"
        " ('realtime/run1', '3', 'sw4_1hz', 1, 'failed', '2026-11-03T03:00:00Z', 'slurmstepd: error: *** JOB 1 DUE TO TIME LIMIT'),"
        " ('realtime/run1', '4', 'upload_1hz', 1, 'failed', '2026-11-04T03:00:00Z', 'Traceback (most recent call last)'),"
        " ('realtime/run1', '4', 'sw4_1hz', 1, 'succeeded', '2026-11-04T02:00:00Z', NULL)"
    )
    db.execute(
        "INSERT INTO findings (check_id, subject, kind, severity, status, title, first_event, last_event, n_failing,"
        " opened_at, updated_at, issue_url) VALUES"
        " ('perf.speed', '1hz', 'perf', 'high', 'open', 'SW4 slowed', '2026p005', '2026p005', 1, '2026-11-19', '2026-11-19', NULL),"
        " ('science.bias', 'PGV', 'science', 'medium', 'verified', 'PGV bias', '2026p001', '2026p002', 2, '2026-11-02',"
        " '2026-11-10', 'https://github.com/ucgmsim/sw4/issues/1')"
    )
    db.commit()
    db.close()


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "upload.db"
    make_snapshot(path)
    return path


@pytest.fixture
def target(tmp_path: Path) -> sqlite3.Connection:
    """The server's scan database, holding an older upload's leftovers."""
    conn = sqlite3.connect(tmp_path / "scan.db")
    conn.execute("CREATE TABLE stale (x)")
    conn.execute("CREATE TABLE events (event_id TEXT)")
    conn.execute("INSERT INTO events VALUES ('old')")
    conn.execute("CREATE VIEW stale_view AS SELECT * FROM stale")
    conn.commit()
    plugin.add_functions(conn)
    return conn


def names(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def test_merge_replaces_tables_and_makes_views(target: sqlite3.Connection, snapshot: Path) -> None:
    counts = plugin.merge(target, str(snapshot))
    assert counts["events"] == 6 and counts["runs"] == 12 and counts["findings"] == 2
    assert "stale" not in names(target, "table") and "stale_view" not in names(target, "view")
    assert {"findings_open", "event_summary", "perf_by_run", "station_bias", "timers_top"} <= names(target, "view")
    assert "findings_key" in names(target, "index")
    assert target.execute("SELECT count(*) FROM events WHERE event_id = 'old'").fetchone()[0] == 0
    assert not target.in_transaction
    assert target.execute("PRAGMA database_list").fetchall()[-1][1] == "main"  # snapshot detached


def test_merge_is_idempotent(target: sqlite3.Connection, snapshot: Path) -> None:
    first = plugin.merge(target, str(snapshot))
    assert plugin.merge(target, str(snapshot)) == first


def test_every_view_runs(target: sqlite3.Connection, snapshot: Path) -> None:
    plugin.merge(target, str(snapshot))
    for view in sorted(names(target, "view")):
        target.execute(f"SELECT * FROM {view}").fetchall()


def test_view_values(target: sqlite3.Connection, snapshot: Path) -> None:
    plugin.merge(target, str(snapshot))
    target.row_factory = sqlite3.Row
    row = target.execute("SELECT * FROM event_summary WHERE event_id = '2026p003'").fetchone()
    assert row["status_1hz"] == "succeeded" and row["status_0_5hz"] is None
    assert row["event_term_pgv_1hz"] == pytest.approx(0.3)
    assert row["event_term_lf_1hz"] == pytest.approx(-0.2)
    assert row["xres_0_25hz_1hz"] == pytest.approx(0.05)
    assert row["hours_to_results_1hz"] == pytest.approx(5.0)
    assert row["n_checks"] == 1 and row["n_failing_checks"] == 0

    term = target.execute(
        "SELECT * FROM science_event_terms WHERE event_id = '2026p002' AND resolution = '1hz' AND im = 'pSA_1.0'"
    ).fetchone()
    assert term["event_term"] == pytest.approx(0.2)
    assert term["mean"] == pytest.approx(0.21) and term["mad_sigma"] == pytest.approx(0.6)
    assert term["n_stations"] == 20

    xres = target.execute("SELECT * FROM xres_by_event WHERE event_id = '2026p000'").fetchone()
    assert (xres["pair"], xres["median_mismatch"], xres["mad_sigma"], xres["n_sites"]) == ("0_25hz-1hz", 0.05, 0.1, 300)

    perf = target.execute("SELECT * FROM perf_by_run WHERE event_id = '2026p001' AND resolution = '1hz'").fetchone()
    assert perf["updates_per_core_s"] == pytest.approx(1.99e6)
    assert perf["cores"] == 1024 and perf["actual_core_hours"] == pytest.approx(101.0)
    assert perf["hours_to_results"] == pytest.approx(5.0)

    bias = target.execute(
        "SELECT * FROM station_bias WHERE station = 'ST3' AND resolution = '1hz' AND im = 'PGA'"
    ).fetchone()
    # Each event's residuals are 0.05 i + (0, .1, .2, .3): ST3's site term is 0.3 - 0.15.
    assert bias["n_events"] == 6 and bias["site_term"] == pytest.approx(0.15)
    assert bias["site_sigma"] == pytest.approx(0.0, abs=1e-9)

    by_event = target.execute(
        "SELECT * FROM residuals_by_event WHERE event_id = '2026p000' AND resolution = '1hz' AND im = 'PGA'"
    ).fetchone()
    assert by_event["event_term"] == pytest.approx(0.15)
    assert by_event["within_sigma"] == pytest.approx(statistics.stdev([0, 0.1, 0.2, 0.3]), abs=1e-4)

    classes = dict(target.execute("SELECT name, failure_class FROM job_failures").fetchall())
    assert classes == {"sw4_1hz": "time limit", "upload_1hz": "Python exception"}

    timers = target.execute("SELECT region, rank FROM timers_top WHERE event_id = '2026p000' AND resolution = '1hz'")
    assert dict(timers.fetchall()) == {"solve": 1, "solve/time_stepping": 2}

    open_ = target.execute("SELECT id, status FROM findings_open").fetchall()
    assert [tuple(r) for r in open_] == [(1, "open")]


def test_merge_rejects_unknown_schema_version(target: sqlite3.Connection, snapshot: Path) -> None:
    conn = sqlite3.connect(snapshot)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema_version"):
        plugin.merge(target, str(snapshot))
    assert "stale" in names(target, "table")  # untouched
    assert not target.in_transaction


def test_merge_rejects_other_databases(target: sqlite3.Connection, tmp_path: Path) -> None:
    cylc = tmp_path / "cylc.db"
    conn = sqlite3.connect(cylc)
    conn.execute("CREATE TABLE task_jobs (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="not a scan.db"):
        plugin.merge(target, str(cylc))

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        plugin.merge(target, str(junk))
    assert "stale" in names(target, "table")


def test_merge_rolls_back_a_failing_view(target: sqlite3.Connection, snapshot: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        plugin.merge(target, str(snapshot), views_sql="CREATE VIEW broken AS SELEC 1;\n")
    assert "stale" in names(target, "table")
    assert target.execute("SELECT event_id FROM events").fetchall() == [("old",)]


def test_views_noop_without_tables(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "empty.db")
    plugin.views(conn)
    assert names(conn, "view") == set()


def test_views_remade(target: sqlite3.Connection, snapshot: Path) -> None:
    plugin.merge(target, str(snapshot))
    target.execute("DROP VIEW event_summary")
    plugin.views(target)
    assert "event_summary" in names(target, "view")


def test_gunzip_limited() -> None:
    raw = b"SQLite format 3\x00" + b"x" * 1000
    assert plugin.gunzip_limited(gzip.compress(raw)) == raw
    assert plugin.gunzip_limited(raw) == raw
    with pytest.raises(ValueError, match="too big"):
        plugin.gunzip_limited(gzip.compress(b"\x00" * 5_000_000), limit=1_000_000)
    with pytest.raises(ValueError, match="too big"):
        plugin.gunzip_limited(raw, limit=10)
    with pytest.raises(ValueError, match="gzip"):
        plugin.gunzip_limited(b"\x1f\x8b" + b"garbage")


def test_stdev_aggregate() -> None:
    conn = sqlite3.connect(":memory:")
    plugin.add_functions(conn)
    conn.execute("CREATE TABLE t (x)")
    values = [0.3, -1.2, 2.5, 0.0, 0.7]
    conn.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values] + [(None,), ("text",)])
    assert conn.execute("SELECT stdev(x) FROM t").fetchone()[0] == pytest.approx(statistics.stdev(values))
    assert conn.execute("SELECT stdev(x) FROM t WHERE x = 0.3").fetchone()[0] is None


def test_statements_split() -> None:
    sql = "-- a comment;\n-- only\nCREATE VIEW a AS SELECT 1;\n\nCREATE VIEW b AS\nSELECT 2;\n"
    parts = plugin.statements(sql)
    assert len(parts) == 2
    assert "VIEW a AS" in parts[0] and "VIEW b AS" in parts[1]


def dashboard_queries() -> list[tuple[str, str]]:
    yaml = pytest.importorskip("yaml")
    config = yaml.safe_load((SERVER / "scan_dashboard.yaml").read_text())
    dashboard = config["scan"]
    queries = [(f"filter {name}", spec["query"]) for name, spec in dashboard["filters"].items()]
    queries += [(f"chart {name}", spec["query"]) for name, spec in dashboard["charts"].items()]
    charts = set(dashboard["charts"])
    laid_out = {cell for row in dashboard["layout"] for cell in row}
    assert laid_out == charts, "every chart in the layout, and nothing else"
    assert len({len(row) for row in dashboard["layout"]}) == 1, "layout rows of equal width"
    for spec in dashboard["charts"].values():
        assert spec["db"] == "scan" and spec["library"] in {"metric", "table", "vega-lite"}
    return queries


def fill(query: str, options: dict[str, str]) -> str:
    """datasette-dashboards' replacement: keep a [[ ... :name ... ]] block when the filter is set."""

    def keep(match: re.Match[str]) -> str:
        variable = re.search(r":(\w+)", match.group(0))
        return match.group(0)[2:-2] if variable and options.get(variable.group(1)) else ""

    return re.sub(r"\[\[[^\]]*\]\]", keep, query)


@pytest.mark.parametrize("options", [{}, {"resolution": "1hz"}, {"resolution": "1hz", "im": "PGV"}])
def test_dashboard_queries_run(target: sqlite3.Connection, snapshot: Path, options: dict[str, str]) -> None:
    queries = dashboard_queries()
    plugin.merge(target, str(snapshot))
    for name, query in queries:
        rows = target.execute(fill(query, options), options).fetchall()
        assert rows is not None, name
    assert json.dumps(options) is not None


def test_dashboard_has_data(target: sqlite3.Connection, snapshot: Path) -> None:
    queries = dict(dashboard_queries())
    plugin.merge(target, str(snapshot))
    options = {"resolution": "1hz"}
    for name in ("chart event-terms", "chart xres", "chart speed", "chart cost", "chart mpi", "chart time-to-results"):
        assert target.execute(fill(queries[name], options), options).fetchall(), f"{name} is empty"


def make_cmt_snapshot(path: Path, tmp_path: Path) -> None:
    """A cmt.db with two events extracted from synthetic downloads, checked."""
    from test_cmt import EVENT, write_download

    from auto_scan import engine, registry
    from auto_scan.cmt import extract

    db = connect(path)
    for day, event in enumerate([EVENT, "2026p866049"], start=1):
        root = write_download(tmp_path / event)
        db.execute(
            "INSERT INTO events (event_id, event_dir, origin_time, magnitude) VALUES (?, ?, ?, 4.8)",
            (event, event, f"2026-11-1{day}T10:30:48+00:00"),
        )
        for run in ("all_stations", "azimuth", "drop_outliers_dev"):
            db.execute(
                "INSERT INTO runs (event_id, resolution, status, written) VALUES (?, ?, 'ok', ?)",
                (event, run, f"2026-11-1{day}T10:58:20+00:00"),
            )
        extract.extract_cmt(db, event, root)
    registry.load_cmt_plugins()
    engine.run_checks(db, checks=registry.CMT_CHECKS)
    db.commit()
    db.close()


def cmt_dashboard() -> dict:
    yaml = pytest.importorskip("yaml")
    dashboard = yaml.safe_load((SERVER / "cmt_dashboard.yaml").read_text())["cmt"]
    assert {cell for row in dashboard["layout"] for cell in row} == set(dashboard["charts"])
    assert len({len(row) for row in dashboard["layout"]}) == 1
    assert all(spec["db"] == "cmt" for spec in [*dashboard["charts"].values(), *dashboard["filters"].values()])
    return dashboard


@pytest.mark.parametrize("options", [{}, {"run": "all_stations"}])
def test_cmt_views_and_dashboard(tmp_path: Path, options: dict[str, str]) -> None:
    snapshot = tmp_path / "upload.db"
    make_cmt_snapshot(snapshot, tmp_path)
    target = sqlite3.connect(tmp_path / "cmt.db")
    plugin.add_functions(target)
    counts = plugin.merge(target, str(snapshot), database="cmt")
    assert counts["events"] == 2 and counts["runs"] == 6
    assert {"findings_open", "cmt_by_run", "cmt_event_summary", "cmt_timings"} <= names(target, "view")
    assert "event_summary" not in names(target, "view")  # the simulation views are not made here
    target.row_factory = sqlite3.Row
    row = target.execute("SELECT * FROM cmt_event_summary WHERE event_id = '2026p866048'").fetchone()
    assert row["selected_run"] == "all_stations" and row["geonet_magnitude"] == pytest.approx(5.33)
    assert row["n_stations"] == 4 and row["n_runs_ok"] == 3 and row["minutes_to_solution"] == pytest.approx(27.5)
    run = target.execute(
        "SELECT * FROM cmt_by_run WHERE event_id = '2026p866048' AND run = 'drop_outliers_dev'"
    ).fetchone()
    assert run["deviatoric"] == 1 and run["iso_percent"] == pytest.approx(0.0)
    stages = {r["stage"] for r in target.execute("SELECT stage FROM cmt_timings WHERE run = 'all_stations'")}
    assert stages == {"mouse_detection", "process_data", "other"}
    for view in sorted(names(target, "view")):
        target.execute(f"SELECT * FROM {view}").fetchall()
    dashboard = cmt_dashboard()
    queries = [spec["query"] for spec in [*dashboard["filters"].values(), *dashboard["charts"].values()]]
    for query in queries:
        target.execute(fill(query, options), options).fetchall()
    stages_query = fill(dashboard["charts"]["stages"]["query"], options)
    assert target.execute(stages_query, options).fetchall(), "the stages chart is empty"
    plugin.views(target, database="cmt")  # startup remakes them
