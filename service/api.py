"""Local JSON/HTTP API the UI talks to. Bound to 127.0.0.1 and protected by a token."""
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import catalog
import cron
import printing
import scheduler
import sources
import store

log = logging.getLogger("newspaper.api")
VERSION = "1.0"
STARTED = time.time()
CTX: dict = {"cfg": {}, "shutdown": lambda: None}


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Req:
    def __init__(self, handler, params, query):
        self.h, self.params, self.query = handler, params, query

    def json(self) -> dict:
        n = int(self.h.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.h.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "invalid JSON body") from None
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data


ROUTES: list = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


def _int(v, name="id") -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be a number") from None


def _limit_args(d: dict) -> tuple[int, str]:
    """Validated (max_pages, over_limit) from a job or print request."""
    try:
        pages = int(d.get("max_pages") or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "max_pages must be a number") from None
    if not 0 <= pages <= 10000:
        raise ApiError(400, "max_pages must be between 0 (no limit) and 10000")
    policy = d.get("over_limit") or "fit"
    if policy not in printing.OVER_LIMIT:
        raise ApiError(400, "over_limit must be one of: " + ", ".join(printing.OVER_LIMIT))
    return pages, policy


def _background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ------------------------------------------------------------------ status
@route("GET", r"/api/status")
def status(req):
    return {"version": VERSION, "uptime_s": int(time.time() - STARTED), "now": int(time.time()),
            "data_dir": str(store.DATA_DIR), "output_dir": str(store.OUTPUT_DIR),
            "counts": store.counts(), "printing": printing.capabilities(),
            "limits": {"max_pages_per_job": scheduler.page_ceiling(),
                       "retention_days": store.retention_days(CTX["cfg"]),
                       "history_days": store.history_days(CTX["cfg"])},
            "port": CTX["cfg"].get("port")}


# ---------------------------------------------------------------- settings
SETTINGS = ("retention_days", "history_days")


@route("GET", r"/api/settings")
def settings_get(req):
    return {"retention_days": store.retention_days(CTX["cfg"]), "history_days": store.history_days(CTX["cfg"])}


@route("PUT", r"/api/settings")
def settings_put(req):
    d, new = req.json(), {}
    for key in SETTINGS:
        if key not in d:
            continue
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= store.RETENTION_MAX_DAYS:
            raise ApiError(400, f"{key} must be a whole number of days from 1 to {store.RETENTION_MAX_DAYS}")
        new[key] = v
    if not new:
        raise ApiError(400, "nothing to change: send retention_days and/or history_days")
    cfg = CTX["cfg"]
    cfg.update(new)  # the scheduler and fetchers share this dict, so the change applies at once
    store.save_config(cfg)
    pruned = store.prune(store.retention_days(cfg), store.history_days(cfg))
    log.info("settings changed: %s; pruned %d articles", new, pruned)
    return {**settings_get(req), "pruned": pruned}


@route("POST", r"/api/shutdown")
def shutdown(req):
    _background(lambda: (time.sleep(0.3), CTX["shutdown"]()))
    return {"stopping": True}


@route("GET", r"/api/log")
def log_tail(req):
    n = _int(req.query.get("lines", 200), "lines")
    try:
        lines = store.LOG_PATH.read_text("utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        lines = []
    return {"lines": lines}


# ----------------------------------------------------------------- sources
def _source_payload(d: dict, old: dict | None = None) -> dict:
    name, kind, url = str(d.get("name", "")).strip(), d.get("kind"), str(d.get("url", "")).strip()
    if not name or not url:
        raise ApiError(400, "name and url are required")
    if kind not in ("rss", "github"):
        raise ApiError(400, "kind must be 'rss' or 'github'")
    opts = dict(d.get("options") or {})
    if old and old.get("options", {}).get("seen_files") and "seen_files" not in opts:
        opts["seen_files"] = old["options"]["seen_files"]
    return {"name": name, "kind": kind, "url": url, "lang": str(d.get("lang", "")),
            "enabled": bool(d.get("enabled", True)), "interval_min": max(0, _int(d.get("interval_min", 0), "interval_min")),
            "options": opts}


@route("GET", r"/api/sources")
def sources_list(req):
    return store.list_sources()


@route("POST", r"/api/sources")
def sources_create(req):
    sid = store.save_source(_source_payload(req.json()))
    _background(sources.refresh_source, sid)
    return store.get_source(sid)


@route("POST", r"/api/sources/test")
def sources_test(req):
    d = req.json()
    try:
        return sources.preview(d.get("kind", "rss"), str(d.get("url", "")).strip(), d.get("options") or {})
    except Exception as exc:
        raise ApiError(400, f"Test failed: {exc}") from None


@route("PUT", r"/api/sources/(?P<id>\d+)")
def sources_update(req):
    sid = _int(req.params["id"])
    old = store.get_source(sid)
    if not old:
        raise ApiError(404, "no such source")
    store.save_source(_source_payload(req.json(), old), sid)
    return store.get_source(sid)


@route("DELETE", r"/api/sources/(?P<id>\d+)")
def sources_delete(req):
    store.delete_source(_int(req.params["id"]))
    return {"deleted": True}


@route("POST", r"/api/refresh")
def refresh(req):
    d = req.json()
    if d.get("source_id"):
        _background(sources.refresh_source, _int(d["source_id"]), bool(d.get("force")))
    else:
        _background(sources.refresh_all, bool(d.get("force")))
    return {"started": True}


# ---------------------------------------------------------------- articles
@route("GET", r"/api/articles")
def articles_list(req):
    try:
        items, total = store.query_articles(req.query, limit=min(_int(req.query.get("limit", 40), "limit"), 500),
                                            offset=_int(req.query.get("offset", 0), "offset"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return {"items": items, "total": total}


@route("GET", r"/api/articles/(?P<id>\d+)")
def articles_get(req):
    a = store.get_article(_int(req.params["id"]))
    if not a:
        raise ApiError(404, "no such article")
    return a


@route("GET", r"/api/categories")
def categories(req):
    return store.categories()


# --------------------------------------------------------------- templates
def _template_payload(d: dict):
    name = str(d.get("name", "")).strip()
    if not name:
        raise ApiError(400, "template name is required")
    return name, d.get("config") or {}


@route("GET", r"/api/templates")
def templates_list(req):
    return store.list_templates()


@route("GET", r"/api/templates/default")
def templates_default(req):
    return printing.DEFAULT_LAYOUT


@route("POST", r"/api/templates")
def templates_create(req):
    name, cfg = _template_payload(req.json())
    return store.get_template(store.save_template(name, cfg))


@route("PUT", r"/api/templates/(?P<id>\d+)")
def templates_update(req):
    tid = _int(req.params["id"])
    if not store.get_template(tid):
        raise ApiError(404, "no such template")
    name, cfg = _template_payload(req.json())
    store.save_template(name, cfg, tid)
    return store.get_template(tid)


@route("DELETE", r"/api/templates/(?P<id>\d+)")
def templates_delete(req):
    tid = _int(req.params["id"])
    t = store.get_template(tid)
    if t and t["is_default"]:
        raise ApiError(400, "This is the default layout. Make another layout the default first.")
    store.delete_template(tid)
    return {"deleted": True}


@route("POST", r"/api/templates/(?P<id>\d+)/default")
def templates_set_default(req):
    tid = _int(req.params["id"])
    if not store.get_template(tid):
        raise ApiError(404, "no such template")
    store.set_default_template(tid)
    return store.get_template(tid)


# ----------------------------------------------------------------- catalog
@route("GET", r"/api/catalog")
def catalog_list(req):
    have = {catalog.norm(s["url"]) for s in store.list_sources()}
    return [{**c, "added": catalog.norm(c["url"]) in have} for c in catalog.CATALOG]


@route("POST", r"/api/catalog/add")
def catalog_add(req):
    have = {catalog.norm(s["url"]) for s in store.list_sources()}
    added = skipped = 0
    for cid in req.json().get("ids", []):
        c = catalog.BY_ID.get(cid)
        if not c or catalog.norm(c["url"]) in have:
            skipped += 1
            continue
        sid = store.save_source({"name": c["name"], "kind": "rss", "url": c["url"], "lang": c["lang"],
                                 "interval_min": 0, "options": {}})
        have.add(catalog.norm(c["url"]))
        _background(sources.refresh_source, sid)
        added += 1
    return {"added": added, "skipped": skipped}


@route("POST", r"/api/catalog/check")
def catalog_check(req):
    ids = [i for i in req.json().get("ids", []) if i in catalog.BY_ID][:60]

    def one(cid):
        try:
            r = sources.preview("rss", catalog.BY_ID[cid]["url"], {})
            return cid, {"ok": r["count"] > 0, "count": r["count"], "error": "" if r["count"] else "feed is empty"}
        except Exception as exc:
            return cid, {"ok": False, "count": 0, "error": str(exc)[:120]}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, ids))


# ----------------------------------------------------------- render / print
def _layout_from(d: dict) -> dict:
    if d.get("layout"):
        return d["layout"]
    return scheduler.resolve_layout(d.get("template_id"))


@route("POST", r"/api/render")
def render(req):
    d = req.json()
    f = d.get("filters") or {}
    try:
        items, _ = store.query_articles(f, limit=max(int(f.get("limit") or 30), len(f.get("article_ids") or [])),
                                        with_content=True)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return {"html": printing.render(items, _layout_from(d), print_dialog=bool(d.get("print_dialog"))),
            "count": len(items)}


@route("POST", r"/api/print")
def print_now(req):
    d = req.json()
    action = d.get("action", "print")
    if action not in ("print", "save"):
        raise ApiError(400, "action must be 'print' or 'save'")
    pages, policy = _limit_args(d)
    return scheduler.execute(d.get("name") or "Manual print", _layout_from(d), d.get("filters") or {}, action,
                             str(d.get("printer", "")), bool(d.get("refresh_first", False)), None, "manual",
                             pages, policy)


# -------------------------------------------------------------------- jobs
def _job_payload(d: dict) -> dict:
    name, expr = str(d.get("name", "")).strip(), str(d.get("cron", "")).strip()
    if not name:
        raise ApiError(400, "job name is required")
    try:
        cron.parse_cron(expr)
        cron.next_run(expr, datetime.now())
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    if d.get("action", "print") not in ("print", "save"):
        raise ApiError(400, "action must be 'print' or 'save'")
    try:
        store.category_list(d.get("filters") or {})
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    tid = d.get("template_id")
    if tid and not store.get_template(_int(tid, "template_id")):
        raise ApiError(400, "template does not exist")
    pages, policy = _limit_args(d)
    return {**d, "name": name, "cron": expr, "template_id": tid or None, "max_pages": pages, "over_limit": policy}


@route("GET", r"/api/jobs")
def jobs_list(req):
    return store.list_jobs()


@route("POST", r"/api/jobs")
def jobs_create(req):
    p = _job_payload(req.json())
    jid = store.save_job(p)
    scheduler.schedule_job(jid)
    return store.get_job(jid)


@route("PUT", r"/api/jobs/(?P<id>\d+)")
def jobs_update(req):
    jid = _int(req.params["id"])
    if not store.get_job(jid):
        raise ApiError(404, "no such job")
    store.save_job(_job_payload(req.json()), jid)
    scheduler.schedule_job(jid)
    return store.get_job(jid)


@route("DELETE", r"/api/jobs/(?P<id>\d+)")
def jobs_delete(req):
    store.delete_job(_int(req.params["id"]))
    return {"deleted": True}


@route("POST", r"/api/jobs/(?P<id>\d+)/run")
def jobs_run(req):
    jid = _int(req.params["id"])
    if not store.get_job(jid):
        raise ApiError(404, "no such job")
    _background(scheduler.run_job, jid, "manual")
    return {"started": True}


@route("GET", r"/api/runs")
def runs(req):
    jid = req.query.get("job_id")
    return store.list_runs(_int(req.query.get("limit", 50), "limit"), _int(jid) if jid else None)


@route("GET", r"/api/cron/preview")
def cron_preview(req):
    try:
        times = cron.preview(req.query.get("expr", ""), _int(req.query.get("count", 3), "count"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return {"next": [int(t.timestamp()) for t in times], "text": [t.strftime("%a %d %b %Y %H:%M") for t in times]}


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "Newspaper/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        try:
            token = self.headers.get("X-Newspaper-Token", "")
            if not hmac.compare_digest(token, CTX["cfg"].get("token", "")):
                raise ApiError(401, "missing or wrong token")
            u = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            for m, rx, fn in ROUTES:
                mo = rx.fullmatch(u.path)
                if m == method and mo:
                    return self._send(200, fn(Req(self, mo.groupdict(), query)))
            raise ApiError(404, f"no route for {method} {u.path}")
        except ApiError as exc:
            self._send(exc.code, {"error": exc.msg})
        except sqlite3.IntegrityError as exc:
            self._send(409, {"error": f"conflict: {exc}"})
        except Exception as exc:
            log.exception("API error")
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(cfg: dict, shutdown_cb) -> Server:
    CTX["cfg"], CTX["shutdown"] = cfg, shutdown_cb
    return Server((cfg["host"], int(cfg["port"])), Handler)
