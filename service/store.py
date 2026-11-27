"""Newspaper storage layer (SQLite) plus shared paths and config."""
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DATA_DIR = Path(os.getenv("NEWSPAPER_HOME", str(Path.home() / ".newspaper")))
DB_PATH = DATA_DIR / "newspaper.db"
CONFIG_PATH = DATA_DIR / "config.json"
OUTPUT_DIR = DATA_DIR / "prints"
LOG_PATH = DATA_DIR / "service.log"
PID_PATH = DATA_DIR / "service.pid"

DEFAULT_CONFIG = {"host": "127.0.0.1", "port": 8765,
                  "retention_days": 7,    # articles older than this (published date, else fetched) are deleted
                  "history_days": 90,     # job run history is kept this long
                  "misfire_grace_s": 600,
                  "max_pages_per_job": 0}  # service-wide ceiling for pages in any one print job; 0 = none
CONFIG: dict = {}  # the running service's config (set at start; the API updates it in place)
RETENTION_MAX_DAYS = 3650
SHARED_GROUP = False  # system-service mode: config/data readable by the service group (UI users join it)


def set_data_dir(path) -> None:
    """Point every path at another data folder (used by --data-dir). Always access these as store.X."""
    global DATA_DIR, DB_PATH, CONFIG_PATH, OUTPUT_DIR, LOG_PATH, PID_PATH
    DATA_DIR = Path(path).expanduser().absolute()
    DB_PATH, CONFIG_PATH = DATA_DIR / "newspaper.db", DATA_DIR / "config.json"
    OUTPUT_DIR, LOG_PATH, PID_PATH = DATA_DIR / "prints", DATA_DIR / "service.log", DATA_DIR / "service.pid"


def _chmod_config() -> None:
    try:
        os.chmod(CONFIG_PATH, 0o640 if SHARED_GROUP else 0o600)
    except OSError:
        pass


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")
    _chmod_config()


def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
        except ValueError:
            cfg = {}
    before = dict(cfg)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
    if cfg != before or not CONFIG_PATH.exists():
        save_config(cfg)
    _chmod_config()
    return cfg


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- 'rss' | 'github'
  url TEXT NOT NULL DEFAULT '',       -- feed URL, or owner/repo for github
  lang TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  interval_min INTEGER NOT NULL DEFAULT 0,   -- 0 = only when a job runs / on request
  options TEXT NOT NULL DEFAULT '{}',
  last_fetch INTEGER, last_status TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS articles(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  url TEXT NOT NULL UNIQUE,
  title TEXT, summary TEXT, content TEXT, author TEXT, category TEXT, lang TEXT,
  published INTEGER, fetched INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published);
CREATE INDEX IF NOT EXISTS idx_articles_when ON articles(COALESCE(published, fetched) DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE TABLE IF NOT EXISTS templates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  config TEXT NOT NULL DEFAULT '{}',
  is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  cron TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  template_id INTEGER REFERENCES templates(id) ON DELETE SET NULL,
  filters TEXT NOT NULL DEFAULT '{}',
  action TEXT NOT NULL DEFAULT 'print',      -- 'print' | 'save'
  printer TEXT NOT NULL DEFAULT '',
  refresh_first INTEGER NOT NULL DEFAULT 1,
  max_pages INTEGER NOT NULL DEFAULT 0,        -- 0 = no limit
  over_limit TEXT NOT NULL DEFAULT 'fit',      -- 'fit' | 'truncate' | 'skip'
  next_run INTEGER, last_run INTEGER, last_status TEXT, last_message TEXT, last_output TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS job_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  job_name TEXT NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'schedule',
  started INTEGER NOT NULL, finished INTEGER,
  status TEXT NOT NULL, message TEXT, output TEXT
);
"""


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        cols = [r[1] for r in c.execute("PRAGMA table_info(templates)")]
        if "is_default" not in cols:  # database from an older version
            c.execute("ALTER TABLE templates ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0")
        jcols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
        if "max_pages" not in jcols:  # database from an older version
            c.execute("ALTER TABLE jobs ADD COLUMN max_pages INTEGER NOT NULL DEFAULT 0")
        if "over_limit" not in jcols:
            c.execute("ALTER TABLE jobs ADD COLUMN over_limit TEXT NOT NULL DEFAULT 'fit'")
        if not c.execute("SELECT 1 FROM meta WHERE key='fetch_on_demand_v1'").fetchone():
            # sources used to poll every 30 min by default; they are now fetched when a print job runs
            c.execute("UPDATE sources SET interval_min=0 WHERE interval_min=30")
            c.execute("INSERT INTO meta(key,value) VALUES ('fetch_on_demand_v1','1')")
        c.commit()


def rows(sql, args=()) -> list[dict]:
    with closing(connect()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def row(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql, args=()) -> int:
    with closing(connect()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def jload(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


def reset_stale() -> None:
    """Called at service start: anything still marked 'running' was interrupted."""
    execute("UPDATE sources SET last_status='interrupted' WHERE last_status='running'")
    execute("UPDATE jobs SET last_status='interrupted' WHERE last_status='running'")
    execute("UPDATE job_runs SET status='interrupted', finished=? WHERE status='running'", (int(time.time()),))


# ---------------------------------------------------------------- sources
def _src(r):
    if r:
        r["options"] = jload(r.get("options"), {})
        r["enabled"] = bool(r["enabled"])
    return r


def list_sources() -> list[dict]:
    return [_src(r) for r in rows(
        "SELECT s.*, (SELECT COUNT(*) FROM articles a WHERE a.source_id=s.id) AS item_count "
        "FROM sources s ORDER BY s.id")]


def get_source(sid):
    return _src(row("SELECT * FROM sources WHERE id=?", (sid,)))


def save_source(d: dict, sid=None) -> int:
    vals = (d["name"], d["kind"], d["url"], d.get("lang", ""), 1 if d.get("enabled", True) else 0,
            max(0, int(d.get("interval_min", 0))), json.dumps(d.get("options") or {}))
    if sid is None:
        return execute("INSERT INTO sources(name,kind,url,lang,enabled,interval_min,options) "
                       "VALUES (?,?,?,?,?,?,?)", vals)
    execute("UPDATE sources SET name=?,kind=?,url=?,lang=?,enabled=?,interval_min=?,options=? WHERE id=?",
            vals + (sid,))
    return sid


def delete_source(sid) -> None:
    execute("DELETE FROM sources WHERE id=?", (sid,))


def set_source_state(sid, status, error="", options=None) -> None:
    if status == "running":
        execute("UPDATE sources SET last_status=?, last_error=? WHERE id=?", (status, error, sid))
    elif options is not None:
        execute("UPDATE sources SET last_fetch=?, last_status=?, last_error=?, options=? WHERE id=?",
                (int(time.time()), status, error, json.dumps(options), sid))
    else:
        execute("UPDATE sources SET last_fetch=?, last_status=?, last_error=? WHERE id=?",
                (int(time.time()), status, error, sid))


# --------------------------------------------------------------- articles
def source_label(a: dict) -> str:
    url = a.get("url", "")
    if "aljazeera.net" in url:
        return "Al Jazeera Arabic"
    if "aljazeera.com" in url:
        return "Al Jazeera English"
    return a.get("source_name") or "Unknown"


def upsert_articles(source_id: int, items: list[dict]) -> int:
    added, now = 0, int(time.time())
    with closing(connect()) as c:
        for it in items:
            url = (it.get("url") or "").strip()
            if not url:
                continue
            vals = [it.get("title") or "", it.get("summary") or "", it.get("content") or "",
                    it.get("author") or "", it.get("category") or "", it.get("lang") or ""]
            if c.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone() is None:
                c.execute("INSERT INTO articles(source_id,url,title,summary,content,author,category,lang,"
                          "published,fetched) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [source_id, url] + vals + [it.get("published"), now])
                added += 1
            else:
                c.execute("UPDATE articles SET title=COALESCE(NULLIF(?,''),title), "
                          "summary=COALESCE(NULLIF(?,''),summary), content=COALESCE(NULLIF(?,''),content), "
                          "author=COALESCE(NULLIF(?,''),author), category=COALESCE(NULLIF(?,''),category), "
                          "lang=COALESCE(NULLIF(?,''),lang), published=COALESCE(?,published) WHERE url=?",
                          vals + [it.get("published"), url])
        c.commit()
    return added


def urls_with_content(urls: list[str]) -> set[str]:
    if not urls:
        return set()
    out = set()
    with closing(connect()) as c:
        for i in range(0, len(urls), 400):
            chunk = urls[i:i + 400]
            q = "SELECT url FROM articles WHERE content<>'' AND url IN (%s)" % ",".join("?" * len(chunk))
            out.update(r[0] for r in c.execute(q, chunk).fetchall())
    return out


def category_list(f: dict):
    """The `categories` include-list of a filter, or None for "no category filter".
    "" in the list stands for articles that have no category. May arrive as a JSON string (query string)."""
    v = f.get("categories")
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            raise ValueError("categories must be a JSON list of category names") from None
    if not isinstance(v, list):
        raise ValueError("categories must be a list of category names")
    return [str(c) for c in v]


def _where(f: dict):
    where, args = [], []
    ids = [int(i) for i in (f.get("article_ids") or [])]
    if ids:
        where.append("a.id IN (%s)" % ",".join("?" * len(ids)))
        args += ids
    sids = [int(i) for i in (f.get("source_ids") or [])]
    if f.get("source_id"):
        sids.append(int(f["source_id"]))
    if sids:
        where.append("a.source_id IN (%s)" % ",".join("?" * len(sids)))
        args += sids
    if f.get("lang"):
        where.append("a.lang = ?")
        args.append(f["lang"])
    if f.get("category"):  # single category (older jobs / API users)
        where.append("a.category = ?")
        args.append(f["category"])
    cats = category_list(f)
    if cats is not None:
        named = [c for c in cats if c]
        parts = []
        if named:
            parts.append("a.category IN (%s)" % ",".join("?" * len(named)))
            args += named
        if "" in cats:
            parts.append("(a.category IS NULL OR a.category = '')")
        where.append("(" + " OR ".join(parts) + ")" if parts else "0")  # empty list = nothing selected
    hours = float(f.get("hours") or 0)
    if hours > 0:
        where.append("COALESCE(a.published, a.fetched) >= ?")
        args.append(int(time.time() - hours * 3600))
    kw = (f.get("keyword") or f.get("q") or "").strip()
    if kw:
        where.append("(a.title LIKE ? OR a.summary LIKE ?)")
        args += [f"%{kw}%"] * 2
    return (" WHERE " + " AND ".join(where)) if where else "", args


def query_articles(f: dict, limit=50, offset=0, with_content=False):
    w, args = _where(f)
    cols = ("a.id,a.source_id,a.url,a.title,a.summary,a.author,a.category,a.lang,a.published,a.fetched,"
            "s.name AS source_name,(a.content IS NOT NULL AND a.content<>'') AS has_content")
    if with_content:
        cols += ",a.content"
    base = f" FROM articles a JOIN sources s ON s.id=a.source_id{w}"
    total = row("SELECT COUNT(*) AS n" + base, args)["n"]
    items = rows(f"SELECT {cols}{base} ORDER BY COALESCE(a.published,a.fetched) DESC, a.id DESC "
                 f"LIMIT ? OFFSET ?", args + [int(limit), int(offset)])
    for it in items:
        it["label"] = source_label(it)
        it["has_content"] = bool(it["has_content"])
    return items, total


def get_article(aid):
    items, _ = query_articles({"article_ids": [aid]}, limit=1, with_content=True)
    return items[0] if items else None


def categories() -> list[dict]:
    """Known categories with article counts, biggest first. A row with category "" counts uncategorized articles."""
    out = rows("SELECT category, COUNT(*) AS n FROM articles WHERE category IS NOT NULL AND category<>'' "
               "GROUP BY category ORDER BY n DESC LIMIT 200")
    none = row("SELECT COUNT(*) AS n FROM articles WHERE category IS NULL OR category=''")["n"]
    if none:
        out.append({"category": "", "n": none})
    return out


def counts() -> dict:
    return {
        "sources": row("SELECT COUNT(*) AS n FROM sources")["n"],
        "articles": row("SELECT COUNT(*) AS n FROM articles")["n"],
        "jobs": row("SELECT COUNT(*) AS n FROM jobs")["n"],
    }


def _days(value, default: int) -> int:
    try:
        return min(RETENTION_MAX_DAYS, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def retention_days(cfg: dict | None = None) -> int:
    """How many days articles are kept (config.json "retention_days")."""
    cfg = CONFIG if cfg is None else cfg
    return _days(cfg.get("retention_days"), DEFAULT_CONFIG["retention_days"])


def history_days(cfg: dict | None = None) -> int:
    """How many days job run history is kept (config.json "history_days")."""
    cfg = CONFIG if cfg is None else cfg
    return _days(cfg.get("history_days"), DEFAULT_CONFIG["history_days"])


def retention_cutoff(cfg: dict | None = None) -> int:
    """Articles dated before this timestamp are too old to keep."""
    return int(time.time() - retention_days(cfg) * 86400)


def prune(days: int, history: int | None = None) -> int:
    """Delete articles older than `days` (published date, else fetched date) and job runs older than `history`
    days (default: the configured history_days). Returns the number of articles deleted."""
    cutoff = int(time.time() - days * 86400)
    run_cutoff = int(time.time() - (history if history is not None else history_days()) * 86400)
    with closing(connect()) as c:
        cur = c.execute("DELETE FROM articles WHERE COALESCE(published, fetched) < ?", (cutoff,))
        c.execute("DELETE FROM job_runs WHERE started < ?", (run_cutoff,))
        c.commit()
        return cur.rowcount


# -------------------------------------------------------------- templates
def _tpl(r):
    if r:
        r["config"] = jload(r.get("config"), {})
        r["is_default"] = bool(r.get("is_default"))
    return r


def list_templates():
    return [_tpl(r) for r in rows("SELECT * FROM templates ORDER BY id")]


def get_template(tid):
    return _tpl(row("SELECT * FROM templates WHERE id=?", (tid,)))


def get_default_template():
    return _tpl(row("SELECT * FROM templates WHERE is_default=1 ORDER BY id LIMIT 1"))


def set_default_template(tid) -> None:
    with closing(connect()) as c:
        c.execute("UPDATE templates SET is_default=0")
        c.execute("UPDATE templates SET is_default=1 WHERE id=?", (tid,))
        c.commit()


def save_template(name, config, tid=None) -> int:
    if tid is None:
        return execute("INSERT INTO templates(name,config) VALUES (?,?)", (name, json.dumps(config)))
    execute("UPDATE templates SET name=?, config=? WHERE id=?", (name, json.dumps(config), tid))
    return tid


def delete_template(tid) -> None:
    execute("DELETE FROM templates WHERE id=?", (tid,))


# ------------------------------------------------------------------- jobs
def _job(r):
    if r:
        r["filters"] = jload(r.get("filters"), {})
        r["enabled"] = bool(r["enabled"])
        r["refresh_first"] = bool(r["refresh_first"])
    return r


def list_jobs():
    return [_job(r) for r in rows(
        "SELECT j.*, t.name AS template_name FROM jobs j LEFT JOIN templates t ON t.id=j.template_id "
        "ORDER BY j.id")]


def get_job(jid):
    return _job(row("SELECT * FROM jobs WHERE id=?", (jid,)))


def save_job(d: dict, jid=None, next_run=None) -> int:
    vals = (d["name"], d["cron"], 1 if d.get("enabled", True) else 0, d.get("template_id"),
            json.dumps(d.get("filters") or {}), d.get("action", "print"), d.get("printer", ""),
            1 if d.get("refresh_first", True) else 0, int(d.get("max_pages") or 0), d.get("over_limit") or "fit",
            next_run)
    if jid is None:
        return execute("INSERT INTO jobs(name,cron,enabled,template_id,filters,action,printer,refresh_first,"
                       "max_pages,over_limit,next_run) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
    execute("UPDATE jobs SET name=?,cron=?,enabled=?,template_id=?,filters=?,action=?,printer=?,"
            "refresh_first=?,max_pages=?,over_limit=?,next_run=? WHERE id=?", vals + (jid,))
    return jid


_JOB_STATE = {"next_run", "last_run", "last_status", "last_message", "last_output", "enabled"}


def update_job_state(jid, **kw) -> None:
    kw = {k: v for k, v in kw.items() if k in _JOB_STATE}
    if kw:
        execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in kw) + " WHERE id=?", list(kw.values()) + [jid])


def delete_job(jid) -> None:
    execute("DELETE FROM jobs WHERE id=?", (jid,))


def start_run(job_id, job_name, trigger) -> int:
    return execute("INSERT INTO job_runs(job_id,job_name,trigger,started,status) VALUES (?,?,?,?,'running')",
                   (job_id, job_name, trigger, int(time.time())))


def finish_run(run_id, status, message, output="") -> None:
    execute("UPDATE job_runs SET finished=?, status=?, message=?, output=? WHERE id=?",
            (int(time.time()), status, message, output, run_id))


def list_runs(limit=50, job_id=None):
    if job_id:
        return rows("SELECT * FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit))
    return rows("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))
