"""Background scheduler: periodic feed refresh + cron-driven print jobs."""
import logging
import threading
import time
from datetime import datetime

import cron
import printing
import sources
import store

log = logging.getLogger("newspaper.scheduler")
_running_jobs: set[int] = set()
_lock = threading.Lock()
CONFIG: dict = {}  # set by the service at start (config.json)


def page_ceiling() -> int:
    """Service-wide maximum pages per print job (config.json "max_pages_per_job"); 0 = none."""
    try:
        return max(0, int(CONFIG.get("max_pages_per_job") or 0))
    except (TypeError, ValueError):
        return 0


def effective_limit(job_limit: int) -> tuple[int, bool]:
    """(pages allowed, True if the service-wide ceiling is what applies)."""
    job_limit, ceiling = max(0, int(job_limit or 0)), page_ceiling()
    if job_limit and ceiling:
        return min(job_limit, ceiling), ceiling < job_limit
    return (ceiling, True) if ceiling else (job_limit, False)


def next_ts(expr: str, after: datetime | None = None) -> int:
    return int(cron.next_run(expr, after or datetime.now()).timestamp())


def schedule_job(job_id: int) -> None:
    job = store.get_job(job_id)
    store.update_job_state(job_id, next_run=next_ts(job["cron"]) if job and job["enabled"] else None)


def resolve_layout(template_id) -> dict:
    """The chosen layout; if none (or it was deleted) the layout marked as default."""
    t = (store.get_template(template_id) if template_id else None) or store.get_default_template()
    return t["config"] if t else {}


def _fetch_summary(filters: dict) -> tuple[str, list[str]]:
    """Fetch the sources a job uses (enabled ones only), in parallel. Returns (summary, failures)."""
    names = {s["id"]: s["name"] for s in store.list_sources() if s["enabled"]}
    wanted = [int(i) for i in (filters.get("source_ids") or names)]
    ids = [i for i in wanted if i in names]
    if not ids:
        return "no enabled sources to fetch", []
    res = sources.refresh_many(ids)
    ok = [i for i, r in res.items() if r.get("status") in ("ok", "busy")]
    added = sum(r.get("added", 0) for r in res.values())
    failed = [f"{names[i]}: {str(r.get('error', ''))[:80]}" for i, r in res.items() if r.get("status") == "error"]
    return f"fetched {len(ok)}/{len(ids)} sources, {added} new articles", failed


def execute(name, layout, filters, action, printer, refresh_first, job_id=None, trigger="manual",
            max_pages=0, over_limit="fit") -> dict:
    """One print run: fetch sources -> select -> render -> save/print, recorded in job_runs."""
    run_id = store.start_run(job_id, name, trigger)
    status, message, output, result = "error", "", "", {}
    try:
        fetch_note, failures = (_fetch_summary(filters) if refresh_first and not filters.get("article_ids")
                                else ("", []))
        limit = int(filters.get("limit") or 30)
        if filters.get("article_ids"):
            limit = max(limit, len(filters["article_ids"]))
        items, _ = store.query_articles(filters, limit=limit, with_content=True)
        if not items:
            status, message = "skipped", "No articles matched the filters, nothing printed."
        else:
            limit, by_service = effective_limit(max_pages)
            result = printing.produce(items, layout, name, action, printer, limit, over_limit)
            if by_service and result.get("limited"):
                result["message"] += "  [service-wide page limit]"
            output = result.get("pdf") or result.get("html") or ""
            status = "saved" if (action == "print" and not result["printed"]) else "ok"
            message = result["message"]
        if fetch_note:
            message += f"  [{fetch_note}]"
        if failures:
            more = f"; +{len(failures) - 3} more" if len(failures) > 3 else ""
            message += "  [failed: " + "; ".join(failures[:3]) + more + "]"
    except Exception as exc:
        log.exception("run '%s' failed", name)
        status, message = "error", str(exc)
    store.finish_run(run_id, status, message, output)
    if job_id:
        store.update_job_state(job_id, last_run=int(time.time()), last_status=status,
                               last_message=message, last_output=output)
    return {"run_id": run_id, "status": status, "message": message, "output": output,
            "html": result.get("html"), "pdf": result.get("pdf"), "printed": result.get("printed", False),
            "count": result.get("count", 0), "pages": result.get("pages"), "limited": result.get("limited")}


def run_job(job_id: int, trigger="schedule") -> dict:
    with _lock:
        if job_id in _running_jobs:
            return {"status": "busy", "message": "job is already running"}
        _running_jobs.add(job_id)
    try:
        job = store.get_job(job_id)
        if not job:
            return {"status": "error", "message": "no such job"}
        store.update_job_state(job_id, last_status="running", last_message="")
        return execute(job["name"], resolve_layout(job["template_id"]), job["filters"], job["action"],
                       job["printer"], job["refresh_first"], job_id, trigger, job["max_pages"], job["over_limit"])
    finally:
        with _lock:
            _running_jobs.discard(job_id)


class Scheduler(threading.Thread):
    def __init__(self, cfg: dict):
        super().__init__(daemon=True, name="scheduler")
        self.cfg, self.stop_event, self.last_prune = cfg, threading.Event(), 0.0

    def run(self):
        store.reset_stale()
        for j in store.list_jobs():
            if j["enabled"] and not j["next_run"]:
                try:
                    schedule_job(j["id"])
                except ValueError as exc:
                    log.warning("job %s has a bad schedule: %s", j["id"], exc)
        log.info("scheduler started")
        while not self.stop_event.wait(15):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    def tick(self):
        now = time.time()
        for s in store.list_sources():
            if s["interval_min"] <= 0:  # 0 = on demand: fetched only by print jobs / manual refresh
                continue
            due = s["last_fetch"] is None or now - s["last_fetch"] >= s["interval_min"] * 60
            if s["enabled"] and due and s["last_status"] != "running":
                threading.Thread(target=sources.refresh_source, args=(s["id"],), daemon=True).start()

        grace = int(self.cfg.get("misfire_grace_s", 600))
        for j in store.list_jobs():
            if not j["enabled"] or not j["next_run"] or j["next_run"] > now:
                continue
            try:
                store.update_job_state(j["id"], next_run=next_ts(j["cron"]))
            except ValueError as exc:
                store.update_job_state(j["id"], enabled=0, last_status="error", last_message=str(exc))
                continue
            if now - j["next_run"] > grace:
                msg = "Skipped: the service was not running at the scheduled time."
                store.finish_run(store.start_run(j["id"], j["name"], "schedule"), "skipped", msg)
                store.update_job_state(j["id"], last_run=int(now), last_status="skipped", last_message=msg)
            else:
                threading.Thread(target=run_job, args=(j["id"], "schedule"), daemon=True).start()

        if now - self.last_prune > 6 * 3600:
            self.last_prune = now
            n = store.prune(store.retention_days(self.cfg), store.history_days(self.cfg))
            if n:
                log.info("pruned %d old articles", n)
