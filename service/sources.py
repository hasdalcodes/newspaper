"""Feed grabbers: generic RSS/Atom, and the Al Jazeera scraper's JSON files stored on GitHub."""
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import concurrent.futures as cf

import requests

import store

log = logging.getLogger("newspaper.sources")
UA = "Mozilla/5.0 (compatible; Newspaper/1.0)"
_running: set[int] = set()
_lock = threading.Lock()


def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


# ------------------------------------------------------------ text helpers
class _Stripper(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n" if tag == "br" else "\n\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def strip_html(s: str) -> str:
    """HTML -> plain text, keeping paragraph breaks."""
    if not s:
        return ""
    p = _Stripper()
    try:
        p.feed(s)
        p.close()
    except Exception:
        return " ".join(re.sub(r"<[^>]+>", " ", s).split())
    text = re.sub(r"[ \t\r\f\v\u00a0]+", " ", "".join(p.out))
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def one_line(s: str, limit=600) -> str:
    t = " ".join(strip_html(s).split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def detect_lang(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return ""
    arabic = sum(1 for c in letters if "\u0600" <= c <= "\u06ff")
    latin = sum(1 for c in letters if c.isascii())
    if arabic / len(letters) > 0.4:
        return "ar"
    return "en" if latin / len(letters) > 0.8 else ""


def parse_date(s) -> int | None:
    if not s:
        return None
    s = str(s).strip()
    dt = None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class _TextGrabber(HTMLParser):
    """Very small 'readability': collect <p> text, preferring paragraphs inside <article>."""
    SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "form", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.article, self.p = 0, 0, None
        self.in_article, self.anywhere = [], []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "article":
            self.article += 1
        elif tag == "p" and not self.skip:
            self.p = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "article":
            self.article = max(0, self.article - 1)
        elif tag == "p" and self.p is not None:
            text = " ".join("".join(self.p).split())
            if len(text) > 40:
                (self.in_article if self.article else self.anywhere).append(text)
            self.p = None

    def handle_data(self, data):
        if self.p is not None and not self.skip:
            self.p.append(data)


def fetch_full_text(url: str) -> str:
    r = http_get(url, timeout=20)
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    g = _TextGrabber()
    g.feed(r.text)
    g.close()
    return "\n\n".join(g.in_article or g.anywhere)


# ------------------------------------------------------------- RSS / Atom
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_feed(data: bytes) -> list[dict]:
    """Parse RSS 2.0, RSS 1.0 (RDF) and Atom into plain dicts."""
    root = ET.fromstring(data.lstrip())
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        f: dict = {}
        for ch in el:
            name, text = _local(ch.tag), (ch.text or "").strip()
            if name == "link":
                href = ch.get("href")
                if href and (ch.get("rel", "alternate") == "alternate" or "link" not in f):
                    f["link"] = href
                elif text:
                    f.setdefault("link", text)
            elif name in ("author", "creator"):
                nm = ch.find("{*}name")
                val = (nm.text or "").strip() if nm is not None else text
                if val:
                    f.setdefault("author", val)
            elif name == "category":
                val = ch.get("term") or text or ch.get("label") or ""
                if val.strip():
                    f.setdefault("category", val.strip())
            elif name in ("title", "description", "summary", "encoded", "content", "guid",
                          "pubDate", "published", "updated", "date") and text:
                f.setdefault(name, text)
        link = f.get("link") or (f.get("guid") if str(f.get("guid", "")).startswith("http") else "")
        if not link:
            continue
        out.append({
            "title": f.get("title", ""),
            "url": link,
            "summary_raw": f.get("description") or f.get("summary") or "",
            "content_raw": f.get("encoded") or f.get("content") or "",
            "author": f.get("author", ""),
            "category": f.get("category", ""),
            "published": parse_date(f.get("pubDate") or f.get("published") or f.get("date") or f.get("updated")),
        })
    return out


def _normalize_rss(it: dict, src: dict) -> dict:
    title = one_line(it["title"], 300)
    summary_raw = it["summary_raw"]
    content_raw = it["content_raw"] or (summary_raw if len(summary_raw) > 700 else "")
    summary = one_line(summary_raw or content_raw)
    return {
        "title": title, "url": it["url"], "summary": summary, "content": strip_html(content_raw),
        "author": it["author"], "category": it["category"], "published": it["published"],
        "lang": src.get("lang") or detect_lang(title + " " + summary),
    }


def too_old(items: list[dict], cutoff: int) -> list[dict]:
    """Items whose published date is before `cutoff` (items without a date are never too old)."""
    return [i for i in items if i.get("published") and i["published"] < cutoff]


def fetch_rss(src: dict) -> list[dict]:
    items = [_normalize_rss(i, src) for i in parse_feed(http_get(src["url"]).content)]
    old = {id(i) for i in too_old(items, store.retention_cutoff())}  # no full text for articles we won't keep
    opts = src.get("options", {})
    if opts.get("fetch_full"):
        have = store.urls_with_content([i["url"] for i in items])
        todo = [i for i in items if i["url"] not in have and not i["content"] and id(i) not in old]
        for i in todo[: int(opts.get("max_full", 10))]:
            try:
                i["content"] = fetch_full_text(i["url"])
            except Exception as exc:  # one bad page must not fail the whole feed
                log.warning("full text failed for %s: %s", i["url"], exc)
            time.sleep(1)
    return items


# ---------------------------------------------- GitHub (Al Jazeera scraper)
def _repo_slug(s: str) -> str:
    s = s.strip().rstrip("/")
    for p in ("https://github.com/", "http://github.com/", "github.com/"):
        if s.startswith(p):
            s = s[len(p):]
    return s.removesuffix(".git")


def _gh_list(src: dict):
    o = src.get("options", {})
    headers = {"Accept": "application/vnd.github+json"}
    if o.get("token"):
        headers["Authorization"] = f"Bearer {o['token']}"
    r = http_get(f"https://api.github.com/repos/{_repo_slug(src['url'])}/contents/{o.get('path', 'data')}"
                 f"?ref={o.get('branch', 'main')}", headers=headers)
    files = [f for f in r.json() if f.get("name", "").endswith(".json")]
    return sorted(files, key=lambda f: f["name"], reverse=True), headers


def fetch_github(src: dict, force=False):
    o = dict(src.get("options", {}))
    files, headers = _gh_list(src)
    files = files[: int(o.get("max_files", 8))]
    seen = set() if force else set(o.get("seen_files", []))
    new_files = [f for f in files if f["name"] not in seen]
    items = []
    for f in reversed(new_files):  # oldest first, so newer data wins on upsert
        data = http_get(f["download_url"], timeout=40, headers=headers).json()
        for a in data:
            title = one_line(a.get("title", ""), 300)
            summary = one_line(a.get("summary", ""))
            items.append({
                "title": title, "url": a.get("url", ""), "summary": summary,
                "content": a.get("content", ""), "author": a.get("author", ""),
                "category": a.get("category", ""),
                "published": parse_date(a.get("published")) or parse_date(a.get("scraped_at")),
                "lang": a.get("language") or src.get("lang") or detect_lang(title),
            })
    o["seen_files"] = (list(seen) + [f["name"] for f in new_files])[-300:]
    return items, o


def preview(kind: str, url: str, options: dict) -> dict:
    """Used by the UI's 'Test' button before saving a source."""
    if kind == "rss":
        items = parse_feed(http_get(url).content)
        return {"ok": True, "count": len(items), "titles": [one_line(i["title"], 100) for i in items[:5]]}
    files, _ = _gh_list({"url": url, "options": options})
    return {"ok": True, "count": len(files), "titles": [f["name"] for f in files[:5]]}


# ------------------------------------------------------------ orchestration
def refresh_source(sid: int, force=False) -> dict:
    with _lock:
        if sid in _running:
            return {"status": "busy"}
        _running.add(sid)
    try:
        src = store.get_source(sid)
        if not src:
            return {"status": "error", "error": "no such source"}
        store.set_source_state(sid, "running")
        opts = None
        if src["kind"] == "github":
            items, opts = fetch_github(src, force)
        else:
            items = fetch_rss(src)
        old = {id(i) for i in too_old(items, store.retention_cutoff())}  # would be pruned anyway
        added = store.upsert_articles(sid, [i for i in items if id(i) not in old])
        store.set_source_state(sid, "ok", "", opts)
        log.info("source %s (%s): %d fetched, %d new, %d older than %d days skipped", sid, src["name"],
                 len(items), added, len(old), store.retention_days())
        return {"status": "ok", "fetched": len(items), "added": added, "skipped_old": len(old)}
    except Exception as exc:
        log.warning("source %s failed: %s", sid, exc)
        store.set_source_state(sid, "error", str(exc)[:300])
        return {"status": "error", "error": str(exc)}
    finally:
        with _lock:
            _running.discard(sid)


def refresh_all(force=False) -> None:
    for s in store.list_sources():
        if s["enabled"]:
            refresh_source(s["id"], force)


def refresh_many(ids: list[int], deadline: float = 150.0, workers: int = 6) -> dict[int, dict]:
    """Fetch several sources in parallel. A source that is still busy when the deadline passes is reported
    as timed out, so one dead feed can never hold up a print job for long."""
    results: dict[int, dict] = {}
    if not ids:
        return results
    pool = cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(ids))), thread_name_prefix="fetch")
    futs = {pool.submit(refresh_source, sid): sid for sid in ids}
    done, pending = cf.wait(futs, timeout=deadline)
    for f in done:
        results[futs[f]] = f.result()
    for f in pending:
        results[futs[f]] = {"status": "error", "error": f"timed out after {int(deadline)} s"}
    pool.shutdown(wait=False, cancel_futures=True)
    return results
