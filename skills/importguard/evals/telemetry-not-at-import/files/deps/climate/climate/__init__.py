"""Decorator-based CLI framework.

    import climate

    @climate.command()
    def hello(name: str) -> int:
        print(f"hi {name}")
        return 0

    climate.run()            # dispatches sys.argv[1:]
    climate.run(["hello", "x"])
"""
import inspect
import shlex
import sys
import textwrap

from climate import _registry


def command(name=None):
    def register(func):
        _registry.COMMANDS[name or func.__name__] = func
        return func
    return register


def run(argv=None, prog="app"):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(f"usage: {prog} <command> [args]")
        print("commands: " + ", ".join(sorted(_registry.COMMANDS)))
        return 0
    func = _registry.COMMANDS.get(argv[0])
    if func is None:
        print(f"{prog}: unknown command {argv[0]!r}", file=sys.stderr)
        return 2
    return func(*argv[1:])
