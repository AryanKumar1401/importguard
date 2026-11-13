"""Probe script executed inside the *target* interpreter via ``python -c``.

It imports one module and records, as JSON, every module that the import
loaded, which module and source line triggered each load, and the side
effects (network, subprocess, file writes, environment changes, threads)
performed while importing.

Constraints:
- Must run on Python 3.10+ with no third-party dependencies.
- Must not import anything (beyond what the interpreter preloads) before the
  target import, or those modules would be hidden from the measurement.

Threat model: the probe runs inside the same interpreter as the code it
measures, so code written specifically to deceive it can always succeed. It
is hardened against accidental blind spots (startup hooks, finders inserted
ahead of it, modules deleted from sys.modules, spoofed ``__name__``); the host
cross-checks its records against ``-X importtime`` output.

env IMPORTGUARD_STUBS: comma-separated modules to replace with inert stand-ins
  when they are missing (used to import a base commit whose dependencies are
  no longer installed); stand-ins are not reported as loaded.

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
    source_cache = {}

    def source_line(filename, lineno):
        # Plain open(): io and the utf-8 codec are preloaded, so this imports
        # nothing (linecache would pull in tokenize mid-import).
        lines = source_cache.get(filename)
        if lines is None:
            try:
                with open(filename, "rb") as fh:
                    lines = fh.read().decode("utf-8", "replace").splitlines()
            except Exception:
                lines = []
            source_cache[filename] = lines
        return lines[lineno - 1] if 0 < lineno <= len(lines) else ""

    pycache_prefix = getattr(sys, "pycache_prefix", None)

    preloaded = set(sys.modules)
    records = {}  # name -> [name, parent, file, line]
    # Recorded names seen in sys.modules at some point, so deleting a module
    # from sys.modules after importing it does not hide it.
    confirmed = set()
    pending = set()

    def confirm():
        modules = sys.modules
        for name in [n for n in pending if n in modules]:
            pending.discard(name)
            confirmed.add(name)

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
            # Record (or re-record, if an earlier lookup such as
            # importlib.util.find_spec did not load it) the first real import.
            if pending:
                confirm()
            if state["on"] and fullname not in preloaded and fullname not in sys.modules:
                f = import_site(2)
                parent = file = line = None
                if f is not None:
                    parent = f.f_globals.get("__name__")
                    file, line = f.f_code.co_filename, f.f_lineno
                    # A parent must be a module that already exists; this keeps the
                    # graph acyclic even if code spoofs __name__.
                    if parent == fullname or (parent not in preloaded and parent not in records):
                        parent = None
                records.pop(fullname, None)
                records[fullname] = [fullname, parent, file, line]
                pending.add(fullname)
            return None

    std_names = getattr(sys, "stdlib_module_names", frozenset())
    write_flags = 0
    if os is not None:
        for flag in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"):
            write_flags |= getattr(os, flag, 0)

    network_events = {
        "socket.connect": 1,
        "socket.bind": 1,
        "socket.sendto": 1,
        "socket.getaddrinfo": None,
        "socket.gethostbyname": 0,
        "socket.gethostbyaddr": 0,
        "socket.getnameinfo": 0,
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
            if pycache_prefix and spath.startswith(pycache_prefix):
                return None
            if isinstance(mode_, str):
                writing = any(c in mode_ for c in "wax+")
            else:
                writing = isinstance(flags, int) and bool(flags & write_flags)
            return ("file_write", spath) if writing else None
        if event in fs_events:
            detail = repr(args[0]) if args else event
            if pycache_prefix and pycache_prefix in detail:
                return None
            return "file_write", detail
        if event == "sqlite3.connect":
            db = args[0] if args else ""
            if isinstance(db, bytes):
                db = db.decode("utf-8", "replace")
            db = str(db)
            if db == ":memory:" or db.startswith("file::memory") or not db:
                return None
            return "file_write", db
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
            if pending and (event == "exec" or event == "import"):
                confirm()
            hit = classify(event, args)
            if hit is None or "__pycache__" in hit[1]:
                return  # bytecode caching by the import system itself
            # Attribute to the innermost module whose top-level code is running;
            # outside module code (e.g. a thread started at import), to the
            # innermost frame of non-stdlib code.
            f = getframe(1)
            fallback = None
            while f is not None:
                modname = f.f_globals.get("__name__") or ""
                if f.f_code.co_name == "<module>" and modname != "__main__":
                    break
                if fallback is None and modname and modname.partition(".")[0] not in std_names:
                    fallback = f
                f = f.f_back
            if f is None:
                f = fallback
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

        def explicitly_lazy(f, name):
            """Was this statement written lazy (or listed in __lazy_modules__)?"""
            listed = f.f_globals.get("__lazy_modules__")
            if listed is not None:
                try:
                    if name in listed:
                        return True
                except TypeError:
                    pass
            text = source_line(f.f_code.co_filename, f.f_lineno).lstrip()
            return text.startswith("lazy ") or text.startswith("lazy\t")

        def lazy_filter(importer, name, fromlist):
            fl = ",".join(fromlist) if fromlist else ""
            f = getframe(1)
            if lazy_keys is not None:
                key = (importer, name, fl)
                if key in lazy_keys:
                    lazy_hits.add(key)
                    return True
            # Anything else keeps the laziness its source asked for.
            if explicitly_lazy(f, name):
                return True
            if lazy_keys is None and state["on"] and importer != "__main__":
                statements.append([importer, name, fl, f.f_code.co_filename, f.f_lineno])
            return False

        sys.set_lazy_imports_filter(lazy_filter)
        sys.set_lazy_imports("all")

    sys.addaudithook(audit)
    sys.meta_path.insert(0, Watcher)

    stubs = [n for n in (os.environ.get("IMPORTGUARD_STUBS", "") if os else "").split(",") if n]
    stubbed = set()
    if stubs:
        bootstrap = sys.modules["_frozen_importlib"]
        module_type = type(sys)

        class Inert:
            # Stands in for anything taken from a stubbed module.
            def __init__(self, *args, **kwargs):
                pass

            def __call__(self, *args, **kwargs):
                return Inert()

            def __getattr__(self, name):
                return Inert()

            def __getitem__(self, item):
                return Inert()

            def __mro_entries__(self, bases):
                return (object,)

        class StubLoader:
            @staticmethod
            def create_module(spec):
                mod = module_type(spec.name)
                mod.__getattr__ = lambda name: Inert()
                mod.__path__ = []
                return mod

            @staticmethod
            def exec_module(module):
                stubbed.add(module.__name__)

        class StubFinder:
            # Appended last: only used when the real module cannot be found.
            @staticmethod
            def find_spec(fullname, path=None, target=None):
                top = fullname.partition(".")[0]
                if top in stubs:
                    return bootstrap.ModuleSpec(fullname, StubLoader, is_package=True)
                return None

        sys.meta_path.append(StubFinder)

    threading = sys.modules.get("threading")
    threads_before = set(threading.enumerate()) if threading is not None else set()

    error = None
    sys.stderr.flush()
    sys.stderr.write("__importguard_begin__\n")
    sys.stderr.flush()
    try:
        __import__(target)
    except BaseException as exc:  # SystemExit/KeyboardInterrupt at import are findings too
        error = exc
    # Give threads started during import a moment, so what they import or do
    # right away is still attributed to startup.
    threading = sys.modules.get("threading")
    if threading is not None:
        budget = 1.0
        clock = sys.modules.get("time")
        for t in threading.enumerate():
            if t in threads_before or t is threading.current_thread():
                continue
            start = clock.monotonic() if clock else 0.0
            try:
                t.join(timeout=budget)
            except Exception:
                pass
            if clock:
                budget -= clock.monotonic() - start
            if budget <= 0:
                break
    confirm()
    state["on"] = False
    try:
        sys.stderr.flush()
        sys.stderr.write("\n__importguard_end__\n")
        sys.stderr.flush()
    except Exception:
        pass

    std = getattr(sys, "stdlib_module_names", frozenset())

    def describe(name):
        mod = sys.modules.get(name)
        spec = getattr(mod, "__spec__", None)
        origin = getattr(spec, "origin", None) or getattr(mod, "__file__", None)
        namespace = False
        if spec is not None and spec.submodule_search_locations is not None:
            if origin in (None, "namespace"):
                namespace = True
                locs = list(spec.submodule_search_locations)
                origin = locs[0] if locs else None
        return (origin if isinstance(origin, str) else None), namespace

    modules = []
    for name in stubbed:
        records.pop(name, None)
    known = set(records) | stubbed
    for name, parent, file, line in records.values():
        origin, namespace = describe(name)
        modules.append(
            {
                "name": name,
                "parent": parent,
                "file": file,
                "line": line,
                "origin": origin,
                "namespace": namespace,
                "stdlib": name.partition(".")[0] in std,
                # False if it was looked up but is not in sys.modules now (an
                # optional import that failed, or a module deleted afterwards;
                # the host decides using -X importtime).
                "present": name in sys.modules or name in confirmed,
            }
        )
    # Modules loaded behind our back (e.g. a finder inserted ahead of ours).
    for name in list(sys.modules):
        if name not in preloaded and name not in known:
            origin, namespace = describe(name)
            mod = sys.modules.get(name)
            modules.append(
                {
                    "name": name,
                    "parent": None,
                    "file": None,
                    "line": None,
                    "origin": origin,
                    "namespace": namespace,
                    "stdlib": name.partition(".")[0] in std,
                    "present": True,
                    # Inserted into sys.modules directly, with no spec or file
                    # (e.g. Cython's cython_runtime): bookkeeping, not a package.
                    "synthetic": origin is None
                    and getattr(mod, "__spec__", None) is None
                    and not namespace,
                }
            )
    # Non-stdlib code that ran before the probe (sitecustomize, .pth files...).
    startup = []
    for name in sorted(preloaded):
        if name.partition(".")[0] in std or name in ("__main__", "builtins", "sys"):
            continue
        origin, _ = describe(name)
        startup.append({"name": name, "origin": origin})

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
        "probe_version": 2,
        "python": ".".join(str(v) for v in sys.version_info[:3]),
        "executable": sys.executable,
        "target": target,
        "mode": mode,
        "error": err,
        "modules": modules,
        "startup": startup,
        "effects": [
            dict(zip(("category", "event", "detail", "module", "file", "line"), e, strict=True))
            for e in effects
        ],
        "statements": [
            dict(zip(("importer", "name", "fromlist", "file", "line"), s, strict=True))
            for s in statements
        ],
        "lazy_hits": sorted("\t".join(k) for k in lazy_hits),
        "stubbed": sorted(stubbed),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


_main()
