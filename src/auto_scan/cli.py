"""The ``auto-scan`` command.

    auto-scan pull              fetch scan.db from the state store (a first run starts empty)
    auto-scan collect [EVENT]   ingest new or changed runs: download, extract, record
    auto-scan check [EVENT]     run every check, update the findings
    auto-scan verify            follow filed findings' issues; verify or reopen
    auto-scan brief [--out F]   write the triage brief for the findings not yet filed
    auto-scan link ID URL       record the GitHub issue filed for a finding
    auto-scan dismiss ID WHY    close a finding as not a problem
    auto-scan push              store scan.db and upload it to the Datasette
    auto-scan run               pull, collect, check, verify, push

``--review cmt`` (before the subcommand) does the same for the CMT review:
SeismicNow's automatic CMT inversions, in cmt.db ($AUTO_SCAN_CMT_DB). Its collect
takes any number of event ids, whatever their age; with none it takes the CMTs
SeismicNow posted since the last run.

    auto-scan --review cmt run
    auto-scan --review cmt collect 2026p866048 2026p862161 --force
    auto-scan --review cmt fetch 2026p866048 --out .ai/cmt/2026p866048   (unpack its results)

and, for its open-ended review of each event (auto_scan.cmt.observe):

    auto-scan --review cmt unexplored                  the events no reviewer has read, as JSON
    auto-scan --review cmt sheet EVENT [--out F]       the numbers a reviewer gets with the download
    auto-scan --review cmt observe EVENT FILE          record a reviewer's observations (JSON)
    auto-scan --review cmt resolve KEY STATUS WHY      known, filed or dismissed, for every event with KEY

Each subcommand is one function in the module that owns it; this file only parses.
"""

import argparse
import json
import sys
from pathlib import Path

from auto_scan import collect, engine, issues, publish, registry
from auto_scan.cmt import collect as cmt_collect
from auto_scan.cmt import observe as cmt_observe
from auto_scan.cmt import source as cmt_source
from auto_scan.config import Config
from auto_scan.db import connect


def main(argv: list[str] | None = None) -> int:
    """Run the ``auto-scan`` command.

    Parameters
    ----------
    argv : list of str, optional
        The arguments, by default ``sys.argv[1:]``.

    Returns
    -------
    int
        The exit status.
    """
    parser = argparse.ArgumentParser(
        prog="auto-scan", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--db", type=Path, help="the database (default: $AUTO_SCAN_DB or ./scan.db; cmt: $AUTO_SCAN_CMT_DB or ./cmt.db)"
    )
    parser.add_argument(
        "--review",
        choices=("simulation", "cmt"),
        default="simulation",
        help="which review: the realtime simulations (default) or SeismicNow's CMTs",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pull")
    command = commands.add_parser("collect")
    command.add_argument("events", nargs="*", help="only these event ids (the simulation review takes one)")
    command.add_argument("--force", action="store_true", help="redo runs already scanned")
    command = commands.add_parser("check")
    command.add_argument("event", nargs="?", help="only this event id")
    command.add_argument("--force", action="store_true", help="unused; kept for symmetry with collect")
    commands.add_parser("verify")
    commands.add_parser("unexplored")
    sheet = commands.add_parser("sheet")
    sheet.add_argument("event")
    sheet.add_argument("--out", type=Path, help="write the sheet here instead of stdout")
    observe_command = commands.add_parser("observe")
    observe_command.add_argument("event")
    observe_command.add_argument("file", type=Path, help="the reviewer's observations, a JSON list")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("key")
    resolve.add_argument("status", choices=sorted(cmt_observe.RESOLVED))
    resolve.add_argument("resolution", help="the check or issue URL that covers it, or why it is no problem")
    fetch = commands.add_parser("fetch")
    fetch.add_argument("event")
    fetch.add_argument("--out", type=Path, required=True, help="directory to unpack into (replaced)")
    brief = commands.add_parser("brief")
    brief.add_argument("--out", type=Path, help="write the brief here instead of stdout")
    brief.add_argument("--max-findings", type=int, default=5, help="0: every open finding")
    link = commands.add_parser("link")
    link.add_argument("finding", type=int)
    link.add_argument("url")
    dismiss = commands.add_parser("dismiss")
    dismiss.add_argument("finding", type=int)
    dismiss.add_argument("reason")
    push = commands.add_parser("push")
    push.add_argument("--no-datasette", action="store_true", help="store scan.db but don't upload it")
    run = commands.add_parser("run")
    run.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)

    config = Config()
    cmt = args.review == "cmt"
    db_path = args.db or (config.cmt_db_path if cmt else config.db_path)
    store = publish.CMT_STORE_NAME if cmt else publish.STORE_NAME
    if args.command == "collect" and not cmt and len(args.events) > 1:
        parser.error("the simulation review collects one event at a time")

    def run_collect(events: list[str], force: bool) -> None:
        """Collect for the chosen review.

        Parameters
        ----------
        events : list of str
            The event ids named, or none for every new one.
        force : bool
            Redo events already scanned.
        """
        if cmt:
            cmt_collect.collect(config, db, events=events or None, force=force)
        else:
            collect.collect(config, db, event=events[0] if events else None, force=force)

    def run_checks(event: str | None) -> None:
        """Check for the chosen review.

        Parameters
        ----------
        event : str or None
            Only this event id, or every event.
        """
        if cmt:
            registry.load_cmt_plugins()
            engine.run_checks(db, event=event, checks=registry.CMT_CHECKS)
        else:
            engine.run_checks(db, event=event)

    if args.command in ("unexplored", "sheet", "observe", "resolve") and not cmt:
        parser.error(f"{args.command} is for --review cmt")
    if args.command == "fetch":
        if not cmt:
            parser.error("fetch is for --review cmt; the simulations' files are in the rclone results store")
        if cmt_source.download_results(config, args.event, args.out) is None:
            print(f"auto-scan: SeismicNow has no CMT results for {args.event}", file=sys.stderr)
            return 1
        print(f"auto-scan: unpacked {args.event}'s CMT results into {args.out}", file=sys.stderr)
        return 0
    if args.command == "pull":
        publish.pull(config, db_path, store=store)
        return 0
    if args.command == "run":
        publish.pull(config, db_path, store=store)
    db = connect(db_path)
    try:
        if args.command == "collect":
            run_collect(args.events, args.force)
        elif args.command == "check":
            run_checks(args.event)
        elif args.command == "verify":
            issues.verify(db)
        elif args.command == "unexplored":
            print(json.dumps(cmt_observe.unexplored(db)))
        elif args.command == "sheet":
            text = cmt_observe.sheet(db, args.event)
            if args.out:
                args.out.write_text(text)
            else:
                sys.stdout.write(text)
        elif args.command == "observe":
            count = cmt_observe.import_observations(db, args.event, args.file)
            print(f"auto-scan: cmt: {args.event}: {count} observation(s) recorded", file=sys.stderr)
        elif args.command == "resolve":
            count = cmt_observe.resolve(db, args.key, args.status, args.resolution)
            print(f"auto-scan: cmt: {args.key}: {count} observation(s) {args.status}", file=sys.stderr)
        elif args.command == "brief":
            text = issues.brief(db, max_findings=args.max_findings, review=args.review)
            if args.out:
                args.out.write_text(text)
            else:
                sys.stdout.write(text)
        elif args.command == "link":
            issues.link(db, args.finding, args.url)
        elif args.command == "dismiss":
            issues.dismiss(db, args.finding, args.reason)
        elif args.command == "run":
            run_collect([], False)
            run_checks(None)
            issues.verify(db)
        db.commit()
    finally:
        db.close()
    if args.command == "push":
        publish.push(config, db_path, datasette=not args.no_datasette, store=store)
    elif args.command == "run" and not args.no_push:
        publish.push(config, db_path, store=store)
    return 0
