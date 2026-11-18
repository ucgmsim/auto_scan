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

Each subcommand is one function in the module that owns it; this file only parses.
"""

import argparse
import sys
from pathlib import Path

from auto_scan import collect, engine, issues, publish
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
    parser.add_argument("--db", type=Path, help="scan.db (default: $AUTO_SCAN_DB or ./scan.db)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pull")
    for name in ("collect", "check"):
        command = commands.add_parser(name)
        command.add_argument("event", nargs="?", help="only this event id")
        command.add_argument("--force", action="store_true", help="redo runs already scanned")
    commands.add_parser("verify")
    brief = commands.add_parser("brief")
    brief.add_argument("--out", type=Path, help="write the brief here instead of stdout")
    brief.add_argument("--max-findings", type=int, default=5)
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
    db_path = args.db or config.db_path
    if args.command == "pull":
        publish.pull(config, db_path)
        return 0
    if args.command == "run":
        publish.pull(config, db_path)
    db = connect(db_path)
    try:
        if args.command == "collect":
            collect.collect(config, db, event=args.event, force=args.force)
        elif args.command == "check":
            engine.run_checks(db, event=args.event)
        elif args.command == "verify":
            issues.verify(db)
        elif args.command == "brief":
            text = issues.brief(db, max_findings=args.max_findings)
            if args.out:
                args.out.write_text(text)
            else:
                sys.stdout.write(text)
        elif args.command == "link":
            issues.link(db, args.finding, args.url)
        elif args.command == "dismiss":
            issues.dismiss(db, args.finding, args.reason)
        elif args.command == "run":
            collect.collect(config, db)
            engine.run_checks(db)
            issues.verify(db)
        db.commit()
    finally:
        db.close()
    if args.command == "push":
        publish.push(config, db_path, datasette=not args.no_datasette)
    elif args.command == "run" and not args.no_push:
        publish.push(config, db_path)
    return 0
