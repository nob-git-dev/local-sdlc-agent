#!/usr/bin/env python3
"""Command-line interface for minigit."""

import argparse
import sys
from pathlib import Path

from minigit.errors import MiniGitError
from minigit.repository import Repository


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that raises on error instead of exiting."""

    def error(self, message):
        raise argparse.ArgumentError(None, message)


def _build_parser():
    parser = _ArgumentParser(prog="minigit")
    parser.add_argument("--repo", default=".", help="repository path")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("path", nargs="?", default=None)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("paths", nargs="+")

    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("-m", "--message", required=True)
    commit_parser.add_argument("--author", default="unknown")
    commit_parser.add_argument("--timestamp", default=None)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--porcelain", action="store_true")

    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--oneline", action="store_true")
    log_parser.add_argument("--max-count", type=int, default=None)

    checkout_parser = subparsers.add_parser("checkout")
    checkout_parser.add_argument("revision")
    checkout_parser.add_argument("--force", action="store_true")

    return parser


def _repo_path(args) -> Path:
    if args.command == "init" and args.path is not None:
        return Path(args.path).resolve()
    return Path(args.repo).resolve()


def _run(args) -> int:
    path = _repo_path(args)
    if args.command == "init":
        Repository.init(path)
        return 0
    repo = Repository.open(path)
    if args.command == "add":
        repo.add(args.paths)
        return 0
    if args.command == "commit":
        oid = repo.commit(
            args.message, author=args.author, timestamp=args.timestamp
        )
        print(oid)
        return 0
    if args.command == "status":
        status = repo.status()
        if args.porcelain:
            for path in status["staged"]:
                print(f"A  {path}")
            for path in status["modified"]:
                print(f" M {path}")
            for path in status["deleted"]:
                print(f" D {path}")
            for path in status["untracked"]:
                print(f"?? {path}")
        else:
            for key in ("staged", "modified", "deleted", "untracked"):
                for path in status[key]:
                    print(f"{key}: {path}")
        return 0
    if args.command == "log":
        items = repo.log(max_count=args.max_count)
        for item in items:
            if args.oneline:
                print(f"{item['oid'][:12]} {item['message']}")
            else:
                print(item["oid"])
                print(f"message: {item['message']}")
                print(f"author: {item['author']}")
                print(f"timestamp: {item['timestamp']}")
        return 0
    if args.command == "checkout":
        repo.checkout(args.revision, force=args.force)
        return 0
    return 2


def main(argv=None):
    """Entry point for the minigit CLI."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as exc:
        print(f"minigit: {exc}", file=sys.stderr)
        return 2
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        return _run(args)
    except MiniGitError as exc:
        print(f"minigit: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
