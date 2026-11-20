"""Tests of publish.py: pull and push against a fake rclone and a fake Datasette."""

import datetime
import gzip
import io
import json
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from email.message import Message
from pathlib import Path

import pytest

from auto_scan import publish
from auto_scan.config import Config
from auto_scan.db import connect


class FakeRclone:
    """rclone over a local directory standing in for the remote ``store:``."""

    def __init__(self, root: Path, fail: set[str] | None = None, broken: bool = False) -> None:
        self.root = root
        self.fail = fail or set()
        self.broken = broken
        self.calls: list[list[str]] = []

    def local(self, path: str) -> Path:
        assert path.startswith("store:"), path
        return self.root / path.removeprefix("store:").lstrip("/")

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        assert args[0] == "rclone"
        if self.broken:
            return subprocess.CompletedProcess(args, 1, "", "Failed to create file system: token expired")
        if args[1] in self.fail:
            return subprocess.CompletedProcess(args, 5, "", "temporary error")
        if args[1] == "lsjson":
            target = self.local(args[-1])
            if target.is_file():
                return subprocess.CompletedProcess(args, 0, json.dumps({"Path": target.name}), "")
            return subprocess.CompletedProcess(args, 3, "", "Failed to lsjson: directory not found")
        if args[1] == "copyto":
            ignore_existing = "--ignore-existing" in args
            source, dest = [a for a in args[2:] if not a.startswith("--")]
            src = self.local(source) if source.startswith("store:") else Path(source)
            dst = self.local(dest) if dest.startswith("store:") else Path(dest)
            if not src.is_file():
                return subprocess.CompletedProcess(args, 3, "", "directory not found")
            if ignore_existing and dst.exists():
                return subprocess.CompletedProcess(args, 0, "", "")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected rclone call {args}")


def make_db(path: Path, events: int) -> None:
    """A scan.db with this many events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    for i in range(events):
        connection.execute("INSERT INTO events (event_id, event_dir) VALUES (?, ?)", (f"e{i}", f"{i}-e{i}"))
    connection.commit()
    connection.close()


def count_events(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        connection.close()


def meta(path: Path, key: str) -> str | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        connection.close()


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    monkeypatch.setenv("AUTO_SCAN_STATE_URL", "store:state")
    monkeypatch.setenv("AUTO_SCAN_DATASETTE_URL", "https://datasette.test/")
    monkeypatch.setenv("AUTO_SCAN_WRITER_TOKEN", "dstok_writer")
    monkeypatch.setenv("AUTO_SCAN_WRITER_TOKEN_FILE", str(tmp_path / "no-token-file"))
    monkeypatch.delenv("AUTO_SCAN_ALLOW_SHRINK", raising=False)
    return Config()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    return root


def test_pull_missing_remote_leaves_no_file(config: Config, remote: Path, tmp_path: Path) -> None:
    rclone = FakeRclone(remote)
    db_path = tmp_path / "work" / "scan.db"
    publish.pull(config, db_path, runner=rclone)
    assert not db_path.exists()
    assert all(call[1] != "copyto" for call in rclone.calls)


def test_pull_failing_rclone_raises(config: Config, remote: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "scan.db"
    with pytest.raises(publish.StoreError):
        publish.pull(config, db_path, runner=FakeRclone(remote, broken=True))
    assert not db_path.exists()


def test_pull_copy_failure_raises(config: Config, remote: Path, tmp_path: Path) -> None:
    make_db(remote / "state" / "scan.db", 2)
    with pytest.raises(publish.StoreError):
        publish.pull(config, tmp_path / "scan.db", runner=FakeRclone(remote, fail={"copyto"}))


def test_pull_rejects_non_sqlite(config: Config, remote: Path, tmp_path: Path) -> None:
    (remote / "state").mkdir()
    (remote / "state" / "scan.db").write_text("<html>not a database</html>")
    with pytest.raises(publish.StoreError, match="not an SQLite"):
        publish.pull(config, tmp_path / "scan.db", runner=FakeRclone(remote))


def test_pull_records_event_count(config: Config, remote: Path, tmp_path: Path) -> None:
    make_db(remote / "state" / "scan.db", 3)
    db_path = tmp_path / "scan.db"
    db_path.with_name("scan.db-journal").write_text("stale")
    publish.pull(config, db_path, runner=FakeRclone(remote))
    assert count_events(db_path) == 3
    assert meta(db_path, "pulled_events") == "3"
    assert not db_path.with_name("scan.db-journal").exists()


def test_push_stores_history_and_uploads(config: Config, remote: Path, tmp_path: Path) -> None:
    make_db(remote / "state" / "scan.db", 2)
    rclone = FakeRclone(remote)
    db_path = tmp_path / "scan.db"
    publish.pull(config, db_path, runner=rclone)
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO events (event_id, event_dir) VALUES ('new', '9-new')")
    connection.commit()
    connection.close()

    sent: list[urllib.request.Request] = []

    def poster(request: urllib.request.Request) -> bytes:
        sent.append(request)
        return b'{"ok": true}'

    publish.push(config, db_path, runner=rclone, poster=poster, today=datetime.date(2026, 11, 19))
    assert count_events(remote / "state" / "scan.db") == 3
    assert count_events(remote / "state" / "history" / "scan-20261119.db") == 3
    assert meta(db_path, "pulled_events") == "3"

    (request,) = sent
    assert request.full_url == "https://datasette.test/-/scan/upload"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer dstok_writer"
    assert request.get_header("User-agent") == config.user_agent
    assert isinstance(request.data, bytes)
    body = gzip.decompress(request.data)
    assert body.startswith(b"SQLite format 3\x00")

    # A second push the same day keeps the day's first copy.
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO events (event_id, event_dir) VALUES ('newer', '10-newer')")
    connection.commit()
    connection.close()
    publish.push(config, db_path, runner=rclone, poster=poster, today=datetime.date(2026, 11, 19))
    assert count_events(remote / "state" / "scan.db") == 4
    assert count_events(remote / "state" / "history" / "scan-20261119.db") == 3


def test_push_refuses_to_shrink(config: Config, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_db(remote / "state" / "scan.db", 3)
    rclone = FakeRclone(remote)
    db_path = tmp_path / "scan.db"
    publish.pull(config, db_path, runner=rclone)
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM events WHERE event_id = 'e0'")
    connection.commit()
    connection.close()
    with pytest.raises(publish.StoreError, match="refusing"):
        publish.push(config, db_path, datasette=False, runner=rclone)
    assert count_events(remote / "state" / "scan.db") == 3

    monkeypatch.setenv("AUTO_SCAN_ALLOW_SHRINK", "1")
    publish.push(config, db_path, datasette=False, runner=rclone)
    assert count_events(remote / "state" / "scan.db") == 2


def test_push_never_pulled_refuses_over_existing_store(config: Config, remote: Path, tmp_path: Path) -> None:
    make_db(remote / "state" / "scan.db", 3)
    db_path = tmp_path / "scan.db"
    make_db(db_path, 5)  # more events, but never pulled: still refused
    with pytest.raises(publish.StoreError, match="never pulled"):
        publish.push(config, db_path, datasette=False, runner=FakeRclone(remote))


def test_push_first_run_with_empty_store(config: Config, remote: Path, tmp_path: Path) -> None:
    rclone = FakeRclone(remote)
    db_path = tmp_path / "scan.db"
    publish.pull(config, db_path, runner=rclone)
    make_db(db_path, 1)
    publish.push(config, db_path, datasette=False, runner=rclone, today=datetime.date(2026, 1, 2))
    assert count_events(remote / "state" / "scan.db") == 1
    assert (remote / "state" / "history" / "scan-20260102.db").is_file()


def test_push_store_failure_raises(config: Config, remote: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "scan.db"
    make_db(db_path, 1)
    with pytest.raises(publish.StoreError, match="copyto"):
        publish.push(config, db_path, runner=FakeRclone(remote, fail={"copyto"}), poster=lambda r: b"")
    assert not (remote / "state" / "scan.db").exists()


def test_push_without_token_skips_datasette(
    config: Config, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AUTO_SCAN_WRITER_TOKEN")
    db_path = tmp_path / "scan.db"
    make_db(db_path, 1)

    def poster(request: urllib.request.Request) -> bytes:
        raise AssertionError("should not upload without a token")

    publish.push(Config(), db_path, runner=FakeRclone(remote), poster=poster)
    assert "not uploading" in capsys.readouterr().err
    assert (remote / "state" / "scan.db").is_file()


def test_push_datasette_failure_is_a_warning(
    config: Config, remote: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "scan.db"
    make_db(db_path, 1)

    def poster(request: urllib.request.Request) -> bytes:
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", Message(), io.BytesIO(b"forbidden"))

    publish.push(config, db_path, runner=FakeRclone(remote), poster=poster)
    assert "HTTP 403" in capsys.readouterr().err
    assert (remote / "state" / "scan.db").is_file()

    publish.push(config, db_path, runner=FakeRclone(remote), poster=lambda r: b'{"ok": false, "error": "bad"}')
    assert "not accepted" in capsys.readouterr().err


def test_push_missing_database(config: Config, remote: Path, tmp_path: Path) -> None:
    with pytest.raises(publish.StoreError, match="does not exist"):
        publish.push(config, tmp_path / "absent.db", runner=FakeRclone(remote))


def test_push_cmt_store_uploads_to_its_route(config: Config, remote: Path, tmp_path: Path) -> None:
    make_db(remote / "state" / "cmt.db", 1)
    rclone = FakeRclone(remote)
    db_path = tmp_path / "cmt.db"
    publish.pull(config, db_path, runner=rclone, store=publish.CMT_STORE_NAME)
    sent: list[urllib.request.Request] = []

    def poster(request: urllib.request.Request) -> bytes:
        sent.append(request)
        return b'{"ok": true}'

    publish.push(
        config, db_path, runner=rclone, poster=poster, today=datetime.date(2026, 11, 19), store=publish.CMT_STORE_NAME
    )
    assert count_events(remote / "state" / "history" / "cmt-20261119.db") == 1
    assert not (remote / "state" / "scan.db").exists()
    (request,) = sent
    assert request.full_url == "https://datasette.test/-/scan/upload/cmt"
