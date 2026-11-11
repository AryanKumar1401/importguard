"""Probe script executed inside the *target* interpreter via ``python -c``.

It imports one module and records, as JSON, every module that the import
loaded, which module and source line triggered each load, and the side
effects (network, subprocess, file writes, environment changes, threads)
performed while importing.

Constraints:
- Must run on Python 3.10+ with no third-party dependencies.
- Must not import anything (beyond what the interpreter preloads) before the
  target import, or those modules would be hidden from the measurement.

argv: <out_path> <target> <mode> <lazy_spec_path>
  mode: "eager"    - normal import
        "discover" - eager import, plus record every lazy-eligible import
                     statement (Python 3.15+, PEP 810)
        "lazy"     - make the import statements listed in lazy_spec_path lazy
"""

import sys


def _main():
    out_path, target, mode, spec_path = sys.argv[1:5]
    getframe = sys._getframe
    os = sys.modules.get("os")

    preloaded = set(sys.modules)
    seen = set(preloaded)
    records = []  # [name, parent, file, line]
    attempted = []  # names searched for but never loaded (e.g. optional deps)
    effects = []  # [category, event, detail, module, file, line]
    statements = []  # [importer, name, fromlist, file, line] (discover mode)
    state = {"on": True}

    skip_suffixes = (
        "importlib/__init__.py",
        "importlib\\__init__.py",
        "importlib/_bootstrap.py",
        "importlib\\_bootstrap.py",
        "importlib/_bootstrap_external.py",
        "importlib\\_bootstrap_external.py",
    )

    def import_site(depth):
        f = getframe(depth)
        while f is not None:
            fn = f.f_code.co_filename
            if fn.startswith("<frozen importlib") or fn.endswith(skip_suffixes):
                f = f.f_back
                continue
            return f
        return None

    class Watcher:
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if state["on"] and fullname not in seen:
                seen.add(fullname)
                f = import_site(2)
                if f is None:
                    records.append([fullname, None, None, None])
                else:
                    records.append(
                        [fullname, f.f_globals.get("__name__"), f.f_code.co_filename, f.f_lineno]
                    )
            return None

    write_flags = 0
    if os is not None:
        for flag in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"):
            write_flags |= getattr(os, flag, 0)

    network_events = {
        "socket.connect": 1,
        "socket.bind": 1,
        "socket.sendto": 2,
        "socket.getaddrinfo": None,
        "http.client.connect": None,
        "urllib.Request": None,
    }
    subprocess_events = (
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
        "os.startfile",
    )
    fs_events = (
        "os.remove",
        "os.rename",
        "os.mkdir",
        "os.rmdir",
        "os.truncate",
        "os.symlink",
        "os.link",
        "os.chmod",
        "os.chown",
        "shutil.rmtree",
        "shutil.copyfile",
        "shutil.move",
    )
    env_events = ("os.putenv", "os.unsetenv")
    thread_events = ("_thread.start_new_thread", "_thread.start_joinable_thread")

    def classify(event, args):
        if event in network_events:
            idx = network_events[event]
            if idx is not None and len(args) > idx:
                return "network", repr(args[idx])
            if event == "socket.getaddrinfo" and len(args) >= 2:
                return "network", f"{args[0]}:{args[1]}"
            return "network", repr(args)
        if event in subprocess_events:
            return "subprocess", repr(args[1] if event == "subprocess.Popen" else args)
        if event == "open":
            path, mode_, flags = [*list(args), None, None, None][:3]
            if not isinstance(path, (str, bytes)):
                return None
            spath = path if isinstance(path, str) else path.decode("utf-8", "replace")
            if "__pycache__" in spath or spath.endswith(".pyc"):
                return None
            if isinstance(mode_, str):
                writing = any(c in mode_ for c in "wax+")
            else:
                writing = isinstance(flags, int) and bool(flags & write_flags)
            return ("file_write", spath) if writing else None
        if event in fs_events:
            return "file_write", repr(args[0]) if args else event
        if event in env_events:
            key = args[0] if args else ""
            if isinstance(key, bytes):
                key = key.decode("utf-8", "replace")
            return "env", str(key)
        if event in thread_events:
            return "thread", ""
        return None

    def audit(event, args):
        if not state["on"]:
            return
        try:
            hit = classify(event, args)
            if hit is None or "__pycache__" in hit[1]:
                return  # bytecode caching by the import system itself
            # Attribute to the innermost module whose top-level code is running.
            f = getframe(1)
            while f is not None:
                if f.f_code.co_name == "<module>" and f.f_globals.get("__name__") != "__main__":
                    break
                f = f.f_back
            if f is None:
                effects.append([hit[0], event, hit[1][:300], None, None, None])
            else:
                effects.append(
                    [
                        hit[0],
                        event,
                        hit[1][:300],
                        f.f_globals.get("__name__"),
                        f.f_code.co_filename,
                        f.f_lineno,
                    ]
                )
        except Exception:
            pass

    lazy_keys = None
    lazy_hits = set()
    if mode in ("discover", "lazy"):
        if not hasattr(sys, "set_lazy_imports"):
            raise SystemExit(
                "importguard: this interpreter does not support lazy imports (PEP 810)"
            )
        if mode == "lazy":
            lazy_keys = set()
            with open(spec_path, encoding="utf-8") as fh:
                for raw in fh.read().splitlines():
                    if raw:
                        imp, name, fl = raw.split("\t")
                        lazy_keys.add((imp, name, fl))

        def lazy_filter(importer, name, fromlist):
            fl = ",".join(fromlist) if fromlist else ""
            if lazy_keys is not None:
                key = (importer, name, fl)
                if key in lazy_keys:
                    lazy_hits.add(key)
                    return True
                return False
            if state["on"] and importer != "__main__":
                f = getframe(1)
                statements.append([importer, name, fl, f.f_code.co_filename, f.f_lineno])
            return False

        sys.set_lazy_imports_filter(lazy_filter)
        sys.set_lazy_imports("all")

    sys.addaudithook(audit)
    sys.meta_path.insert(0, Watcher)

    error = None
    try:
        __import__(target)
    except BaseException as exc:  # SystemExit/KeyboardInterrupt at import are findings too
        error = exc
    state["on"] = False

    loaded = [r for r in records if r[0] in sys.modules]
    attempted = [r[0] for r in records if r[0] not in sys.modules]
    # Modules loaded behind our back (e.g. a finder inserted ahead of ours).
    known = {r[0] for r in records}
    for name in list(sys.modules):
        if name not in preloaded and name not in known:
            loaded.append([name, None, None, None])

    std = getattr(sys, "stdlib_module_names", frozenset())
    modules = []
    for name, parent, file, line in loaded:
        mod = sys.modules.get(name)
        spec = getattr(mod, "__spec__", None)
        origin = getattr(spec, "origin", None) or getattr(mod, "__file__", None)
        if origin is None and spec is not None and spec.submodule_search_locations:
            origin = next(iter(spec.submodule_search_locations))
        modules.append(
            {
                "name": name,
                "parent": parent,
                "file": file,
                "line": line,
                "origin": origin if isinstance(origin, str) else None,
                "stdlib": name.partition(".")[0] in std,
            }
        )

    err = None
    if error is not None:
        import traceback

        err = {
            "type": type(error).__name__,
            "message": str(error)[:2000],
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )[-4000:],
        }

    import json

    payload = {
        "probe_version": 1,
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "executable": sys.executable,
        "target": target,
        "mode": mode,
        "error": err,
        "modules": modules,
        "attempted": attempted,
        "effects": [
            dict(zip(("category", "event", "detail", "module", "file", "line"), e, strict=True))
            for e in effects
        ],
        "statements": [
            dict(zip(("importer", "name", "fromlist", "file", "line"), s, strict=True))
            for s in statements
        ],
        "lazy_hits": sorted("\t".join(k) for k in lazy_hits),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


_main()
