"""Customisable HTML print pages, HTML->PDF conversion (WeasyPrint) and sending to a printer."""
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import store

log = logging.getLogger("newspaper.printing")
WIN = sys.platform.startswith("win")
BSD = sys.platform.startswith(("freebsd", "openbsd", "netbsd", "dragonfly"))
FREEBSD = sys.platform.startswith("freebsd")  # CUPS there installs its client as `lpr-cups` (base `lpr` is BSD's own)
NO_WINDOW = {"creationflags": 0x08000000} if WIN else {}
# cron / rc.d / launchd start programs with a minimal PATH, so look in the usual extra places too
EXTRA_PATHS = ["/usr/local/bin", "/usr/local/sbin", "/opt/homebrew/bin", "/usr/bin", "/usr/sbin", "/bin", "/sbin"]


def which(name: str) -> str | None:
    path = os.environ.get("PATH", "")
    if not WIN:
        have = path.split(os.pathsep)
        path = os.pathsep.join([path] + [p for p in EXTRA_PATHS if p not in have])
    return shutil.which(name, path=path)


ALL_FIELDS = ["title", "source", "datetime", "category", "author", "summary", "content", "url"]
PAPERS = {"A3", "A4", "A5", "Letter", "Legal"}

# ---------------------------------------------------------------- default HTML
# Placeholders: {name} is always replaced. [[if:name]]...[[/if]] keeps its block only when that field is
# ticked in the layout AND has a value ("meta" = source / date / category / author line).
DEFAULT_CARD_TEMPLATE = """<article class="card" dir="{dir}">
  [[if:title]]<h2>{title}</h2>[[/if]]
  [[if:meta]]<div class="meta">{meta}</div>[[/if]]
  [[if:summary]]<p class="summary">{summary}</p>[[/if]]
  [[if:content]]<div class="content">{content}</div>[[/if]]
  [[if:url]]<div class="url">{url}</div>[[/if]]
</article>"""

DEFAULT_HEADER_TEMPLATE = """<h1>{title}</h1>
<div class="sub">{subtitle}</div>"""

DEFAULT_FOOTER_TEMPLATE = ""

DEFAULT_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<header class="page-header">
{header}
</header>
<main class="grid">
{cards}
</main>
<footer class="page-footer">{footer}</footer>
{script}
</body>
</html>"""

DEFAULT_LAYOUT = {
    "title": "News Digest",
    "subtitle": "{date} · {count} articles",
    "fields": ["title", "source", "datetime", "summary"],
    "columns": 2,
    "paper": "A4",
    "orientation": "portrait",
    "margin_mm": 12,
    "font_family": "'Segoe UI', Tahoma, 'Noto Naskh Arabic', Arial, sans-serif",
    "font_size_pt": 10,
    "accent": "#d4881c",
    "card_border": True,
    "page_break_each": False,
    "summary_chars": 260,
    "content_chars": 0,           # 0 = full text
    "date_format": "%d %b %Y, %H:%M",
    "timezone": "",               # empty = this computer's timezone
    "page_template": DEFAULT_PAGE_TEMPLATE,   # whole document; needs {cards}
    "card_template": DEFAULT_CARD_TEMPLATE,   # one article
    "header_html": DEFAULT_HEADER_TEMPLATE,   # placeholders {title} {subtitle} {date} {time} {count}
    "footer_html": DEFAULT_FOOTER_TEMPLATE,
    "custom_css": "",             # appended after the generated CSS
}
_TEXT_DEFAULTS = {"page_template": DEFAULT_PAGE_TEMPLATE, "card_template": DEFAULT_CARD_TEMPLATE,
                  "header_html": DEFAULT_HEADER_TEMPLATE}


def merge_layout(user: dict | None) -> dict:
    layout = {**DEFAULT_LAYOUT, **(user or {})}
    if not isinstance(layout["fields"], list):
        layout["fields"] = list(DEFAULT_LAYOUT["fields"])
    for key, default in _TEXT_DEFAULTS.items():  # an empty editor means "use the default HTML"
        if not str(layout.get(key) or "").strip():
            layout[key] = default
    if "{cards}" not in layout["page_template"]:
        layout["page_template"] = DEFAULT_PAGE_TEMPLATE
    return layout


# ------------------------------------------------------------------- rendering
def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def is_rtl(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum("\u0590" <= c <= "\u08ff" for c in letters) / len(letters) > 0.4


def fmt_ts(ts, fmt: str, tz: str = "") -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts, ZoneInfo(tz)) if tz else datetime.fromtimestamp(ts).astimezone()
        return dt.strftime(fmt)
    except Exception:
        return datetime.fromtimestamp(ts).strftime(fmt)


def fill(text: str, mapping: dict) -> str:
    """Replace {name} placeholders; unknown names (and CSS braces) are left untouched."""
    return re.sub(r"\{(\w+)\}", lambda m: str(mapping.get(m.group(1), m.group(0))), text or "")


def apply_conditionals(tpl: str, values: dict, fields: list[str]) -> str:
    def keep(name: str) -> bool:
        if name == "meta":
            return bool(values.get("meta"))
        return name in fields and bool(values.get(name))
    return re.sub(r"\[\[if:(\w+)\]\](.*?)\[\[/if\]\]",
                  lambda m: m.group(2) if keep(m.group(1)) else "", tpl, flags=re.S)


def _cut(s: str, limit: int) -> str:
    s = s or ""
    return s if not limit or len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def card_values(a: dict, L: dict, index: int) -> dict:
    summary = _cut(a.get("summary", ""), int(L["summary_chars"]))
    content = _cut(a.get("content", ""), int(L["content_chars"]))
    paras = "".join(f"<p>{esc(p.strip())}</p>" for p in re.split(r"\n\s*\n", content) if p.strip())
    v = {
        "title": esc(a.get("title")), "source": esc(a.get("label") or a.get("source_name")),
        "datetime": esc(fmt_ts(a.get("published") or a.get("fetched"), L["date_format"], L["timezone"])),
        "category": esc(a.get("category")), "author": esc(a.get("author")),
        "summary": esc(summary), "content": paras, "url": esc(a.get("url")),
        "dir": "rtl" if is_rtl((a.get("title") or "") + " " + summary) else "ltr", "index": index,
    }
    v["meta"] = " · ".join(v[k] for k in ("source", "datetime", "category", "author")
                           if k in L["fields"] and v[k])
    return v


def build_css(L: dict) -> str:
    paper = L["paper"] if L["paper"] in PAPERS else "A4"
    orient = "landscape" if L["orientation"] == "landscape" else "portrait"
    cols = max(1, min(4, int(L["columns"])))
    long_cards = "content" in L["fields"]
    border = "1px solid #cfcfcf" if L["card_border"] else "none"
    return f"""
@page {{ size: {paper} {orient}; margin: {int(L['margin_mm'])}mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: {L['font_family']}; font-size: {float(L['font_size_pt'])}pt; color: #111; margin: 0; }}
@media screen {{ body {{ max-width: 1000px; margin: 0 auto; padding: 14px; background: #fff; }} }}
.page-header {{ border-bottom: 3px solid {L['accent']}; margin-bottom: 7mm; padding-bottom: 3mm; }}
.page-header h1 {{ margin: 0; font-size: 1.8em; }}
.page-header .sub {{ color: #555; font-size: .9em; margin-top: 1mm; }}
.grid {{ column-count: {1 if L['page_break_each'] else cols}; column-gap: 8mm; }}
.card {{ break-inside: {'auto' if long_cards else 'avoid'}; margin: 0 0 5mm; padding: 3mm;
        border: {border}; border-radius: 2mm; {'break-after: page;' if L['page_break_each'] else ''} }}
.card:last-child {{ break-after: auto; }}  /* no blank page after the last article */
.card h2 {{ margin: 0 0 1.5mm; font-size: 1.15em; line-height: 1.35; }}
.meta {{ color: #666; font-size: .8em; margin-bottom: 2mm; }}
.summary {{ margin: 0; line-height: 1.5; }}
.content p {{ margin: 0 0 2mm; line-height: 1.55; }}
.url {{ font-size: .7em; color: #555; word-break: break-all; margin-top: 2mm; }}
.page-footer {{ margin-top: 6mm; color: #666; font-size: .8em; }}
{L['custom_css']}
"""


PRINT_SCRIPT = "<script>window.addEventListener('load',()=>setTimeout(()=>window.print(),400));</script>"


def render(articles: list[dict], layout: dict | None, print_dialog=False) -> str:
    L = merge_layout(layout)
    now = datetime.now()
    info = {"title": esc(L["title"]), "date": now.strftime("%A %d %B %Y"), "time": now.strftime("%H:%M"),
            "count": len(articles)}
    info["subtitle"] = esc(fill(L["subtitle"], info))

    cards = []
    for i, a in enumerate(articles, 1):
        v = card_values(a, L, i)
        cards.append(fill(apply_conditionals(L["card_template"], v, L["fields"]), v))

    script = PRINT_SCRIPT if print_dialog else ""
    page = L["page_template"]
    if script and "{script}" not in page:
        page = re.sub(r"</body>", lambda _m: "{script}</body>", page, count=1, flags=re.I) \
            if re.search(r"</body>", page, re.I) else page + "{script}"
    return fill(page, {**info, "css": build_css(L), "header": fill(L["header_html"], info),
                       "cards": "\n".join(cards), "footer": fill(L["footer_html"], info), "script": script})


# ------------------------------------------------------------- PDF + print
# PDF conversion uses WeasyPrint only (a Python HTML/CSS engine; no browser involved).
OVER_LIMIT = ("fit", "truncate", "skip")  # what to do with a job that would use more pages than allowed
FIT_DEADLINE = 120.0  # seconds the "fit" search may take before falling back to cutting pages
_engine: dict = {}
_pdf_lock = threading.Lock()  # one conversion at a time: Pango/fontconfig are not happy with parallel renders


def pdf_hint() -> str:
    if WIN:
        return "pip install weasyprint  (Windows also needs the GTK3/Pango runtime, see the WeasyPrint docs)"
    if BSD:
        return f"pkg install py{sys.version_info.major}{sys.version_info.minor}-weasyprint  (needs Pango)"
    return "pip install weasyprint  (needs the Pango library, e.g. apt install libpango-1.0-0)"


def load_engine() -> dict:
    """Import WeasyPrint lazily. A failed import is retried after 30 s so installing it later just works."""
    global _engine
    if _engine and (_engine.get("module") or time.time() - _engine["tried"] < 30):
        return _engine
    try:
        import weasyprint
        _engine = {"module": weasyprint, "name": "WeasyPrint", "version": getattr(weasyprint, "__version__", "?"),
                   "error": "", "tried": time.time()}
    except Exception as exc:  # ImportError, or OSError when the Pango library is missing
        first = (str(exc).strip().splitlines() or [""])[0][:200]
        _engine = {"module": None, "error": f"{type(exc).__name__}: {first}", "tried": time.time()}
    return _engine


def find_sumatra() -> str | None:
    if shutil.which("SumatraPDF"):
        return shutil.which("SumatraPDF")
    for base in filter(None, (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA"))):
        p = Path(base) / "SumatraPDF" / "SumatraPDF.exe"
        if p.exists():
            return str(p)
    return None


def capabilities() -> dict:
    eng = load_engine()
    return {"pdf_engine": {"name": eng["name"], "version": eng["version"]} if eng.get("module") else None,
            "pdf_error": eng.get("error", ""), "pdf_hint": pdf_hint(),
            "lp": bool(which("lp")), "lpr": bool(which("lpr")), "lpr_cups": bool(which("lpr-cups")),
            "sumatra": find_sumatra() if WIN else None, "platform": sys.platform}


def send_to_printer(pdf: Path, printer: str = "") -> tuple[bool, str]:
    where = f"printer '{printer}'" if printer else "the default printer"
    if WIN:
        sumatra = find_sumatra()
        if sumatra:
            cmd = [sumatra, "-print-to", printer, "-silent", str(pdf)] if printer else \
                  [sumatra, "-print-to-default", "-silent", str(pdf)]
            r = subprocess.run(cmd, capture_output=True, timeout=120, **NO_WINDOW)
            return (r.returncode == 0, f"sent to {where} via SumatraPDF" if r.returncode == 0
                    else "SumatraPDF failed: " + r.stderr.decode(errors="ignore")[:160])
        try:
            os.startfile(str(pdf), "print")  # type: ignore[attr-defined]
            return True, "sent to the default printer via Windows (delivery cannot be confirmed)"
        except OSError as exc:
            return False, f"Windows could not print the PDF: {exc}"

    if FREEBSD:  # only the CUPS client: FreeBSD's own `lp`/`lpr` talk to the old BSD spooler, not CUPS
        tool = which("lpr-cups")
        if not tool:
            return False, "'lpr-cups' is not installed (FreeBSD: pkg install cups)"
        cmd, name = [tool] + (["-P", printer] if printer else []) + [str(pdf)], "lpr-cups"
    else:
        lp, lpr = which("lp"), which("lpr")  # CUPS 'lp' first; 'lpr' as the fallback
        if lp:
            cmd, name = [lp] + (["-d", printer] if printer else []) + [str(pdf)], "lp"
        elif lpr:
            cmd, name = [lpr] + (["-P", printer] if printer else []) + [str(pdf)], "lpr"
        else:
            return False, "neither 'lp' nor 'lpr' is installed"
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    if r.returncode == 0:
        return True, f"sent to {where} via {name} {r.stdout.decode(errors='ignore').strip()}".strip()
    return False, f"{name} failed: " + (r.stderr.decode(errors="ignore").strip() or r.stdout.decode(errors="ignore").strip())[:160]


# ------------------------------------------------------------------ page limit
def _render_doc(weasy, articles: list[dict], layout):
    html_text = render(articles, layout)
    return html_text, weasy.HTML(string=html_text).render()


def paginate(weasy, articles: list[dict], layout, max_pages: int, policy: str) -> dict:
    """Lay the articles out and apply the page limit. Returns the document to write, the HTML that matches it,
    the articles it contains and what (if anything) was done to respect the limit."""
    html_text, doc = _render_doc(weasy, articles, layout)
    total = len(doc.pages)
    out = {"doc": doc, "html": html_text, "articles": articles, "pages_total": total, "pages": total,
           "limited": None, "note": ""}
    if max_pages <= 0 or total <= max_pages:
        return out
    if policy == "skip":
        out.update(limited="skip", note=f"{total} pages is over the limit of {max_pages}")
        return out
    if policy == "fit" and len(articles) > 1:
        # whole articles, newest first: the largest prefix that fits (page count only grows with more articles)
        started, lo, hi, best = time.time(), 1, len(articles) - 1, None
        while lo <= hi and time.time() - started < FIT_DEADLINE:
            mid = (lo + hi) // 2
            h, d = _render_doc(weasy, articles[:mid], layout)
            if len(d.pages) <= max_pages:
                best, lo = (mid, h, d), mid + 1
            else:
                hi = mid - 1
        if best:
            k, h, d = best
            out.update(doc=d, html=h, articles=articles[:k], pages=len(d.pages), limited="fit",
                       note=f"{k} of {len(articles)} articles fit in {len(d.pages)} page{'s' if len(d.pages) != 1 else ''} "
                            f"(limit {max_pages})")
            return out
        # not even the newest article fits (or the search ran out of time): cut pages instead
    out.update(doc=doc.copy(doc.pages[:max_pages]), pages=max_pages, limited="truncate",
               note=(f"only the first page of {total}" if max_pages == 1 else f"only the first {max_pages} of {total} pages")
                    + f" (limit {max_pages})")
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:40] or "print"


def _pages(n: int | None) -> str:
    return f" ({n} page{'s' if n != 1 else ''})" if n else ""


def produce(articles: list[dict], layout: dict | None, name: str, action="print", printer="",
            max_pages=0, over_limit="fit") -> dict:
    """Render, save (HTML + PDF), apply the page limit and optionally print.
    max_pages 0 = no limit; over_limit says what to do when the job is longer: fit | truncate | skip."""
    store.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = store.OUTPUT_DIR / f"{_slug(name)}_{time.strftime('%Y%m%d_%H%M%S')}"
    html_path, pdf_path = base.with_suffix(".html"), base.with_suffix(".pdf")
    res = {"html": str(html_path), "pdf": None, "printed": False, "count": len(articles), "message": "",
           "pages": None, "limited": None}

    html_text, note, pg = render(articles, layout), "", None
    eng = load_engine()
    if eng.get("module"):
        try:
            with _pdf_lock:
                pg = paginate(eng["module"], articles, layout, int(max_pages or 0), over_limit)
                pg["doc"].write_pdf(str(pdf_path))
            html_text = pg["html"]
            res.update(pdf=str(pdf_path), pages=pg["pages"], limited=pg["limited"], count=len(pg["articles"]))
        except Exception as exc:
            pdf_path.unlink(missing_ok=True)
            pg, note = None, f"WeasyPrint failed: {type(exc).__name__}: {str(exc)[:200]}"
    else:
        note = f"WeasyPrint is not available ({eng['error']}). Install it: {pdf_hint()}"
    html_path.write_text(html_text, "utf-8")

    n = res["count"]
    # a cut-off document no longer holds "n articles", only some pages of them
    what = (f"{res['pages']} page{'s' if res['pages'] != 1 else ''}" if res["limited"] == "truncate"
            else f"{n} articles{_pages(res['pages'])}")
    if action == "print":
        if not res["pdf"]:
            res["message"] = f"Saved HTML only, not printed: {note}"
        elif res["limited"] == "skip":
            res["message"] = (f"Not printed: {pg['note']}. The full PDF ({pg['pages']} pages) was saved for review.")
        else:
            res["printed"], msg = send_to_printer(pdf_path, printer)
            res["message"] = f"Printed {what}: {msg}" if res["printed"] else f"Saved but not printed: {msg}"
    else:
        res["message"] = f"Saved {what}" + ("" if res["pdf"] else f" (HTML only: {note})")
    if res["limited"] in ("fit", "truncate"):
        res["message"] += f"  [{pg['note']}]"
    elif max_pages and not res["pdf"]:
        res["message"] += "  [page limit not applied: no PDF engine]"
    _prune_outputs()
    return res


def _prune_outputs(keep=200) -> None:
    files = sorted(store.OUTPUT_DIR.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass
