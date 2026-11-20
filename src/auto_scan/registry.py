"""The two plug-in points: extractors, which turn an event's files into rows, and
checks, which turn rows into evaluations.

A module under ``auto_scan.extract`` or ``auto_scan.checks`` registers its
functions with the decorators here when it is imported; ``load_plugins`` imports
every module of both packages.

The CMT review (``auto_scan.cmt``) keeps its own lists, ``CMT_EXTRACTORS`` and
``CMT_CHECKS``, filled by ``cmt_extractor`` and ``cmt_check`` and loaded by
``load_cmt_plugins``: its checks run on ``cmt.db``, never on the simulations'
``scan.db``, and the simulation checks never run on it.
"""

import importlib
import pkgutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Kind = Literal["science", "perf", "ops"]
Severity = Literal["critical", "high", "medium", "low"]


@dataclass
class Evaluation:
    """What one check concluded about one subject on one run.

    A check yields an evaluation whether it passes or fails, so a finding's
    history shows when it stopped failing: that is how a fix is verified. A check
    that can't be evaluated (an input is missing) yields nothing.

    Attributes
    ----------
    check_id : str
        The check's dotted name, e.g. ``perf.cost_model_overestimate``.
    event_id : str
        The event evaluated.
    failing : bool
        Whether the check failed.
    title : str
        One line naming the problem, without this event's numbers: the title of
        the finding and of its GitHub issue.
    summary : str
        One or two sentences with this event's numbers.
    kind : {"science", "perf", "ops"}
        What sort of problem it is.
    severity : {"critical", "high", "medium", "low"}
        ``critical`` and ``high``: silently wrong scientific results, or no
        results at all.
    resolution : str
        The run's resolution, or ``""`` when the check spans the event.
    subject : str
        What the problem is about when one check can find several at once (a
        station, a task, a resolution). ``(check_id, subject)`` names a finding.
    value : float or None
        The number tested.
    threshold : float or None
        What it was tested against.
    evidence : dict
        Anything JSON-serialisable a person or Claude needs to investigate: the
        numbers, the files they came from, the history compared against.
    suspect_sources : list of str
        The ``run_sources.source`` names most likely at fault (``sw4``,
        ``workflow``, ``im-calculation``, ...), or ``realtime`` for the Cylc
        workflow itself. The triage narrows the commit range to these.
    """

    check_id: str
    event_id: str
    failing: bool
    title: str
    summary: str
    kind: Kind
    severity: Severity = "medium"
    resolution: str = ""
    subject: str = ""
    value: float | None = None
    threshold: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    suspect_sources: list[str] = field(default_factory=list)


# extractor(db, event_id, resolution, run_dir): write rows about one run from the
# files in its cache directory (cache/<cycle>-<event>/<resolution>).
Extractor = Callable[[sqlite3.Connection, str, str, Path], None]
# event_extractor(db, event_id, event_dir): rows that need every resolution's
# files at once (cache/<cycle>-<event>), run after the per-run extractors.
EventExtractor = Callable[[sqlite3.Connection, str, Path], None]
# check(db, event_id): evaluations about one event, from the database alone.
Check = Callable[[sqlite3.Connection, str], Iterable[Evaluation]]

EXTRACTORS: list[Extractor] = []
EVENT_EXTRACTORS: list[EventExtractor] = []
CHECKS: list[Check] = []
# cmt_extractor(db, event_id, event_dir): rows about one event's CMT inversion from
# its unpacked SeismicNow download (cache/cmt/<event_id>), every run at once.
CMT_EXTRACTORS: list[EventExtractor] = []
CMT_CHECKS: list[Check] = []


def extractor(function: Extractor) -> Extractor:
    """Register a per-run extractor.

    Parameters
    ----------
    function : Extractor
        ``function(db, event_id, resolution, run_dir)``. It must tolerate missing
        files (a failed run uploads only some) by writing nothing for them, and
        must be idempotent: a run is re-extracted when its manifest changes.

    Returns
    -------
    Extractor
        The function, unchanged.
    """
    EXTRACTORS.append(function)
    return function


def event_extractor(function: EventExtractor) -> EventExtractor:
    """Register an extractor that runs once an event, after the per-run ones.

    Parameters
    ----------
    function : EventExtractor
        ``function(db, event_id, event_dir)``, with the same tolerance and
        idempotence as a per-run extractor.

    Returns
    -------
    EventExtractor
        The function, unchanged.
    """
    EVENT_EXTRACTORS.append(function)
    return function


def check(function: Check) -> Check:
    """Register a check.

    Parameters
    ----------
    function : Check
        ``function(db, event_id)``, yielding ``Evaluation``s. It reads only the
        database, so it can be re-run on any earlier event.

    Returns
    -------
    Check
        The function, unchanged.
    """
    CHECKS.append(function)
    return function


def cmt_extractor(function: EventExtractor) -> EventExtractor:
    """Register a CMT extractor.

    Parameters
    ----------
    function : EventExtractor
        ``function(db, event_id, event_dir)``, where ``event_dir`` is the event's
        unpacked CMT download. Same tolerance and idempotence as ``extractor``.

    Returns
    -------
    EventExtractor
        The function, unchanged.
    """
    CMT_EXTRACTORS.append(function)
    return function


def cmt_check(function: Check) -> Check:
    """Register a CMT check, run on ``cmt.db`` only.

    Parameters
    ----------
    function : Check
        ``function(db, event_id)``, yielding ``Evaluation``s, as for ``check``.

    Returns
    -------
    Check
        The function, unchanged.
    """
    CMT_CHECKS.append(function)
    return function


def load_cmt_plugins() -> None:
    """Import the CMT review's extractors and checks."""
    importlib.import_module("auto_scan.cmt.extract")
    importlib.import_module("auto_scan.cmt.checks")


def load_plugins() -> None:
    """Import every module under ``auto_scan.extract`` and ``auto_scan.checks``."""
    for package_name in ("auto_scan.extract", "auto_scan.checks"):
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{package_name}.{module.name}")
