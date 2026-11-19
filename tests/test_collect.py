"""Tests of the collector against a fake rclone remote (a local directory) and fake APIs."""

import datetime as dt
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
from pathlib import Path
from typing import Any

import pytest

from auto_scan import collect, registry
from auto_scan.config import Config
from auto_scan.sources import results

EVENT = "2026p866048"


def manifest(written: str = "2026-11-17T20:40:24+00:00", status: str = "succeeded") -> dict:
    return {
        "status": status,
        "event_id": EVENT,
        "defaults_version": "26.7.0.5Hz",
        "written": written,
        "sources": {
            "sw4": {"url": "https://github.com/ucgmsim/sw4.git", "ref": "single_precision", "commit": "1f75"},
            "nzcvm-data": {"url": "dropbox:/VM", "ref": "master", "models_sha256": "6374", "resources_sha256": "2df8"},
            "sites": {"url": "dropbox:/sites.parquet", "sha256": "1064"},
        },
    }


# An mtime well before the fixture's other files: the listing takes the latest manifest as current.
OLDER = dt.datetime(2026, 11, 1, tzinfo=dt.UTC).timestamp()

REALISATION = {
    "sources": {
        "source_geometries": {
            EVENT: {
                "type": "fault",
                "corners": [
                    {"latitude": -43.0, "longitude": 171.0, "depth": 9000.0},
                    {"latitude": -43.0, "longitude": 172.0, "depth": 9000.0},
                    {"latitude": -44.0, "longitude": 172.0, "depth": 11000.0},
                    {"latitude": -44.0, "longitude": 171.0, "depth": 11000.0},
                ],
            }
        }
    },
    "rupture_propagation": {"rupture_causality_tree": {EVENT: None}, "hypocentre": {"s": 0.5, "d": 0.5}},
    "magnitudes": {"magnitudes": {EVENT: 4.78730833064678}},
}

MOVIE = {"location": "15 km south-east of Arthur's Pass", "time": "2026-11-17T23:30:48.708000+13:00", "magnitude": 4.8}


def logs_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("log/job/12/sw4_0_5hz/01/job.out")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    return buffer.getvalue()


def write_run(remote: Path, event_dir: str, resolution: str, **kwargs: str) -> None:
    run = remote / event_dir / resolution
    (run / "simdb").mkdir(parents=True, exist_ok=True)
    (run / "manifest.json").write_text(json.dumps(manifest(**kwargs)))
    (run / "shake_movie.json").write_text(json.dumps(MOVIE))
    (run / "sw4_run.json").write_text("{}")
    (run / "logs.tar.gz").write_bytes(logs_tar())
    (run / f"simdb/{EVENT}.duckdb").write_bytes(b"big")
    (run / "intensity_measures.duckdb").write_bytes(b"duck")
    (run / f"simdb/{EVENT}_waveforms.nc").write_bytes(b"nc")
    for big in ("realisation.bb", "shake_movie.mp4", "intensity_measures.h5"):
        (run / big).write_bytes(b"big")


class FakeRclone:
    """rclone's lsjson and copy, over a local directory standing for the remote."""

    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.calls: list[list[str]] = []

    def path(self, url: str) -> Path:
        return self.remote / url.removeprefix("remote:").strip("/")

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[0] == "lsjson" and "/*/*/manifest.json" in args:
            root = self.path(args[-1])
            return json.dumps(
                [
                    {
                        "Path": str(p.relative_to(root)),
                        "ModTime": dt.datetime.fromtimestamp(p.stat().st_mtime, dt.UTC).isoformat(),
                    }
                    for p in sorted(root.glob("*/*/manifest.json"))
                ]
            )
        if args[0] == "lsjson":
            root = self.path(args[-1])
            files = sorted(p for p in root.rglob("*") if p.is_file())
            return json.dumps([{"Path": str(p.relative_to(root)), "Size": p.stat().st_size} for p in files])
        if args[0] == "copy":
            source, dest = self.path(args[1]), Path(args[2])
            patterns = [args[i + 1] for i, a in enumerate(args) if a == "--include"]
            regexes = [re.compile("^" + re.escape(p).replace(r"\*", "[^/]*") + "$") for p in patterns]
            for path in source.rglob("*"):
                relative = "/" + str(path.relative_to(source))
                if path.is_file() and any(r.match(relative) for r in regexes):
                    target = dest / relative[1:]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            return ""
        raise AssertionError(args)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    remote = tmp_path / "remote"
    event_dir = f"12-{EVENT}"
    for resolution in ("0_5hz", "1hz"):
        write_run(remote, event_dir, resolution)
    (remote / event_dir / "realisation.json").write_text(json.dumps(REALISATION))
    (remote / event_dir / "realisation.srf").write_text("srf")
    (remote / f"11-{EVENT}" / "0_5hz").mkdir(parents=True)  # an older cycle of the same event
    (remote / f"11-{EVENT}" / "0_5hz" / "manifest.json").write_text("{}")
    os.utime(remote / f"11-{EVENT}" / "0_5hz" / "manifest.json", (OLDER, OLDER))
    (remote / "3-2026p824644" / "0_5hz").mkdir(parents=True)  # no manifest yet: ignored
    (remote / "builds").mkdir()
    rclone = FakeRclone(remote)
    monkeypatch.setattr(results, "run_rclone", rclone)

    state: dict[str, Any] = {
        "flatfiles": True,
        "station_ims": {"event_id": EVENT, "sites": []},
        "jobs": [],
        "summary_calls": 0,
    }

    def flatfiles(config, event_id, dest):
        if not state["flatfiles"]:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "earthquake_source_table.csv").write_text(f"evid,datetime\n{event_id},2026-11-17T10:30:48.5Z\n")
        return True

    def summary(config, event_id):
        state["summary_calls"] += 1

    monkeypatch.setattr(collect.seismicnow, "download_flatfiles", flatfiles)
    monkeypatch.setattr(collect.simdb, "station_ims", lambda config, event_id: state["station_ims"])
    monkeypatch.setattr(collect.simdb, "event_summary", summary)
    monkeypatch.setattr(collect.monitor, "fetch_jobs", lambda config: state["jobs"])
    monkeypatch.setattr(collect.registry, "load_plugins", lambda: None)
    monkeypatch.delenv("AUTO_SCAN_KEEP_CACHE", raising=False)

    calls: list[tuple] = []

    def run_extractor(db, event_id, resolution, run_dir):
        calls.append(("run", event_id, resolution, (run_dir / "intensity_measures.duckdb").exists()))

    def event_extractor(db, event_id, event_dir):
        calls.append(("event", event_id, (event_dir / "observed").is_dir()))

    def broken(db, event_id, resolution, run_dir):
        raise ValueError("bad file")

    monkeypatch.setattr(registry, "EXTRACTORS", [broken, run_extractor])
    monkeypatch.setattr(registry, "EVENT_EXTRACTORS", [event_extractor])
    config = Config(results_url="remote:", cache_dir=tmp_path / "cache", workflow="realtime/run1")
    return config, remote, rclone, state, calls


def table(db: sqlite3.Connection, name: str) -> list[dict]:
    return [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY 1, 2, 3")]


def test_first_collect(setup, db: sqlite3.Connection, capsys) -> None:
    config, _remote, _rclone, state, calls = setup
    state["jobs"] = [{"workflow": "realtime/run1", "cycle": "12", "name": "sw4_0_5hz", "submit_num": 1, "cores": 96}]
    collect.collect(config, db)

    events = table(db, "events")
    assert len(events) == 1
    event = events[0]
    assert event["event_dir"] == f"12-{EVENT}" and event["cycle"] == 12
    assert event["origin_time"] == "2026-11-17T10:30:48.708000+00:00"
    assert event["magnitude"] == pytest.approx(4.7873, abs=1e-4)
    assert event["latitude"] == pytest.approx(-43.5) and event["longitude"] == pytest.approx(171.5)
    assert event["depth_km"] == pytest.approx(10.0)
    assert event["location"] == MOVIE["location"]
    assert state["summary_calls"] == 0  # nothing missing, so SimDB's summary is not asked

    runs = table(db, "runs")
    assert [r["resolution"] for r in runs] == ["0_5hz", "1hz"]
    assert runs[0]["status"] == "succeeded" and runs[0]["written"] == "2026-11-17T20:40:24+00:00"
    assert len(runs[0]["manifest_sha256"]) == 64
    sources = {(r["resolution"], r["source"]): r["commit_sha"] for r in table(db, "run_sources")}
    assert sources[("0_5hz", "sw4")] == "1f75"
    assert sources[("0_5hz", "nzcvm-data")] == "6374"
    assert sources[("1hz", "sites")] == "1064"
    assert table(db, "jobs")[0]["cores"] == 96

    # Extractors ran on each run with the duckdb present, then the event one with observed/.
    assert calls == [("run", EVENT, "0_5hz", True), ("run", EVENT, "1hz", True), ("event", EVENT, True)]
    err = capsys.readouterr().err
    assert "bad file" in err and "Traceback" in err
    assert err.count(f"12-{EVENT} ") == 2  # one line per run

    cache = config.event_cache(f"12-{EVENT}")
    assert (cache / "realisation.json").is_file()
    assert (cache / "0_5hz/logs/log/job/12/sw4_0_5hz/01/job.out").read_text() == "ok"
    assert (cache / "1hz/simdb" / f"{EVENT}_waveforms.nc").is_file()
    assert not list(cache.rglob("*.duckdb"))  # deleted after the extractors
    assert json.loads((cache / "simdb_station_ims.json").read_text())["event_id"] == EVENT
    for big in ("realisation.srf", "0_5hz/realisation.bb", "0_5hz/shake_movie.mp4", "0_5hz/intensity_measures.h5"):
        assert not (cache / big).exists()
    assert not (cache / "0_5hz/intensity_measures.duckdb").exists()
    assert not config.event_cache(f"11-{EVENT}").exists()
    assert not config.event_cache("3-2026p824644").exists()
    observed = json.loads(db.execute("SELECT value FROM meta WHERE key = ?", (f"observed:{EVENT}",)).fetchone()[0])
    assert observed["flatfiles"] and observed["station_ims"]


def test_unchanged_is_skipped_and_force_rescans(setup, db: sqlite3.Connection) -> None:
    config, _remote, rclone, _state, calls = setup
    collect.collect(config, db)
    calls.clear()
    rclone.calls.clear()
    collect.collect(config, db)
    assert calls == []
    assert not any(c[0] == "copy" and "/0_5hz/logs.tar.gz" in c for c in rclone.calls)

    collect.collect(config, db, event=EVENT, force=True)
    assert [c[0] for c in calls] == ["run", "run", "event"]


def test_changed_manifest_rescans(setup, db: sqlite3.Connection) -> None:
    config, remote, _rclone, _state, calls = setup
    collect.collect(config, db)
    calls.clear()
    write_run(remote, f"12-{EVENT}", "1hz", written="2026-11-18T02:00:00+00:00", status="failed:sw4_1hz")
    collect.collect(config, db)
    assert [c[0] for c in calls] == ["run", "run", "event"]
    run = db.execute("SELECT * FROM runs WHERE resolution = '1hz'").fetchone()
    assert run["status"] == "failed:sw4_1hz" and run["written"] == "2026-11-18T02:00:00+00:00"


def test_keep_cache(setup, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    config, *_ = setup
    monkeypatch.setenv("AUTO_SCAN_KEEP_CACHE", "1")
    collect.collect(config, db)
    assert len(list(config.cache_dir.rglob("*.duckdb"))) == 2


def test_jobs_kept_when_monitor_returns_nothing(setup, db: sqlite3.Connection) -> None:
    config, _remote, _rclone, state, _calls = setup
    state["jobs"] = [{"workflow": "realtime/run1", "cycle": "12", "name": "a", "submit_num": 1}]
    collect.collect(config, db)
    state["jobs"] = []
    collect.collect(config, db)
    assert len(table(db, "jobs")) == 1
    state["jobs"] = [{"workflow": "realtime/run1", "cycle": "13", "name": "b", "submit_num": 1}]
    collect.collect(config, db)
    assert [r["name"] for r in table(db, "jobs")] == ["b"]


def test_late_observations_trigger_rescan(setup, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _remote, _rclone, state, calls = setup
    # The fixture event is "recent" relative to a pinned clock.
    import datetime as dt

    real = dt.datetime

    class Clock(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 11, 20, tzinfo=tz)

    monkeypatch.setattr(collect.dt, "datetime", Clock)
    state["flatfiles"] = False
    state["station_ims"] = None
    collect.collect(config, db)
    assert calls[-1] == ("event", EVENT, False)
    meta = json.loads(db.execute("SELECT value FROM meta WHERE key = ?", (f"observed:{EVENT}",)).fetchone()[0])
    assert meta["flatfiles"] is False and meta["station_ims"] is False

    calls.clear()
    collect.collect(config, db)  # still nothing: probed, not rescanned
    assert calls == []

    state["flatfiles"] = True
    collect.collect(config, db)
    assert [c[0] for c in calls] == ["run", "run", "event"]
    assert calls[-1] == ("event", EVENT, True)

    calls.clear()
    state["station_ims"] = {"event_id": EVENT, "sites": []}
    collect.collect(config, db)
    assert [c[0] for c in calls] == ["run", "run", "event"]
    calls.clear()
    collect.collect(config, db)
    assert calls == []


def test_old_events_are_not_retried(setup, db: sqlite3.Connection) -> None:
    config, _remote, _rclone, state, calls = setup
    state["flatfiles"] = False
    collect.collect(config, db)
    db.execute("UPDATE events SET origin_time = '2020-01-01T00:00:00+00:00'")
    db.execute("UPDATE runs SET written = '2020-01-02T00:00:00+00:00'")
    db.commit()
    state["flatfiles"] = True
    calls.clear()
    collect.collect(config, db)
    assert calls == []


def test_backlog_event_is_retried(setup, db: sqlite3.Connection) -> None:
    """An old earthquake simulated recently still waits for SimDB and SeismicNow."""
    config, _remote, _rclone, state, calls = setup
    state["flatfiles"] = False
    collect.collect(config, db)
    db.execute("UPDATE events SET origin_time = '2020-01-01T00:00:00+00:00'")
    db.execute("UPDATE runs SET written = ?", (dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),))
    db.commit()
    state["flatfiles"] = True
    calls.clear()
    collect.collect(config, db)
    assert calls  # the extractors ran again: the event was rescanned


def test_rerun_in_a_lower_cycle_replaces_the_event(setup, db: sqlite3.Connection, capsys) -> None:
    """The latest manifest decides, not the cycle number, and nothing of the old simulation is left."""
    config, remote, _rclone, _state, _calls = setup
    collect.collect(config, db)
    db.execute("INSERT INTO metrics VALUES (?, '1hz', 'perf.left_over', 1.0, NULL, NULL)", (EVENT,))
    db.execute(
        "INSERT INTO evaluations VALUES ('perf.x', '', ?, '1hz', 1, NULL, NULL, 'old', '2026-11-18T00:00:00+00:00')",
        (EVENT,),
    )
    db.execute(
        "INSERT INTO findings (check_id, subject, kind, severity, status, title, first_event, last_event)"
        " VALUES ('perf.x', '', 'perf', 'low', 'filed', 't', ?, ?)",
        (EVENT, EVENT),
    )
    db.commit()
    # The workflow was reinstalled (cycles restart) and the event run again, at one resolution.
    write_run(remote, f"2-{EVENT}", "0_5hz")
    newer = dt.datetime.now(dt.UTC).timestamp() + 3600
    os.utime(remote / f"2-{EVENT}" / "0_5hz" / "manifest.json", (newer, newer))
    capsys.readouterr()
    collect.collect(config, db)

    assert f"2-{EVENT}: replaces 12-{EVENT}" in capsys.readouterr().err
    (event,) = table(db, "events")
    assert event["event_dir"] == f"2-{EVENT}" and event["cycle"] == 2
    assert [r["resolution"] for r in table(db, "runs")] == ["0_5hz"]
    assert {r["resolution"] for r in table(db, "run_sources")} == {"0_5hz"}
    assert not db.execute("SELECT 1 FROM metrics WHERE metric = 'perf.left_over'").fetchall()
    assert table(db, "evaluations") == []
    assert len(table(db, "findings")) == 1  # the finding outlives the simulation it was found on
    superseded = db.execute("SELECT value FROM meta WHERE key = ?", (f"superseded:{EVENT}",)).fetchone()
    assert json.loads(superseded["value"]) == [f"11-{EVENT}", f"12-{EVENT}"]


def test_one_event_failing_does_not_stop_others(setup, db: sqlite3.Connection, capsys) -> None:
    config, remote, _rclone, _state, _calls = setup
    other = remote / "13-2026p900000" / "0_5hz"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text("{not json")
    collect.collect(config, db)
    assert {r["event_id"] for r in table(db, "events")} == {EVENT, "2026p900000"}
    assert db.execute("SELECT status FROM runs WHERE event_id = '2026p900000'").fetchone()[0] is None


def test_event_row_falls_back_to_simdb(tmp_path: Path) -> None:
    event = results.EventDir(f"12-{EVENT}", 12, EVENT)
    summary = {"magnitude": 4.79, "hypo_lat": -43.03, "hypo_lon": 171.75, "hypo_depth": 10.0}
    row = collect.event_row(Config(), event, tmp_path, ["0_5hz"], summary_fetcher=lambda c, e: summary)
    assert (row["magnitude"], row["latitude"], row["longitude"], row["depth_km"]) == (4.79, -43.03, 171.75, 10.0)
    assert row["origin_time"] is None


def test_multi_segment_magnitude() -> None:
    mw = collect.realisation_magnitude({"magnitudes": {"magnitudes": {"a": 7.0, "b": 7.0}}})
    assert mw == pytest.approx(7.0 + 2 / 3 * 0.30103, abs=1e-6)


def test_source_rows_hash_fallback() -> None:
    rows = collect.source_rows(manifest())
    assert ("nzcvm-data", "dropbox:/VM", "master", "6374") in rows
    assert ("sites", "dropbox:/sites.parquet", None, "1064") in rows
