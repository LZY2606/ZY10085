"""Command line entry: ``python3 -m diagforge [--host H] [--port P]``.

Starts the API + web UI.  A demo session (examples/ sample compiled by a
fake compiler that crashes on the token ``trigger``) is seeded when the
database is empty, unless --no-demo is given.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import threading
import time
from pathlib import Path

from .engine import Engine
from .export import verify_package
from .predicate import Predicate
from .store import Store
from .webapp import make_server

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def seed_demo(engine):
    sample = EXAMPLES / "sample"
    files = {p.name: p.read_text(encoding="utf-8")
             for p in sorted(sample.iterdir()) if p.is_file()}
    command = "%s %s {src}" % (
        shlex.quote(sys.executable), shlex.quote(str(EXAMPLES / "fakecc.py")))
    predicate = Predicate(exit_codes=(42,),
                          stderr_regex=r"internal compiler error", runs=3)
    return engine.create_session("demo", files, command, predicate,
                                 env_whitelist=("PATH",), timeout_s=10.0)


def scheduler_loop(store, engine, workers, stop):
    while not stop.is_set():
        did_work = False
        for sess in store.list_sessions():
            branch = store.get_active_branch(sess["id"])
            if branch and store.queue(branch["id"]):
                engine.run_workers(branch["id"], n_workers=workers, stop=stop)
                did_work = True
        if not did_work:
            time.sleep(0.2)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "verify":
        pkg = argv[1] if len(argv) > 1 else "."
        report = verify_package(pkg)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    parser = argparse.ArgumentParser(prog="diagforge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5214)
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--no-demo", action="store_true")
    args = parser.parse_args(argv)

    store = Store(args.db)
    engine = Engine(store)
    if not args.no_demo and not store.list_sessions():
        sid, bid = seed_demo(engine)
        print("seeded demo session %s (branch %s)" % (sid[:12], bid[:12]))

    stop = threading.Event()
    scheduler = threading.Thread(
        target=scheduler_loop, args=(store, engine, args.workers, stop),
        daemon=True)
    scheduler.start()

    httpd = make_server(store, engine, args.host, args.port)
    print("diagforge listening on http://%s:%d" % (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
