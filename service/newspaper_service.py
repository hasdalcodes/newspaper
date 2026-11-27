#!/usr/bin/env python3
"""
Newspaper background service.

One process that
  * fetches feeds (RSS/Atom + the Al Jazeera scraper's JSON on GitHub) when a print job runs, or on request
  * runs the cron-style print scheduler
  * serves a local HTTP API that the Tk UI connects to

Commands
  newspaper_service.py run        stay in the foreground (default; used by systemd/launchd and for debugging)
  newspaper_service.py start      start in the background; waits until the service is really up and prints
                                 the reason if it is not (port busy, unwritable data folder, ...)
  newspaper_service.py stop | restart | status
  newspaper_service.py token      show the URL and access token the UI needs (for a system service)

Options
  --data-dir DIR   where the database, config.json, log and printed files live (default ~/.newspaper)
  --host H --port P   listen address (saved into config.json so the UI finds it)
  --user U --group G  when started as root: drop to this account (system service, e.g. FreeBSD rc.d)
  --pidfile FILE   where `start` records the pid (default DATA_DIR/service.pid)
"""
import argparse
import json
import logging
import logging.handlers
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import api
import catalog
import printing
import scheduler
import sources
import store

POSIX = hasattr(os, "fork")
log = logging.getLogger("newspaper")


class StartupError(Exception):
    """Something the person starting the service can fix; shown verbatim."""


# ------------------------------------------------------------------ seeding
def seed_defaults() -> bool:
    """Runs at every start. Guarantees a default print template; on a fresh install adds some sources.
    Returns True when sources were created (so the caller can do one first fetch)."""
    base = printing.DEFAULT_LAYOUT
    templates = store.list_templates()
    if not templates:
        tid = store.save_template("Default", dict(base))
        store.save_template("Full articles", {**base, "title": "Full Articles", "columns": 1,
                                              "fields": ["title", "source", "datetime", "author", "content"],
                                              "page_break_each": True})
        store.set_default_template(tid)
    elif not store.get_default_template():  # e.g. database from an older version
        pick = next((t for t in templates if t["name"] in ("Default", "Headlines (cards)")), None)
        store.set_default_template(pick["id"] if pick else store.save_template("Default", dict(base)))

    if store.list_sources():
        return False
    # interval 0 = fetched only when a print job runs (or you press Fetch); nothing polls in the background
    store.save_source({"name": "Al Jazeera scraper (GitHub)", "kind": "github", "url": "hasdalcodes/news",
                       "interval_min": 0, "options": {"branch": "main", "path": "data", "max_files": 8}})
    for cid in catalog.STARTER_IDS:
        c = catalog.BY_ID[cid]
        store.save_source({"name": c["name"], "kind": "rss", "url": c["url"], "lang": c["lang"],
                           "interval_min": 0, "options": {}})
    return True


def setup_logging(verbose: bool, to_stderr: bool) -> None:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(store.LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("fontTools").setLevel(logging.WARNING)  # font subsetting logs a dozen lines per PDF
    if to_stderr and sys.stderr is not None:  # pythonw has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ------------------------------------------------------- pid / privilege helpers
def read_pid(path) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resolve_identity(args):
    """(uid, gid, username) when we are root and were asked to run as someone else, else None."""
    if not (args.user or args.group):
        return None
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise StartupError("--user/--group can only be used when started as root")
    import grp
    import pwd
    try:
        pw = pwd.getpwnam(args.user) if args.user else pwd.getpwuid(0)
    except KeyError:
        raise StartupError(f"user '{args.user}' does not exist (create it, or run FreeBSD's install.sh)") from None
    gid = pw.pw_gid
    if args.group:
        try:
            gid = grp.getgrnam(args.group).gr_gid
        except KeyError:
            raise StartupError(f"group '{args.group}' does not exist") from None
    return pw.pw_uid, gid, pw.pw_name


def prepare_data_dir(uid: int, gid: int) -> None:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for p in [store.DATA_DIR, *store.DATA_DIR.rglob("*")]:  # fix files left by an earlier run as root
        try:
            os.chown(p, uid, gid)
        except OSError:
            pass
    os.chmod(store.DATA_DIR, 0o750)


def drop_privileges(name: str, uid: int, gid: int) -> None:
    os.initgroups(name, gid)
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() == 0:
        raise StartupError("could not drop root privileges")
    # rc.d/daemon(8) do not set these reliably; fontconfig/Pango want a writable HOME for their caches
    os.environ.update({"HOME": str(store.DATA_DIR), "USER": name, "LOGNAME": name})


# ------------------------------------------------------------------ the service
def serve(args, notify=None, daemon=False) -> int:
    """Initialise everything, then serve until told to stop. `notify` reports readiness/errors to `start`."""
    ident = resolve_identity(args)
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ident:
        store.SHARED_GROUP = True  # config.json group-readable: UI users in the group can connect
        prepare_data_dir(ident[0], ident[1])
        os.umask(0o027)
        drop_privileges(ident[2], ident[0], ident[1])

    setup_logging(args.verbose, to_stderr=not daemon)
    cfg = store.load_config()
    changed = False
    for key in ("host", "port"):
        val = getattr(args, key)
        if val is not None and cfg.get(key) != val:
            cfg[key], changed = val, True
    if changed:
        store.save_config(cfg)
    store.init_db()
    fresh = seed_defaults()

    scheduler.CONFIG = store.CONFIG = cfg
    sched = scheduler.Scheduler(cfg)
    holder: dict = {}
    try:
        server = api.make_server(cfg, lambda: (sched.stop_event.set(), holder["server"].shutdown()))
    except OSError as exc:
        raise StartupError(f"cannot listen on {cfg['host']}:{cfg['port']}: {exc}. "
                           f"Is the service already running?") from None
    holder["server"] = server

    if not daemon:  # `start` writes the pidfile for daemons (it may be root; we may no longer be)
        pid_path = Path(args.pidfile) if args.pidfile else store.PID_PATH
        old = read_pid(pid_path)
        if old and old != os.getpid() and alive(old):
            server.server_close()
            raise StartupError(f"already running (pid {old}, see {pid_path})")
        try:
            pid_path.write_text(str(os.getpid()))
        except OSError as exc:
            log.warning("cannot write pidfile %s: %s", pid_path, exc)
    if notify:
        notify(f"OK {os.getpid()} {cfg['host']}:{cfg['port']}")

    sched.start()
    if fresh:  # first run: fetch the starter sources once so the News tab isn't empty
        threading.Thread(target=sources.refresh_all, daemon=True, name="first-fetch").start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    log.info("Newspaper service (pid %s) listening on http://%s:%s, data: %s", os.getpid(), cfg["host"],
             cfg["port"], store.DATA_DIR)
    try:
        server.serve_forever()
    finally:
        sched.stop_event.set()
        server.server_close()
        if not daemon:
            store.PID_PATH.unlink(missing_ok=True)
        log.info("service stopped")
    return 0


# ----------------------------------------------------------------- commands
def pidfile_path(args) -> Path:
    return Path(args.pidfile) if args.pidfile else store.PID_PATH


def cmd_run(args) -> int:
    try:
        return serve(args)
    except StartupError as exc:
        print(f"newspaper: {exc}", file=sys.stderr)
        return 1


def _wait_pipe(fd: int, timeout: float) -> str:
    chunks, end = [], time.time() + timeout
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            data = os.read(fd, 4096)
            if not data:
                break
            chunks.append(data)
    os.close(fd)
    return b"".join(chunks).decode(errors="replace").strip()


def cmd_start(args) -> int:
    pid_path = pidfile_path(args)
    old = read_pid(pid_path)
    if alive(old):
        print(f"already running (pid {old})")
        return 0
    if not POSIX:  # Windows: no fork, so launch a detached foreground copy
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", *sys.argv[2:]], close_fds=True,
                         creationflags=0x00000008 | 0x00000200, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("started")
        return 0

    r, w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    child = os.fork()
    if child == 0:  # ---- first child: become a session leader, then fork again
        os.close(r)
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        # ---- the daemon
        code = 1
        sent = False

        def notify(msg: str) -> None:
            nonlocal sent
            if not sent:
                sent = True
                os.write(w, msg.encode())
                os.close(w)
        try:
            os.chdir("/")
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            code = serve(args, notify=notify, daemon=True)
        except StartupError as exc:
            notify(f"ERR {exc}")
        except BaseException as exc:  # noqa: BLE001 - the launcher must hear about anything
            logging.getLogger("newspaper").exception("startup failed")
            notify(f"ERR {type(exc).__name__}: {exc}")
        os._exit(code)

    # ---- launcher: wait for the daemon to say it is up (or why not)
    os.close(w)
    os.waitpid(child, 0)
    msg = _wait_pipe(r, 40)
    if msg.startswith("OK"):
        _, pid, addr = msg.split()
        try:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(pid + "\n")
        except OSError as exc:
            print(f"warning: could not write pidfile {pid_path}: {exc}", file=sys.stderr)
        print(f"started (pid {pid}), listening on {addr}, data in {store.DATA_DIR}")
        return 0
    detail = msg[4:] if msg.startswith("ERR") else "the service exited without saying why"
    print(f"newspaper: failed to start: {detail}\n          see {store.LOG_PATH}", file=sys.stderr)
    return 1


def cmd_stop(args) -> int:
    pid_path = pidfile_path(args)
    pid = read_pid(pid_path)
    if not alive(pid):
        pid_path.unlink(missing_ok=True)
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):  # up to 20 s
        if not alive(pid):
            break
        time.sleep(0.2)
    else:
        print(f"newspaper: pid {pid} did not stop within 20 s", file=sys.stderr)
        return 1
    pid_path.unlink(missing_ok=True)
    print("stopped")
    return 0


def cmd_restart(args) -> int:
    return cmd_stop(args) or cmd_start(args)


def _read_config() -> dict | None:
    try:
        return json.loads(store.CONFIG_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def cmd_status(args) -> int:
    pid = read_pid(pidfile_path(args))
    if not alive(pid):
        print("not running")
        return 1
    line = f"running (pid {pid}), data in {store.DATA_DIR}"
    cfg = _read_config()
    if cfg:
        line += f", http://{cfg.get('host')}:{cfg.get('port')}"
    print(line)
    return 0


def cmd_token(args) -> int:
    cfg = _read_config()
    if not cfg:
        print(f"newspaper: cannot read {store.CONFIG_PATH} (not created yet, or no permission)", file=sys.stderr)
        return 1
    host = cfg.get("host", "127.0.0.1")
    print(f"URL:    http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{cfg.get('port')}")
    print(f"Token:  {cfg.get('token')}")
    print("Paste these into the UI: Service tab -> Connect...")
    return 0


COMMANDS = {"run": cmd_run, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart, "status": cmd_status,
            "token": cmd_token}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Newspaper background service", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="run", choices=list(COMMANDS))
    ap.add_argument("--data-dir", help="data folder (default: $NEWSPAPER_HOME or ~/.newspaper)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--user", help="run as this account (needs root)")
    ap.add_argument("--group", help="run with this group (needs root)")
    ap.add_argument("--pidfile")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.data_dir:
        store.set_data_dir(args.data_dir)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
