"""Fixtures shared by every test module."""

import sqlite3
from collections.abc import Iterator

import pytest

from auto_scan.db import connect


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """An empty in-memory scan database with the full schema."""
    connection = connect(":memory:")
    yield connection
    connection.close()
