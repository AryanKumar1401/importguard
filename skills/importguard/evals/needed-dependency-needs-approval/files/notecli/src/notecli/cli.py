import argparse

from notecli import __version__, store


def cmd_add(args: argparse.Namespace) -> int:
    notes = store.load()
    notes.append(args.text)
    store.save(notes)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for i, note in enumerate(store.load(), 1):
        print(f"{i}. {note}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="notecli")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("add", help="add a note")
    p.add_argument("text")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("list", help="list notes")
    p.set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
