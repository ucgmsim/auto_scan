"""Tests of the sources: rclone, the monitor, SimDB and SeismicNow, all faked."""

import email.message
import http.server
import io
import json
import tarfile
import threading
import urllib.error
import zipfile
from pathlib import Path

import pytest

from auto_scan.config import Config
from auto_scan.sources import _http, monitor, results, seismicnow, simdb


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_http, "RETRY_DELAY_S", 0.0)


class FakeResponse(io.BytesIO):
    """What urlopen returns, enough for _http.get."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status


def http_error(url: str, code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", email.message.Message(), io.BytesIO(body))


def fake_urlopen(monkeypatch: pytest.MonkeyPatch, answers: list) -> list:
    """Make urlopen answer from ``answers`` in turn (bytes, or an exception); return the requests."""
    requests = []

    def urlopen(request, timeout=None):
        requests.append(request)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return requests


# --- results (rclone) ---------------------------------------------------------------


def test_parse_event_dir() -> None:
    assert results.parse_event_dir("12-2026p866048/") == results.EventDir("12-2026p866048", 12, "2026p866048")
    for name in ("builds/", "sites.parquet", "20261002T0000+1300-2026p728213", "1-2026p1-draft-300ch", "x-2026p1"):
        assert results.parse_event_dir(name) is None


def test_list_events_keeps_latest_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Times as rclone prints them: in the local zone, which changes with daylight saving.
    manifests = {
        "1-2026p660242/0_5hz/manifest.json": "2026-10-17T21:32:14+13:00",  # an old earthquake, run late
        "2-2026p764940/0_5hz/manifest.json": "2026-10-11T16:57:50+13:00",
        "11-2026p866048/1hz/manifest.json": "2026-11-18T03:05:57+13:00",
        "12-2026p866048/0_5hz/manifest.json": "2026-11-18T09:40:32+13:00",
        "12-2026p866048/1hz/manifest.json": "2026-11-18T15:03:35+13:00",
        # Cycles restart when the workflow is reinstalled: the re-run has the lower number.
        "9-2026p778829/0_5hz/manifest.json": "2026-09-20T10:00:00+12:00",
        "4-2026p778829/0_5hz/manifest.json": "2026-11-03T20:21:23+13:00",
        "20261002T0000+1300-2026p728213/1hz/manifest.json": "2026-10-02T10:00:00+13:00",
    }
    calls = []

    def run(args: list[str]) -> str:
        calls.append(args)
        return json.dumps([{"Path": path, "ModTime": modified} for path, modified in manifests.items()])

    monkeypatch.setattr(results, "run_rclone", run)
    events = results.list_events(Config(results_url="remote:/r"))
    assert [e.name for e in events] == ["2-2026p764940", "1-2026p660242", "4-2026p778829", "12-2026p866048"]
    assert [e.superseded for e in events] == [(), (), ("9-2026p778829",), ("11-2026p866048",)]
    assert events[-1].written == "2026-11-18T02:03:35+00:00"
    assert calls == [["lsjson", "-R", "--files-only", "--include", "/*/*/manifest.json", "remote:/r"]]


def test_list_files_and_run_files(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = [
        {"Path": "0_5hz", "IsDir": True},
        {"Path": "0_5hz/manifest.json", "Size": 10, "IsDir": False},
        {"Path": "0_5hz/intensity_measures.duckdb", "Size": 10, "IsDir": False},
        {"Path": "realisation.json", "Size": 5, "IsDir": False},
    ]
    calls = []

    def run(args: list[str]) -> str:
        calls.append(args)
        return json.dumps(listing)

    monkeypatch.setattr(results, "run_rclone", run)
    files = results.list_files(Config(results_url="remote:/r"), "1-2026p1")
    assert calls == [["lsjson", "-R", "--files-only", "remote:/r/1-2026p1"]]
    assert [f["Path"] for f in files] == ["0_5hz/manifest.json", "0_5hz/intensity_measures.duckdb", "realisation.json"]
    assert [f["Path"] for f in results.run_files(files, "0_5hz")] == ["manifest.json", "intensity_measures.duckdb"]


def test_copy_uses_one_call_with_includes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(results, "run_rclone", lambda args: calls.append(args) or "")
    config = Config(results_url="remote:/r", cache_dir=tmp_path)
    results.copy(config, "1-2026p1", results.run_includes(["0_5hz", "1hz"]))
    assert len(calls) == 1
    args = calls[0]
    assert args[:3] == ["copy", "remote:/r/1-2026p1", str(tmp_path / "1-2026p1")]
    includes = args[4::2]
    assert "/realisation.json" in includes
    assert "/1hz/intensity_measures.duckdb" in includes and "/0_5hz/logs.tar.gz" in includes
    # Nothing big: the patterns can't match .bb/.hf/.lf/.mp4/.h5 or intensity_measures.duckdb.
    assert not any(p.endswith((".bb", ".hf", ".lf", ".mp4", ".h5", ".srf")) for p in includes)
    assert "/0_5hz/*.duckdb" not in includes


def test_unpack_logs_is_safe(tmp_path: Path) -> None:
    run_dir = tmp_path / "0_5hz"
    run_dir.mkdir()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"hello"
        info = tarfile.TarInfo("log/job/1/sw4_0_5hz/01/job.out")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("log/job/1/sw4_0_5hz/NN")
        link.type = tarfile.SYMTYPE
        link.linkname = "01"
        tar.addfile(link)
    (run_dir / "logs.tar.gz").write_bytes(buffer.getvalue())
    assert results.unpack_logs(run_dir)
    assert (run_dir / "logs/log/job/1/sw4_0_5hz/01/job.out").read_text() == "hello"

    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (run_dir / "logs.tar.gz").write_bytes(evil.getvalue())
    assert not results.unpack_logs(run_dir)
    assert not (tmp_path / "escaped.txt").exists()
    assert not results.unpack_logs(tmp_path / "missing")


def test_run_rclone_raises_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 3, "", "directory not found")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="directory not found"):
        results.run_rclone(["lsjson", "x:"])


# --- HTTP -----------------------------------------------------------------------------


def test_http_retries_once_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = fake_urlopen(monkeypatch, [http_error("u", 503), b"ok"])
    assert _http.get("http://x/", "ua") == (200, b"ok")
    assert len(requests) == 2
    assert requests[0].get_header("User-agent") == "ua"


def test_http_gives_up_after_two_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, [TimeoutError("slow"), TimeoutError("slow")])
    with pytest.raises(TimeoutError):
        _http.get("http://x/", "ua")


def test_http_returns_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = fake_urlopen(monkeypatch, [http_error("u", 404, b"nope")])
    assert _http.get("http://x/", "ua") == (404, b"nope")
    assert len(requests) == 1


# --- monitor --------------------------------------------------------------------------


def test_monitor_query_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.delenv("CYLC_MONITOR_READER_TOKEN", raising=False)
    monkeypatch.setenv("CYLC_MONITOR_READER_TOKEN_FILE", str(tmp_path / "none"))
    assert monitor.query(Config(), "SELECT 1") == []
    assert "no monitor reader token" in capsys.readouterr().err


def test_monitor_query_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CYLC_MONITOR_READER_TOKEN", "secret")
    requests = fake_urlopen(monkeypatch, [b'[{"a": 1}]'])
    config = Config(monitor_url="https://m.example", monitor_database="cylc")
    assert monitor.query(config, "SELECT :x AS a", x=1) == [{"a": 1}]
    assert requests[0].full_url.startswith("https://m.example/cylc.json?sql=SELECT")
    assert "_shape=array" in requests[0].full_url and "x=1" in requests[0].full_url
    assert requests[0].get_header("Authorization") == "Bearer secret"


def test_monitor_authorization_survives_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real urllib against a local server that answers 302 like Datasette does."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/cylc.json"):
                self.send_response(302)
                self.send_header("Location", "/cylc/-/query.json?" + self.path.split("?", 1)[1])
                self.end_headers()
                return
            body = b'[{"ok": 1}]' if self.headers.get("Authorization") == "Bearer secret" else b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("CYLC_MONITOR_READER_TOKEN", "secret")
        config = Config(monitor_url=f"http://127.0.0.1:{server.server_port}", monitor_database="cylc")
        assert monitor.query(config, "SELECT 1") == [{"ok": 1}]
    finally:
        server.shutdown()
    assert [auth for _, auth in seen] == ["Bearer secret", "Bearer secret"]


def test_fetch_jobs_pages_until_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [[{"name": "a"}, {"name": "b"}], [{"name": "c"}], []]
    sqls = []

    def query(config, sql, **params):
        sqls.append((sql, params))
        return pages.pop(0)

    monkeypatch.setattr(monitor, "query", query)
    rows = monitor.fetch_jobs(Config(workflow="realtime/run1"))
    assert [r["name"] for r in rows] == ["a", "b", "c"]
    assert "OFFSET 0" in sqls[0][0] and "OFFSET 2" in sqls[1][0] and "OFFSET 3" in sqls[2][0]
    assert sqls[0][1] == {"workflow": "realtime/run1"}


# --- SimDB ----------------------------------------------------------------------------


def test_simdb_station_ims(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = fake_urlopen(monkeypatch, [b'{"event_id": "2026p1", "sites": []}'])
    config = Config(simdb_api="https://s.example/simulation_db/api")
    assert simdb.station_ims(config, "2026p1") == {"event_id": "2026p1", "sites": []}
    assert requests[0].full_url == "https://s.example/simulation_db/api/api/events/2026p1/station-ims?dataset=realtime"


@pytest.mark.parametrize("code", [400, 404])
def test_simdb_not_ingested(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    fake_urlopen(monkeypatch, [http_error("u", code, b'{"error": "unknown event_id"}')])
    assert simdb.event_summary(Config(), "2099p1") is None


# --- SeismicNow -----------------------------------------------------------------------


def make_zip(names: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in names.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_flatfiles_unpack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = make_zip({"flat/site_table.csv": b"sta\n", "flat/gmc_predictions.csv": b"x\n"})
    requests = fake_urlopen(monkeypatch, [body])
    dest = tmp_path / "observed"
    dest.mkdir()
    (dest / "stale.csv").write_text("old")
    config = Config(seismicnow_api="https://n.example/api")
    assert seismicnow.download_flatfiles(config, "2026p1", dest)
    assert requests[0].full_url == "https://n.example/api/download/flatfiles?event_id=2026p1"
    assert sorted(p.name for p in dest.iterdir()) == ["gmc_predictions.csv", "site_table.csv"]


@pytest.mark.parametrize("name", ["../evil.csv", "/abs.csv", "a/../../evil.csv"])
def test_flatfiles_reject_unsafe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    fake_urlopen(monkeypatch, [make_zip({name: b"x", "ok.csv": b"y"})])
    with pytest.raises(ValueError, match="unsafe"):
        seismicnow.download_flatfiles(Config(), "2026p1", tmp_path / "observed")
    assert not (tmp_path / "observed").exists()
    assert not (tmp_path / "evil.csv").exists()


def test_flatfiles_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_urlopen(monkeypatch, [http_error("u", 404), b'{"message": "No earthquakes found"}'])
    assert not seismicnow.download_flatfiles(Config(), "2026p1", tmp_path / "o")
    assert not seismicnow.download_flatfiles(Config(), "2026p1", tmp_path / "o")
    assert not (tmp_path / "o").exists()
