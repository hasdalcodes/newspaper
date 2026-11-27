#!/usr/bin/env python3
"""
Newspaper UI (Tkinter)

A desktop front-end for the Newspaper background service. It holds no data itself: everything
(articles, feeds, print layouts, cron jobs) lives in the service, which keeps running when
this window is closed.

Run:  python newspaper_ui.py
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, messagebox, scrolledtext, ttk

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import textshape  # noqa: E402  (Arabic/RTL display for Tk on X11)

DATA_DIR = Path(os.getenv("NEWSPAPER_HOME", str(Path.home() / ".newspaper")))  # per-user service (default)
SYSTEM_DATA_DIR = Path("/var/db/newspaper")                                   # FreeBSD rc.d service
SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
UI_SETTINGS = Path.home() / ".newspaper-ui.json"                              # saved by Service -> Connect...
WIN = sys.platform.startswith("win")


def ui_settings() -> dict:
    try:
        d = json.loads(UI_SETTINGS.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_settings(d: dict) -> None:
    if not d:
        UI_SETTINGS.unlink(missing_ok=True)
        return
    UI_SETTINGS.write_text(json.dumps(d), "utf-8")
    try:
        os.chmod(UI_SETTINGS, 0o600)
    except OSError:
        pass


WITHIN = {"Any time": 0, "Last 6 hours": 6, "Last 24 hours": 24, "Last 48 hours": 48, "Last 7 days": 168}
STATUS_COLORS = {"ok": "#15803d", "saved": "#b45309", "skipped": "#6b7280", "error": "#b91c1c",
                 "running": "#1d4ed8", "interrupted": "#b91c1c"}


# ======================================================================= API
class ApiError(Exception):
    pass


class ServiceDown(ApiError):
    pass


def local_url(cfg: dict) -> str:
    host = cfg.get("host", "127.0.0.1")
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.get('port', 8765)}"


class Api:
    """Finds a Newspaper service and talks to it. Looks for, in this order: a connection saved with
    Service -> Connect..., $NEWSPAPER_HOME, the per-user service (~/.newspaper) and the FreeBSD system service
    (/var/db/newspaper). The first one that answers is used, and it is re-detected if it goes away."""

    def __init__(self):
        self.conn = None   # {"label", "where", "base", "token"} of the service in use
        self.report: dict = {}

    # ---- manual connection
    @staticmethod
    def manual():
        d = ui_settings()
        return d if d.get("url") and d.get("token") else None

    @staticmethod
    def save_manual(url: str, token: str) -> None:
        d = ui_settings()
        d.update(url=url.strip().rstrip("/"), token=token.strip())
        save_ui_settings(d)

    @staticmethod
    def forget_manual() -> None:
        d = ui_settings()
        d.pop("url", None)
        d.pop("token", None)
        save_ui_settings(d)

    # ---- discovery
    def candidates(self):
        """[(label, where, state, conn)]; state is ok | missing | denied | bad"""
        out = []
        m = self.manual()
        if m:
            out.append(("manual connection", str(UI_SETTINGS), "ok", {"base": m["url"], "token": m["token"]}))
        paths = []
        if os.getenv("NEWSPAPER_HOME"):
            paths.append(("NEWSPAPER_HOME service", Path(os.environ["NEWSPAPER_HOME"]) / "config.json"))
        paths.append(("user service", Path.home() / ".newspaper" / "config.json"))
        if not WIN:
            paths.append(("system service", SYSTEM_DATA_DIR / "config.json"))
        seen = set()
        for label, p in paths:
            if str(p) in seen:
                continue
            seen.add(str(p))
            try:
                cfg = json.loads(p.read_text("utf-8"))
                state, conn = "ok", {"base": local_url(cfg), "token": cfg.get("token", "")}
            except FileNotFoundError:
                state, conn = "missing", None
            except PermissionError:
                state, conn = "denied", None
            except (OSError, ValueError):
                state, conn = "bad", None
            out.append((label, str(p), state, conn))
        return out

    def detect(self):
        rep = {"not_running": [], "denied": [], "rejected": [], "found": []}
        for label, where, state, conn in self.candidates():
            if state == "denied":
                rep["denied"].append((label, where))
            if state != "ok":
                continue
            rep["found"].append(label)
            try:
                r = requests.get(conn["base"] + "/api/status", headers={"X-Newspaper-Token": conn["token"]}, timeout=2)
            except requests.RequestException:
                rep["not_running"].append((label, where))
                continue
            if r.status_code == 200:
                self.conn, self.report = {**conn, "label": label, "where": where}, rep
                return self.conn
            rep["rejected"].append((label, where))
        self.conn, self.report = None, rep
        raise ServiceDown(self.explain(rep))

    @staticmethod
    def explain(rep: dict) -> str:
        lines = []
        for label, where in rep["rejected"]:
            lines.append(f"The {label} answers but rejected the saved access token ({where}). "
                         "Press Connect... and paste the current token (on FreeBSD: service newspaper token).")
        for label, where in rep["denied"]:
            lines.append(f"A {label} is installed ({where}) but this account may not read its access token. "
                         "Add your login to the service group (FreeBSD: pw groupmod newspaper -m YOURLOGIN, then log "
                         "in again), or press Connect... and paste the token from: service newspaper token")
        for label, where in rep["not_running"]:
            how = "service newspaper start (as root)" if label == "system service" else "press Start service"
            lines.append(f"A {label} is configured ({where}) but is not running. To start it: {how}.")
        return "\n".join(lines) or "No Newspaper service found. Press Start service, or install the FreeBSD service."

    # ---- calls
    def call(self, method, path, body=None, params=None, timeout=30):
        if not self.conn:
            self.detect()
        c = self.conn
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            r = requests.request(method, c["base"] + path, json=body, params=params, timeout=timeout,
                                 headers={"X-Newspaper-Token": c["token"]})
        except requests.ConnectionError:
            self.conn = None
            raise ServiceDown("The Newspaper service stopped responding.") from None
        except requests.RequestException as exc:
            raise ApiError(str(exc)) from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 401:
            self.conn = None
            raise ServiceDown("The service rejected the access token. Press Connect... and paste the current one.")
        if r.status_code >= 400:
            raise ApiError((data.get("error") if isinstance(data, dict) else None) or f"HTTP {r.status_code}")
        return data

    def status(self):
        return self.call("GET", "/api/status", timeout=4)

    def get(self, path, params=None, **kw):
        return self.call("GET", path, params=params, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body=body or {}, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body=body or {}, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


# ==================================================================== helpers
def fmt_ts(ts, fmt="%d %b %Y, %H:%M"):
    return datetime.fromtimestamp(ts).strftime(fmt) if ts else "—"


def truncate(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def is_rtl(text):
    letters = [c for c in (text or "") if c.isalpha()]
    return bool(letters) and sum("\u0590" <= c <= "\u08ff" for c in letters) / len(letters) > 0.4


def open_path(p):
    p = str(p)
    try:
        if WIN:
            os.startfile(p)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", p])
        else:  # e.g. FreeBSD without xdg-utils
            webbrowser.open(Path(p).resolve().as_uri())
    except OSError as exc:
        messagebox.showerror("Newspaper", f"Could not open {p}\n{exc}")


def to_int(var, default=0):
    try:
        return int(var.get())
    except (tk.TclError, ValueError):
        return default


def form_row(parent, row, label, widget, span=1):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
    widget.grid(row=row, column=1, columnspan=span, sticky="ew", pady=3)


OVER_LIMIT_LABELS = {
    "fit": "Print fewer articles (whole articles, newest first)",
    "truncate": "Print only the first pages (may cut an article)",
    "skip": "Don't print; keep the PDF for review",
}


def add_limit_rows(f, row: int, pages: int, policy: str):
    """Two form rows: max pages per job and what to do when a job is longer. Returns (pages_var, policy_var)."""
    pv = tk.IntVar(value=max(0, int(pages or 0)))
    fr = ttk.Frame(f)
    ttk.Spinbox(fr, from_=0, to=10000, width=6, textvariable=pv).pack(side="left")
    ttk.Label(fr, text="pages per job   (0 = no limit)", foreground="#6b7280").pack(side="left", padx=8)
    form_row(f, row, "Page limit", fr, span=2)
    lv = tk.StringVar(value=OVER_LIMIT_LABELS.get(policy, OVER_LIMIT_LABELS["fit"]))
    form_row(f, row + 1, "If it is longer", ttk.Combobox(f, textvariable=lv, values=list(OVER_LIMIT_LABELS.values()),
                                                         state="readonly", width=46), span=2)
    return pv, lv


def policy_of(label: str) -> str:
    return next((k for k, v in OVER_LIMIT_LABELS.items() if v == label), "fit")


def limit_text(j: dict) -> str:
    return f"{j['max_pages']} · {j['over_limit']}" if j.get("max_pages") else "—"


def template_choices(app):
    """({display name: template}, display name of the default layout)"""
    by = {app.display(t["name"] + ("  (default)" if t["is_default"] else "")): t for t in app.templates}
    default = next((k for k, t in by.items() if t["is_default"]), next(iter(by), ""))
    return by, default


class Tab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

    def on_lookups(self):
        """Called when sources/templates/categories were (re)loaded from the service."""


# ================================================================ main window
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Newspaper")
        self.geometry("1220x800")
        self.minsize(980, 640)
        self.api = Api()
        self.q: queue.Queue = queue.Queue()
        self.sources, self.templates, self.categories = [], [], []
        self.default_layout: dict = {}
        self.online = None
        self.asked_start = False
        self.down_reason = ""
        # Tk on X11 cannot join/reorder Arabic: "auto" fixes it there only; see textshape.py
        self.arabic_mode = os.getenv("NEWSPAPER_ARABIC_FIX") or ui_settings().get("arabic_fix", "auto")
        if self.arabic_mode not in ("auto", "on", "off"):
            self.arabic_mode = "auto"
        self._font_cache: dict = {}
        self._measure_cache: dict = {}

        base = tkfont.nametofont("TkDefaultFont")
        size = abs(int(base.cget("size"))) or 10
        self.f_bold = base.copy()
        self.f_bold.configure(weight="bold", size=size + 1)
        self.f_title = base.copy()
        self.f_title.configure(weight="bold", size=size + 4)
        self.f_small = base.copy()
        self.f_small.configure(size=max(8, size - 1))
        self.f_body = base.copy()
        self.f_body.configure(size=size + 2)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)

        self.status_lbl = tk.Label(self, text="Connecting to the service…", anchor="w", padx=12, pady=5,
                                   font=self.f_bold)
        self.status_lbl.pack(fill="x")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.tabs: dict[str, Tab] = {}
        for name, cls in (("News", NewsTab), ("Sources", SourcesTab), ("Print layouts", LayoutsTab),
                          ("Scheduler", JobsTab), ("Service", ServiceTab)):
            tab = cls(self.nb, self)
            self.nb.add(tab, text=f"  {name}  ")
            self.tabs[name] = tab

        self.after(80, self.pump)
        self.after(300, self.poll_status)

    # ---- background work: run in a thread, deliver the result on the Tk thread
    def bg(self, fn, ok=None, err=None):
        def work():
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001 - delivered to the UI thread
                self.q.put((err or self.report, (exc,)))
            else:
                if ok:
                    self.q.put((ok, (res,)))
        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except tk.TclError:
                    pass  # widget was closed meanwhile
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(80, self.pump)

    def report(self, exc):
        messagebox.showerror("Newspaper", str(exc), parent=self)

    # ---- Arabic / right-to-left display
    def fix_active(self) -> bool:
        if self.arabic_mode != "auto":
            return self.arabic_mode == "on"
        return self.tk.call("tk", "windowingsystem") == "x11"

    def font_for(self, widget):
        key = str(widget.cget("font"))
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(root=self, font=key)
        return self._font_cache[key]

    def measure_for(self, widget):
        """Font.measure for the widget's font, memoized: each call is a round trip into Tk."""
        key = str(widget.cget("font"))
        if key not in self._measure_cache:
            self._measure_cache[key] = textshape.cached_measure(self.font_for(widget).measure)
        return self._measure_cache[key]

    def fit_text(self, widget, logical: str, width: int) -> str:
        """Wrap logical text for `widget` at `width` px and convert each line to Tk-displayable order."""
        return textshape.wrap(logical, self.measure_for(widget), width, is_rtl(logical))

    def display(self, text: str) -> str:
        """Single-line text for widgets that cannot wrap (table cells, list boxes, drop-down values)."""
        if text and self.fix_active() and textshape.has_rtl(text):
            return textshape.to_visual(text, False)
        return text

    def place_text(self, label, logical: str, width: int) -> None:
        """Put text into a Label that wraps at `width`, fixing Arabic when needed."""
        if self.fix_active() and textshape.has_rtl(logical):
            label.configure(text=self.fit_text(label, logical, width), wraplength=0)
        else:
            label.configure(text=logical, wraplength=width)

    def set_arabic_mode(self, mode: str) -> None:
        self.arabic_mode = mode
        d = ui_settings()
        d["arabic_fix"] = mode
        save_ui_settings(d)
        news = self.tabs["News"]
        news.src.set("All sources")
        news.on_lookups()  # drop-down values are shaped differently now
        news.relayout()

    # ---- service status
    def check_status(self):
        self.bg(self.api.status, self._on_status, self._on_status_err)

    def poll_status(self):
        self.check_status()
        self.after(5000, self.poll_status)

    def _on_status(self, st):
        was = self.online
        self.online = True
        c = st["counts"]
        where = self.api.conn["label"] if self.api.conn else "service"
        self.status_lbl.config(fg="#15803d", text=f"●  Service running ({where})   ·   {c['articles']} articles   ·   "
                                                  f"{c['sources']} sources   ·   {c['jobs']} jobs")
        self.tabs["Service"].show_status(st)
        if was is not True:
            self.reload_lookups()

    def _on_status_err(self, exc):
        was = self.online
        self.online = False
        self.down_reason = str(exc)
        self.status_lbl.config(fg="#b91c1c", text="●  Service not reachable — see the Service tab")
        self.tabs["Service"].show_status(None)
        if was is None and not self.asked_start:
            self.asked_start = True
            rep = self.api.report or {}
            if rep.get("denied") or rep.get("rejected") or any(l == "system service" for l, _ in rep.get("not_running", [])):
                self.nb.select(self.tabs["Service"])  # a system service exists: never start a second, private copy
                messagebox.showinfo("Newspaper", self.down_reason, parent=self)
            elif messagebox.askyesno("Newspaper", "The background service is not running.\nStart it now?", parent=self):
                self.tabs["Service"].start_service()

    def reload_lookups(self, then=None):
        def work():
            return (self.api.get("/api/sources"), self.api.get("/api/templates"),
                    self.api.get("/api/categories"), self.api.get("/api/templates/default"))

        def ok(res):
            self.sources, self.templates, self.categories, self.default_layout = res
            for tab in self.tabs.values():
                tab.on_lookups()
            if then:
                then()
        self.bg(work, ok, lambda e: None)

    # ---- shared actions
    def show_html(self, html: str):
        d = Path(tempfile.gettempdir()) / "newspaper_preview"
        d.mkdir(exist_ok=True)
        for old in d.glob("preview_*.html"):
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink(missing_ok=True)
        p = d / f"preview_{int(time.time() * 1000)}.html"
        p.write_text(html, "utf-8")
        webbrowser.open(p.as_uri())


class CategoryPicker(tk.Toplevel):
    """Tick the categories to include; unticked ones are left out. Applying with everything ticked means
    "no category filter" (so categories that appear later are included too)."""

    def __init__(self, app, selected, on_apply, title="Choose categories"):
        super().__init__(app)
        self.app, self.on_apply = app, on_apply
        self.title(title)
        self.geometry("480x580")
        self.transient(app)
        items = [(c["category"], c["n"]) for c in app.categories]
        known = {name for name, _ in items}
        items += [(c, 0) for c in (selected or []) if c not in known]  # saved choices with no articles right now
        self.items = items
        self.state = {name: (selected is None or name in selected) for name, _ in items}

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.filter_var)
        e.pack(side="left", fill="x", expand=True, padx=6)
        self.filter_var.trace_add("write", lambda *_: self.fill())
        self.count_lbl = ttk.Label(top, text="", width=18, anchor="e")
        self.count_lbl.pack(side="right")

        self.tree = ttk.Treeview(self, columns=("tick", "name", "n"), show="headings", selectmode="none")
        for key, label, width, anchor in (("tick", "", 40, "center"), ("name", "Category", 320, "w"),
                                          ("n", "Articles", 70, "e")):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor, stretch=key == "name")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", pady=4)
        self.tree.pack(fill="both", expand=True, padx=(12, 0), pady=4)
        self.tree.bind("<Button-1>", self.click)

        bar = ttk.Frame(self, padding=(12, 4, 12, 10))
        bar.pack(side="bottom", fill="x")
        for text, cmd in (("Select all", lambda: self.set_shown(True)), ("Select none", lambda: self.set_shown(False)),
                          ("Invert", self.invert)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Apply", command=self.apply).pack(side="right", padx=6)
        ttk.Label(self, foreground="#6b7280", wraplength=420, text=(
            "Ticked categories are included. The buttons act on the rows shown by the filter. "
            "With everything ticked there is no filter, so new categories are included too.")
                  ).pack(side="bottom", anchor="w", padx=12)
        self.fill()
        e.focus_set()

    @staticmethod
    def label(name: str) -> str:
        return name if name else "(uncategorized)"

    def fill(self):
        q = self.filter_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        for i, (name, n) in enumerate(self.items):
            if q and q not in self.label(name).lower():
                continue
            self.tree.insert("", "end", iid=str(i), values=(
                "☑" if self.state[name] else "☐", self.app.display(self.label(name)), n))
        self.update_count()

    def update_count(self):
        self.count_lbl.config(text=f"{sum(self.state.values())} of {len(self.items)} ticked")

    def toggle(self, iid: str):
        name = self.items[int(iid)][0]
        self.state[name] = not self.state[name]
        self.tree.set(iid, "tick", "☑" if self.state[name] else "☐")

    def click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.toggle(iid)
            self.update_count()
        return "break"

    def set_shown(self, value: bool):
        for iid in self.tree.get_children():
            self.state[self.items[int(iid)][0]] = value
            self.tree.set(iid, "tick", "☑" if value else "☐")
        self.update_count()

    def invert(self):
        for iid in self.tree.get_children():
            self.toggle(iid)
        self.update_count()

    def apply(self):
        ticked = [name for name, _ in self.items if self.state[name]]
        self.on_apply(None if len(ticked) == len(self.items) else ticked)
        self.destroy()


def categories_button_text(app, selected) -> str:
    if selected is None:
        return "Categories: all"
    total = len({c["category"] for c in app.categories} | set(selected))
    return f"Categories: {len(selected)} of {total}"


# ====================================================================== News
class NewsTab(Tab):
    PAGE = 36

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.items, self.cards, self.total, self.offset = [], [], 0, 0
        self.ncols, self._resize_job, self._gen, self._first = 0, None, 0, True
        self.card_w, self._build_job, self._canvas_w = 0, None, 0
        self.src_ids: dict[str, int] = {}
        saved = ui_settings().get("news_categories")  # chosen categories are remembered between sessions
        self.cat_sel = [str(c) for c in saved] if isinstance(saved, list) else None

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(8, 2))
        self.q = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.q, width=26)
        e.pack(side="left")
        e.bind("<Return>", lambda _e: self.load())
        self.src = tk.StringVar(value="All sources")
        self.src_cb = ttk.Combobox(bar, textvariable=self.src, state="readonly", width=24, values=["All sources"])
        self.lang = tk.StringVar(value="All languages")
        self.within = tk.StringVar(value="Any time")
        for w in (self.src_cb,
                  ttk.Combobox(bar, textvariable=self.lang, state="readonly", width=14,
                               values=["All languages", "en", "ar"]),
                  ttk.Combobox(bar, textvariable=self.within, state="readonly", width=14, values=list(WITHIN))):
            w.pack(side="left", padx=(6, 0))
            w.bind("<<ComboboxSelected>>", lambda _e: self.load())
        self.cat_btn = ttk.Button(bar, text=categories_button_text(app, self.cat_sel), command=self.pick_categories)
        self.cat_btn.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Search", command=self.load).pack(side="left", padx=(8, 0))

        bar2 = ttk.Frame(self)
        bar2.pack(fill="x", padx=10, pady=(2, 6))
        ttk.Button(bar2, text="Fetch new articles now", command=self.fetch_now).pack(side="left")
        ttk.Button(bar2, text="Print…", command=self.print_dialog).pack(side="left", padx=6)
        ttk.Button(bar2, text="Clear selection", command=self.clear_selection).pack(side="left")
        self.count_lbl = ttk.Label(bar2, text="")
        self.count_lbl.pack(side="right")

        footer = ttk.Frame(self)
        footer.pack(side="bottom", fill="x")
        self.more_btn = ttk.Button(footer, text="Load more", command=lambda: self.load(reset=False))
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bg="#eef0f3")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg="#eef0f3")
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_resize)
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(ev, self.on_wheel, add="+")

    # ---- scrolling / layout
    def on_wheel(self, e):
        try:
            if self.app.nb.select() != str(self):
                return
        except tk.TclError:
            return
        step = -1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1
        self.canvas.yview_scroll(step * 2, "units")

    def on_resize(self, e):
        if e.width == self._canvas_w:  # height-only change: nothing to rewrap
            return
        self._canvas_w = e.width
        self.canvas.itemconfigure(self.win, width=e.width)
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self.relayout)

    def relayout(self, start=0):
        """Grid and wrap cards from index `start` on. Work is skipped for cards whose place and width did
        not change, so resizing within the same column width and "Load more" only touch what is new."""
        self._resize_job = None
        w = max(self.canvas.winfo_width(), 400)
        ncols = max(1, w // 360)
        card_w = (w - 12 - ncols * 12) // ncols
        if ncols != self.ncols:
            for c in range(max(ncols, self.ncols)):
                self.inner.grid_columnconfigure(c, weight=1 if c < ncols else 0,
                                                uniform="cards" if c < ncols else "")
            self.ncols, start = ncols, 0
        if card_w != self.card_w:
            self.card_w, start = card_w, 0
        for i in range(start, len(self.cards)):
            self.place_card(self.cards[i], i)

    def place_card(self, card, i):
        cell = (i // self.ncols, i % self.ncols)
        if card.get("cell") != cell:
            card["frame"].grid(row=cell[0], column=cell[1], padx=6, pady=6, sticky="nsew")
            card["cell"] = cell
        if card.get("width") != self.card_w:
            for lb in card["wrap"]:
                self.app.place_text(lb, card["texts"][lb], self.card_w - 28)
            card["width"] = self.card_w

    # ---- data
    def on_lookups(self):
        d = self.app.display
        self.src_ids = {d(s["name"]): s["id"] for s in self.app.sources}
        self.src_cb.configure(values=["All sources"] + list(self.src_ids))
        self.cat_btn.config(text=categories_button_text(self.app, self.cat_sel))
        if self._first:
            self._first = False
            self.load()

    def filters(self):
        f = {}
        if self.q.get().strip():
            f["q"] = self.q.get().strip()
        if self.src_ids.get(self.src.get()):
            f["source_id"] = self.src_ids[self.src.get()]
        if self.lang.get() in ("en", "ar"):
            f["lang"] = self.lang.get()
        if self.cat_sel is not None:
            f["categories"] = list(self.cat_sel)
        if WITHIN.get(self.within.get()):
            f["hours"] = WITHIN[self.within.get()]
        return f

    def pick_categories(self):
        CategoryPicker(self.app, self.cat_sel, self.set_categories, "Categories to show")

    def set_categories(self, selected):
        self.cat_sel = selected
        d = ui_settings()
        if selected is None:
            d.pop("news_categories", None)
        else:
            d["news_categories"] = selected
        save_ui_settings(d)
        self.cat_btn.config(text=categories_button_text(self.app, selected))
        self.load()

    def load(self, reset=True):
        if reset:
            self.offset = 0
        self._gen += 1
        gen = self._gen
        params = {**self.filters(), "limit": self.PAGE, "offset": self.offset}
        if "categories" in params:  # lists travel as JSON in the query string
            params["categories"] = json.dumps(params["categories"])
        self.app.bg(lambda: self.app.api.get("/api/articles", params=params),
                    lambda r: self.loaded(r, reset, gen),
                    lambda e: self.count_lbl.config(text=str(e)))

    CHUNK = 6  # cards built per event-loop turn, so the window keeps responding while a page appears

    def loaded(self, res, reset, gen):
        if gen != self._gen:
            return
        if self._build_job:
            self.after_cancel(self._build_job)
            self._build_job = None
        if reset:
            self.clear_cards()
        self.offset += len(res["items"])
        self.total = res["total"]
        self.more_btn.pack_forget()
        if not res["items"]:
            self.finish_page()
            return
        self.count_lbl.config(text="Loading…")
        self.build_cards(list(res["items"]), gen)

    def clear_cards(self):
        """Throw the old cards away in one go: a fresh container replaces the old one, which is destroyed
        with all its children by a single Tk call (instead of one destroy per card)."""
        old = self.inner
        self.inner = tk.Frame(self.canvas, bg="#eef0f3")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.delete(self.win)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw",
                                             width=self.canvas.winfo_width())
        old.destroy()
        self.cards, self.ncols, self.card_w = [], 0, 0
        self.canvas.yview_moveto(0)
        self.relayout()  # sets up the columns of the new container

    def build_cards(self, todo, gen):
        self._build_job = None
        if gen != self._gen:  # a newer search started: stop building this one
            return
        start = len(self.cards)
        for it in todo[:self.CHUNK]:
            self.cards.append(self.make_card(it))
        self.relayout(start)
        if len(todo) > self.CHUNK:
            self._build_job = self.after(1, self.build_cards, todo[self.CHUNK:], gen)
        else:
            self.finish_page()

    def finish_page(self):
        self.count_lbl.config(text=f"Showing {len(self.cards)} of {self.total} articles" if self.total
                              else "No articles yet. Sources are fetched when a print job runs; press 'Fetch new articles now' to fetch now.")
        if self.offset < self.total:
            self.more_btn.pack(pady=4)

    def make_card(self, it):
        rtl = is_rtl(it["title"])
        anchor, justify = ("e", "right") if rtl else ("w", "left")
        f = tk.Frame(self.inner, bg="white", highlightbackground="#cfd4da", highlightthickness=1, cursor="hand2")
        title = tk.Label(f, text=it["title"], font=self.app.f_bold, bg="white", anchor=anchor, justify=justify)
        meta = tk.Label(f, text=f"{it['label']}   •   {fmt_ts(it['published'] or it['fetched'])}",
                        font=self.app.f_small, fg="#6b7280", bg="white", anchor="w")
        summary = truncate(it["summary"], 240)
        s_anchor, s_justify = ("e", "right") if is_rtl(summary) else ("w", "left")
        summ = tk.Label(f, text=summary, bg="white", anchor=s_anchor, justify=s_justify)
        title.pack(fill="x", padx=10, pady=(10, 2))
        meta.pack(fill="x", padx=10)
        summ.pack(fill="x", padx=10, pady=(6, 4))
        bar = tk.Frame(f, bg="white")
        bar.pack(fill="x", padx=6, pady=(2, 8), side="bottom")
        var = tk.BooleanVar()
        tk.Checkbutton(bar, text="Select", variable=var, bg="white", activebackground="white").pack(side="left")
        ttk.Button(bar, text="Print", width=7, command=lambda: self.print_dialog([it["id"]])).pack(side="right")
        ttk.Button(bar, text="Read", width=7, command=lambda: ArticleWindow(self.app, it)).pack(side="right", padx=4)
        for w in (f, title, meta, summ):
            w.bind("<Button-1>", lambda _e, it=it: ArticleWindow(self.app, it))
        return {"frame": f, "wrap": [title, summ], "var": var, "it": it,
                "texts": {title: it["title"], summ: summary}}

    def selected_ids(self):
        return [c["it"]["id"] for c in self.cards if c["var"].get()]

    def clear_selection(self):
        for c in self.cards:
            c["var"].set(False)

    def fetch_now(self):
        self.app.bg(lambda: self.app.api.post("/api/refresh"),
                    lambda _r: self.count_lbl.config(text="Fetching all sources in the background…"))
        self.after(7000, lambda: (self.app.reload_lookups(), self.load()))

    def print_dialog(self, ids=None):
        PrintDialog(self.app, self.filters(), ids if ids is not None else self.selected_ids())


class ArticleWindow(tk.Toplevel):
    def __init__(self, app, it):
        super().__init__(app)
        self.app, self.it = app, it
        self.title(truncate(it["title"], 80))
        self.geometry("860x720")
        rtl = is_rtl(it["title"])
        anchor, justify = ("e", "right") if rtl else ("w", "left")
        self.title_lbl = tk.Label(self, text=it["title"], font=app.f_title, justify=justify, anchor=anchor)
        self.title_lbl.pack(fill="x", padx=16, pady=(14, 2))
        self.article, self._job, self._last_w = None, None, 0
        meta = "   •   ".join(x for x in (it["label"], fmt_ts(it["published"] or it["fetched"]),
                                          it.get("category"), it.get("author")) if x)
        self.meta_text = meta
        self.meta_lbl = tk.Label(self, text=meta, fg="#6b7280", anchor="w", justify="left")
        self.meta_lbl.pack(fill="x", padx=16)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=16, pady=8)
        ttk.Button(bar, text="Open original", command=lambda: webbrowser.open(it["url"])).pack(side="left")
        ttk.Button(bar, text="View as web page", command=self.as_web).pack(side="left", padx=6)
        ttk.Button(bar, text="Print this article…", command=lambda: PrintDialog(app, {}, [it["id"]])
                   ).pack(side="left")
        self.text = scrolledtext.ScrolledText(self, wrap="word", padx=14, pady=12, font=app.f_body, relief="flat")
        self.text.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.text.tag_configure("rtl", justify="right")
        self.text.tag_configure("note", foreground="#6b7280")
        self.text.bind("<Configure>", self.on_resize)
        self.refit_header()
        self.write("Loading…", "note")
        app.bg(lambda: app.api.get(f"/api/articles/{it['id']}"), self.show, self.fail)

    def write(self, txt, tag=None):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", txt, (tag,) if tag else ())
        self.text.configure(state="disabled")

    def show(self, a):
        self.article = a
        self.render_body()

    def refit_header(self):
        w = max(300, self.winfo_width() - 40)
        self.app.place_text(self.title_lbl, self.it["title"], w)
        self.app.place_text(self.meta_lbl, self.meta_text, w)

    def on_resize(self, e):
        if self.article is None or abs(e.width - self._last_w) < 24:
            return
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(150, self.render_body)

    def render_body(self):
        """(Re)draw title and text. With the Arabic fix active, lines are wrapped here instead of by Tk."""
        a = self.article
        body = a.get("content") or ((a.get("summary") or "") + "\n\n(No full text stored for this article. Use "
                                    "'Open original', or enable 'Fetch full article text' on its source.)")
        self.refit_header()
        fix = self.app.fix_active() and textshape.has_rtl(a["title"] + body)
        self._last_w = self.text.winfo_width()
        width = max(200, self._last_w - 60)
        self.text.configure(state="normal", wrap="none" if fix else "word")
        self.text.delete("1.0", "end")
        blocks = body.split("\n\n")
        for n, block in enumerate(blocks):
            rtl = is_rtl(block)
            shown = self.app.fit_text(self.text, block, width) if fix else block
            self.text.insert("end", shown + ("\n\n" if n < len(blocks) - 1 else ""), ("rtl",) if rtl else ())
        self.text.configure(state="disabled")

    def fail(self, exc):
        self.write(f"Could not load the article: {exc}", "note")

    def as_web(self):
        layout = {"title": "Article", "subtitle": "", "columns": 1, "page_break_each": False,
                  "fields": ["title", "source", "datetime", "author", "content"]}
        self.app.bg(lambda: self.app.api.post("/api/render", {"layout": layout,
                                                              "filters": {"article_ids": [self.it["id"]]}}),
                    lambda r: self.app.show_html(r["html"]))


class PrintDialog(tk.Toplevel):
    def __init__(self, app, filters, selected):
        super().__init__(app)
        self.app, self.filters, self.selected = app, filters, selected
        self.title("Print articles")
        self.transient(app)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        self.by_name, default = template_choices(app)
        self.tpl = ttk.Combobox(f, state="readonly", values=list(self.by_name), width=34)
        self.tpl.set(default)
        form_row(f, 0, "Print layout", self.tpl)

        self.scope = tk.StringVar(value="selected" if selected else "filtered")
        sc = ttk.Frame(f)
        ttk.Radiobutton(sc, text=f"The {len(selected)} selected article(s)", variable=self.scope, value="selected",
                        state="normal" if selected else "disabled").pack(anchor="w")
        r2 = ttk.Frame(sc)
        r2.pack(anchor="w")
        ttk.Radiobutton(r2, text="Everything matching the current filters, up to", variable=self.scope,
                        value="filtered").pack(side="left")
        self.limit = tk.IntVar(value=30)
        ttk.Spinbox(r2, from_=1, to=200, width=5, textvariable=self.limit).pack(side="left", padx=4)
        form_row(f, 1, "What to print", sc)

        self.action = tk.StringVar(value="preview")
        ac = ttk.Frame(f)
        for text, val in (("Preview in browser (print from there)", "preview"),
                          ("Print on the service computer's printer", "print"),
                          ("Save PDF / HTML only", "save")):
            ttk.Radiobutton(ac, text=text, variable=self.action, value=val).pack(anchor="w")
        form_row(f, 2, "Action", ac)
        self.printer = ttk.Entry(f)
        form_row(f, 3, "Printer name", self.printer)
        ttk.Label(f, text="Leave the printer name empty to use the default printer.", foreground="#6b7280"
                  ).grid(row=4, column=1, sticky="w")
        self.max_pages, self.over_limit = add_limit_rows(f, 5, 0, "fit")
        self.msg = ttk.Label(f, text="", wraplength=420)
        self.msg.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        bar = ttk.Frame(f)
        bar.grid(row=8, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.go_btn = ttk.Button(bar, text="Go", command=self.go)
        self.go_btn.pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="left", padx=(6, 0))

    def go(self):
        tpl = self.by_name.get(self.tpl.get())
        filt = ({"article_ids": self.selected} if self.scope.get() == "selected"
                else {**self.filters, "limit": max(1, to_int(self.limit, 30))})
        payload = {"template_id": tpl["id"] if tpl else None, "filters": filt}
        act = self.action.get()
        self.go_btn.state(["disabled"])
        self.msg.config(text="Working…")
        if act == "preview":
            payload["print_dialog"] = True
            self.app.bg(lambda: self.app.api.post("/api/render", payload, timeout=60), self.previewed, self.fail)
        else:
            payload.update(action=act, printer=self.printer.get().strip(), name="Manual print",
                           max_pages=max(0, to_int(self.max_pages, 0)), over_limit=policy_of(self.over_limit.get()))
            self.app.bg(lambda: self.app.api.post("/api/print", payload, timeout=240), self.done, self.fail)

    def previewed(self, r):
        self.go_btn.state(["!disabled"])
        if not r["count"]:
            self.msg.config(text="No articles matched.")
            return
        self.app.show_html(r["html"])
        self.msg.config(text=f"Opened {r['count']} articles in your browser.")

    def done(self, r):
        self.go_btn.state(["!disabled"])
        self.msg.config(text=r["message"])
        target = r.get("pdf") or r.get("html")
        if target and messagebox.askyesno("Newspaper", f"{r['message']}\n\nOpen the saved file now?", parent=self):
            open_path(target)

    def fail(self, exc):
        self.go_btn.state(["!disabled"])
        self.msg.config(text=f"Failed: {exc}")


# ================================================================== Sources
class SourcesTab(Tab):
    COLS = (("name", "Name", 210), ("kind", "Type", 70), ("url", "URL / repo", 340), ("lang", "Lang", 50),
            ("every", "Every", 70), ("on", "On", 40), ("items", "Items", 60), ("last", "Last fetch", 130),
            ("status", "Status", 230))

    def __init__(self, parent, app):
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        for text, cmd in (("Browse popular feeds…", lambda: CatalogDialog(app, self)),
                          ("Add RSS / Atom feed", lambda: SourceDialog(app, self, kind="rss")),
                          ("Add GitHub scraper source", lambda: SourceDialog(app, self, kind="github")),
                          ("Edit", self.edit), ("Delete", self.delete),
                          ("Fetch selected", self.fetch_selected), ("Fetch all", self.fetch_all),
                          ("Full re-fetch (GitHub)", self.refetch)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Label(self, foreground="#6b7280", text=(
            "Sources are fetched when a print job runs (Scheduler tab) or when you press a Fetch button; nothing "
            "polls in the background unless you set 'auto-fetch every N minutes' on a source.")
                  ).pack(anchor="w", padx=12, pady=(0, 6))
        self.tree = ttk.Treeview(self, columns=[c[0] for c in self.COLS], show="headings", selectmode="browse")
        for key, label, width in self.COLS:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        for tag, col in STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=col)
        self.tree.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree.bind("<Double-1>", lambda _e: self.edit())
        self.rows: dict[str, dict] = {}
        self.after(1500, self.poll)

    def poll(self):
        if self.app.online and self.winfo_ismapped():
            self.reload()
        self.after(5000, self.poll)

    def on_lookups(self):
        self.fill(self.app.sources)

    def reload(self):
        self.app.bg(lambda: self.app.api.get("/api/sources"), self.fill, lambda e: None)

    def fill(self, rows):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.rows = {str(r["id"]): r for r in rows}
        for r in rows:
            st = r["last_status"] or ""
            text = {"ok": "OK", "running": "Fetching…"}.get(st, f"{st}: {truncate(r['last_error'] or '', 70)}" if st else "—")
            self.tree.insert("", "end", iid=str(r["id"]), tags=(st,) if st else (), values=(
                self.app.display(r["name"]), r["kind"].upper() if r["kind"] == "rss" else "GitHub", r["url"], r["lang"] or "auto",
                "on demand" if not r["interval_min"] else f"{r['interval_min']} min", "✔" if r["enabled"] else "✖", r["item_count"],
                fmt_ts(r["last_fetch"], "%d %b %H:%M"), text))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def current(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Newspaper", "Select a source first.", parent=self)
            return None
        return self.rows[sel[0]]

    def edit(self):
        s = self.current()
        if s:
            SourceDialog(self.app, self, src=s)

    def delete(self):
        s = self.current()
        if s and messagebox.askyesno("Newspaper", f"Delete '{s['name']}' and all its stored articles?", parent=self):
            self.app.bg(lambda: self.app.api.delete(f"/api/sources/{s['id']}"),
                        lambda _r: (self.reload(), self.app.reload_lookups()))

    def fetch_selected(self):
        s = self.current()
        if s:
            self.app.bg(lambda: self.app.api.post("/api/refresh", {"source_id": s["id"]}),
                        lambda _r: self.after(1200, self.reload))

    def fetch_all(self):
        self.app.bg(lambda: self.app.api.post("/api/refresh"), lambda _r: self.after(1200, self.reload))

    def refetch(self):
        s = self.current()
        if s:
            self.app.bg(lambda: self.app.api.post("/api/refresh", {"source_id": s["id"], "force": True}),
                        lambda _r: self.after(1200, self.reload))


class SourceDialog(tk.Toplevel):
    def __init__(self, app, tab, src=None, kind="rss"):
        super().__init__(app)
        self.app, self.tab, self.src = app, tab, src
        self.title("Edit source" if src else "Add source")
        self.transient(app)
        self.resizable(False, False)
        o = (src or {}).get("options", {})
        sv = lambda x: tk.StringVar(value=x)  # noqa: E731
        self.name, self.kind = sv(src["name"] if src else ""), sv(src["kind"] if src else kind)
        self.url, self.lang = sv(src["url"] if src else ""), sv(src["lang"] if src else "")
        self.interval = tk.IntVar(value=src["interval_min"] if src else 0)
        self.enabled = tk.BooleanVar(value=src["enabled"] if src else True)
        self.full, self.max_full = tk.BooleanVar(value=o.get("fetch_full", False)), tk.IntVar(value=o.get("max_full", 10))
        self.branch, self.path = sv(o.get("branch", "main")), sv(o.get("path", "data"))
        self.max_files, self.token = tk.IntVar(value=o.get("max_files", 8)), sv(o.get("token", ""))

        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        form_row(f, 0, "Name", ttk.Entry(f, textvariable=self.name, width=48))
        kc = ttk.Combobox(f, textvariable=self.kind, values=["rss", "github"], state="disabled" if src else "readonly")
        kc.bind("<<ComboboxSelected>>", lambda _e: self.refresh_kind())
        form_row(f, 1, "Type", kc)
        self.url_lbl = ttk.Label(f)
        self.url_lbl.grid(row=2, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(f, textvariable=self.url).grid(row=2, column=1, sticky="ew", pady=3)
        form_row(f, 3, "Language", ttk.Combobox(f, textvariable=self.lang, values=["", "en", "ar", "fr", "es", "de"]))
        ttk.Label(f, text="(empty = detect automatically)", foreground="#6b7280").grid(row=3, column=2, padx=6)
        form_row(f, 4, "Auto-fetch every (min)", ttk.Spinbox(f, from_=0, to=1440, textvariable=self.interval, width=8))
        ttk.Label(f, text="0 = only when a print job runs", foreground="#6b7280").grid(row=4, column=2, padx=6)
        ttk.Checkbutton(f, text="Enabled", variable=self.enabled).grid(row=5, column=1, sticky="w")

        self.rss_box = ttk.LabelFrame(f, text="RSS options", padding=8)
        ttk.Checkbutton(self.rss_box, text="Also download the full article text from each page",
                        variable=self.full).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self.rss_box, text="Max pages per fetch").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(self.rss_box, from_=1, to=100, textvariable=self.max_full, width=6).grid(row=1, column=1, sticky="w")
        self.gh_box = ttk.LabelFrame(f, text="GitHub options", padding=8)
        self.gh_box.columnconfigure(1, weight=1)
        for i, (lbl, var) in enumerate((("Branch", self.branch), ("Folder with the JSON files", self.path),
                                        ("Access token (private repos only)", self.token))):
            ttk.Label(self.gh_box, text=lbl).grid(row=i, column=0, sticky="w", padx=(0, 8))
            ttk.Entry(self.gh_box, textvariable=var, show="•" if "token" in lbl else "").grid(row=i, column=1, sticky="ew", pady=2)
        ttk.Label(self.gh_box, text="Newest files to read").grid(row=3, column=0, sticky="w")
        ttk.Spinbox(self.gh_box, from_=1, to=100, textvariable=self.max_files, width=6).grid(row=3, column=1, sticky="w")
        self.rss_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.gh_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.msg = ttk.Label(f, text="", wraplength=460)
        self.msg.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        bar = ttk.Frame(f)
        bar.grid(row=8, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="Test", command=self.test).pack(side="left")
        ttk.Button(bar, text="Save", command=self.save).pack(side="left", padx=6)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="left")
        self.refresh_kind()

    def refresh_kind(self):
        if self.kind.get() == "github":
            self.url_lbl.config(text="GitHub repo (owner/name)")
            self.rss_box.grid_remove()
            self.gh_box.grid()
        else:
            self.url_lbl.config(text="Feed URL")
            self.gh_box.grid_remove()
            self.rss_box.grid()

    def payload(self):
        if self.kind.get() == "rss":
            opts = {"fetch_full": self.full.get(), "max_full": to_int(self.max_full, 10)}
        else:
            opts = {"branch": self.branch.get().strip() or "main", "path": self.path.get().strip() or "data",
                    "max_files": to_int(self.max_files, 8), "token": self.token.get().strip()}
        return {"name": self.name.get().strip(), "kind": self.kind.get(), "url": self.url.get().strip(),
                "lang": self.lang.get().strip(), "enabled": self.enabled.get(),
                "interval_min": max(0, to_int(self.interval, 0)), "options": opts}

    def test(self):
        p = self.payload()
        self.msg.config(text="Testing…")
        self.app.bg(lambda: self.app.api.post("/api/sources/test", p, timeout=60),
                    lambda r: self.msg.config(text=f"OK: found {r['count']} item(s). " + "  |  ".join(r["titles"][:3])),
                    lambda e: self.msg.config(text=str(e)))

    def save(self):
        p = self.payload()
        if not p["name"] or not p["url"]:
            self.msg.config(text="Name and URL are required.")
            return
        call = (lambda: self.app.api.put(f"/api/sources/{self.src['id']}", p)) if self.src \
            else (lambda: self.app.api.post("/api/sources", p))
        self.app.bg(call, self.saved, lambda e: self.msg.config(text=str(e)))

    def saved(self, _r):
        self.tab.reload()
        self.app.reload_lookups()
        self.destroy()


class CatalogDialog(tk.Toplevel):
    """Pick from the built-in list of well-known news feeds; 'Check' tests them live first."""

    def __init__(self, app, tab):
        super().__init__(app)
        self.app, self.tab = app, tab
        self.items: list[dict] = []
        self.results: dict[str, str] = {}
        self.note = ""  # last result, kept next to the "N feeds shown" count
        self.title("Popular news feeds")
        self.geometry("960x620")
        self.transient(app)
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.group, self.lang = tk.StringVar(value="All groups"), tk.StringVar(value="All languages")
        self.group_cb = ttk.Combobox(top, textvariable=self.group, state="readonly", width=26, values=["All groups"])
        self.group_cb.pack(side="left")
        lang_cb = ttk.Combobox(top, textvariable=self.lang, state="readonly", width=16,
                               values=["All languages", "en", "ar", "es", "pt", "uk"])
        lang_cb.pack(side="left", padx=8)
        for cb in (self.group_cb, lang_cb):
            cb.bind("<<ComboboxSelected>>", lambda _e: self.fill())
        ttk.Label(top, foreground="#6b7280", text="Ctrl/Shift-click selects several.   Listed: ✔ seen in feed lists, ? from memory"
                  ).pack(side="left", padx=8)

        cols = (("name", "Feed", 330), ("group", "Group", 170), ("lang", "Lang", 50), ("seen", "Listed", 60),
                ("status", "Status", 300))
        self.tree = ttk.Treeview(self, columns=[c[0] for c in cols], show="headings", selectmode="extended")
        for key, label, w in cols:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=w, anchor="w")
        self.tree.tag_configure("added", foreground="#9ca3af")
        self.tree.tag_configure("ok", foreground="#15803d")
        self.tree.tag_configure("bad", foreground="#b91c1c")
        self.tree.pack(fill="both", expand=True, padx=12, pady=4)
        bar = ttk.Frame(self, padding=(12, 4, 12, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="Select all shown", command=lambda: self.tree.selection_set(self.tree.get_children())
                   ).pack(side="left")
        ttk.Button(bar, text="Check selected", command=self.check).pack(side="left", padx=6)
        ttk.Button(bar, text="Add selected", command=self.add).pack(side="left")
        self.msg = ttk.Label(bar, text="Loading…")
        self.msg.pack(side="left", padx=12)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.load()

    def load(self):
        self.app.bg(lambda: self.app.api.get("/api/catalog"), self.loaded, lambda e: self.msg.config(text=str(e)))

    def loaded(self, items):
        self.items = items
        groups = list(dict.fromkeys(i["group"] for i in items))  # keep the catalog's own order
        self.group_cb.configure(values=["All groups"] + groups)
        self.fill()

    def fill(self):
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for it in self.items:
            if self.group.get() not in ("All groups", it["group"]):
                continue
            if self.lang.get() not in ("All languages", it["lang"]):
                continue
            res = self.results.get(it["id"], "")
            status = "already added" if it["added"] else res
            tag = "added" if it["added"] else ("ok" if res.startswith("✔") else "bad" if res.startswith("✖") else "")
            self.tree.insert("", "end", iid=it["id"], tags=(tag,) if tag else (),
                             values=(it["name"], it["group"], it["lang"], "✔" if it["seen"] else "?", status))
            shown += 1
        for iid in keep:
            if self.tree.exists(iid):
                self.tree.selection_add(iid)
        self.msg.config(text=f"{shown} feeds shown" + (f"   ·   {self.note}" if self.note else ""))

    def selected_ids(self):
        return list(self.tree.selection())

    def check(self):
        ids = self.selected_ids()
        if not ids:
            self.msg.config(text="Select the feeds to check first.")
            return
        self.msg.config(text=f"Checking {len(ids)} feed(s)… (can take up to a minute)")

        def done(res):
            for cid, r in res.items():
                self.results[cid] = f"✔ {r['count']} items" if r["ok"] else f"✖ {r['error']}"
            self.note = f"{sum(1 for r in res.values() if r['ok'])} of {len(res)} checked feeds work."
            self.fill()
        self.app.bg(lambda: self.app.api.post("/api/catalog/check", {"ids": ids}, timeout=240), done,
                    lambda e: self.msg.config(text=str(e)))

    def add(self):
        ids = [i for i in self.selected_ids() if not next(x for x in self.items if x["id"] == i)["added"]]
        if not ids:
            self.msg.config(text="Select feeds that are not added yet.")
            return

        def done(r):
            self.note = f"Added {r['added']} feed(s); fetching their articles in the background."
            self.msg.config(text=self.note)
            self.tab.reload()
            self.app.reload_lookups()
            self.load()
        self.app.bg(lambda: self.app.api.post("/api/catalog/add", {"ids": ids}), done,
                    lambda e: self.msg.config(text=str(e)))


# =============================================================== Print layouts
FIELD_LABELS = (("title", "Title"), ("source", "Source"), ("datetime", "Date & time"), ("category", "Category"),
                ("author", "Author"), ("summary", "Summary"), ("content", "Full text"), ("url", "Link"))


class LayoutsTab(Tab):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.current_id = None
        left = ttk.Frame(self, padding=(10, 8))
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Print layouts", font=app.f_bold).pack(anchor="w")
        self.lb = tk.Listbox(left, width=26, height=14, exportselection=False)
        self.lb.pack(fill="y", expand=True, pady=6)
        self.lb.bind("<<ListboxSelect>>", lambda _e: self.pick())
        for text, cmd in (("New", self.new), ("Duplicate", self.duplicate), ("Delete", self.delete),
                          ("★ Set as default", self.set_default)):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=2)
        ttk.Label(left, text="★ = default layout, used by\njobs and prints that don't\nname a layout.", foreground="#6b7280"
                  ).pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(self, padding=(4, 8, 12, 8))
        right.pack(side="left", fill="both", expand=True)
        g = ttk.Frame(right)
        g.pack(fill="x")
        for c in (1, 3, 5):
            g.columnconfigure(c, weight=1)
        self.v: dict[str, tk.Variable] = {}

        def var(key, kind=tk.StringVar, value=""):
            self.v[key] = kind(value=value)
            return self.v[key]

        def pair(row, col, label, widget):
            ttk.Label(g, text=label).grid(row=row, column=col, sticky="w", padx=(10 if col else 0, 6), pady=3)
            widget.grid(row=row, column=col + 1, sticky="ew", pady=3)

        pair(0, 0, "Layout name", ttk.Entry(g, textvariable=var("name")))
        pair(1, 0, "Page title", ttk.Entry(g, textvariable=var("title")))
        pair(1, 2, "Subtitle", ttk.Entry(g, textvariable=var("subtitle")))
        ttk.Label(g, text="{date} {count}", foreground="#6b7280").grid(row=1, column=4, sticky="w", padx=6)

        ttk.Label(g, text="Show on each card").grid(row=2, column=0, sticky="w", pady=6)
        ff = ttk.Frame(g)
        ff.grid(row=2, column=1, columnspan=5, sticky="w")
        self.field_vars = {}
        for key, label in FIELD_LABELS:
            self.field_vars[key] = tk.BooleanVar()
            ttk.Checkbutton(ff, text=label, variable=self.field_vars[key]).pack(side="left", padx=(0, 10))

        pair(3, 0, "Columns", ttk.Spinbox(g, from_=1, to=4, width=6, textvariable=var("columns", tk.IntVar, 2)))
        pair(3, 2, "Paper", ttk.Combobox(g, textvariable=var("paper"), values=["A4", "A3", "A5", "Letter", "Legal"],
                                         state="readonly", width=10))
        pair(3, 4, "Orientation", ttk.Combobox(g, textvariable=var("orientation"), values=["portrait", "landscape"],
                                               state="readonly", width=10))
        pair(4, 0, "Margin (mm)", ttk.Spinbox(g, from_=0, to=40, width=6, textvariable=var("margin_mm", tk.IntVar, 12)))
        pair(4, 2, "Font size (pt)", ttk.Spinbox(g, from_=6, to=24, increment=0.5, width=6,
                                                 textvariable=var("font_size_pt", tk.DoubleVar, 10.0)))
        ac = ttk.Frame(g)
        ttk.Entry(ac, textvariable=var("accent"), width=9).pack(side="left")
        ttk.Button(ac, text="Pick…", width=8, command=self.pick_color).pack(side="left", padx=4)
        pair(4, 4, "Accent colour", ac)
        pair(5, 0, "Font family", ttk.Entry(g, textvariable=var("font_family")))
        pair(5, 2, "Summary max chars", ttk.Spinbox(g, from_=0, to=2000, width=6,
                                                    textvariable=var("summary_chars", tk.IntVar, 260)))
        pair(5, 4, "Full text max chars", ttk.Spinbox(g, from_=0, to=100000, width=7,
                                                      textvariable=var("content_chars", tk.IntVar, 0)))
        pair(6, 0, "Date format", ttk.Entry(g, textvariable=var("date_format")))
        pair(6, 2, "Timezone", ttk.Entry(g, textvariable=var("timezone")))
        ttk.Label(g, text="(empty = this PC)", foreground="#6b7280").grid(row=6, column=4, columnspan=2, sticky="w", padx=6)
        cf = ttk.Frame(g)
        cf.grid(row=7, column=0, columnspan=6, sticky="w", pady=4)
        ttk.Checkbutton(cf, text="Draw a border around cards", variable=var("card_border", tk.BooleanVar, True)).pack(side="left")
        ttk.Checkbutton(cf, text="One article per page", variable=var("page_break_each", tk.BooleanVar, False)
                        ).pack(side="left", padx=14)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True, pady=(8, 6))
        self.texts: dict[str, tk.Text] = {}
        for key, label in (("page_template", "Page HTML"), ("card_template", "Card HTML"),
                           ("header_html", "Header HTML"), ("footer_html", "Footer HTML"),
                           ("custom_css", "Custom CSS")):
            page = ttk.Frame(nb, padding=4)
            nb.add(page, text=label)
            t = scrolledtext.ScrolledText(page, height=7, wrap="word", font="TkFixedFont", undo=True)
            t.pack(fill="both", expand=True)
            self.texts[key] = t
        ttk.Label(right, foreground="#6b7280", wraplength=820, text=(
            "The editors above hold the default HTML: edit it freely, or clear one to get the default back. "
            "Page: {title} {css} {header} {cards} {footer} {script} (needs {cards}).  Card: {title} {source} "
            "{datetime} {category} {author} {summary} {content} {url} {meta} {dir} {index}; wrap a part in "
            "[[if:summary]]…[[/if]] to show it only when that box is ticked and has a value.  "
            "Header/footer: {title} {subtitle} {date} {time} {count}.")).pack(anchor="w")
        bar = ttk.Frame(right)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Save layout", command=self.save).pack(side="left")
        ttk.Button(bar, text="Preview in browser", command=self.preview).pack(side="left", padx=6)
        ttk.Button(bar, text="Reset to factory default", command=self.reset_factory).pack(side="left")
        self.msg = ttk.Label(bar, text="")
        self.msg.pack(side="left", padx=10)

    # ---- list handling
    def on_lookups(self):
        self.lb.delete(0, "end")
        for t in self.app.templates:
            self.lb.insert("end", self.app.display(("★ " if t["is_default"] else "   ") + t["name"]))
        idx = next((i for i, t in enumerate(self.app.templates) if t["id"] == self.current_id), 0)
        if self.app.templates:
            self.lb.selection_set(idx)
            self.pick()

    def pick(self):
        sel = self.lb.curselection()
        if not sel:
            return
        t = self.app.templates[sel[0]]
        self.current_id = t["id"]
        self.load(t["name"], t["config"])

    def load(self, name, cfg):
        cfg = {**self.app.default_layout, **cfg}
        self.v["name"].set(name)
        for k, var in self.v.items():
            if k != "name" and k in cfg:
                var.set(cfg[k])
        for k, var in self.field_vars.items():
            var.set(k in cfg["fields"])
        for k, t in self.texts.items():  # an empty field means "default", so show the default HTML
            t.delete("1.0", "end")
            t.insert("1.0", cfg.get(k) or self.app.default_layout.get(k, ""))
        self.msg.config(text="")

    def collect(self):
        cfg = {k: to_int(v, 0) if isinstance(v, tk.IntVar) else v.get()
               for k, v in self.v.items() if k != "name"}
        for k in ("font_size_pt",):
            try:
                cfg[k] = float(self.v[k].get())
            except (tk.TclError, ValueError):
                cfg[k] = 10.0
        cfg["fields"] = [k for k, _ in FIELD_LABELS if self.field_vars[k].get()]
        for k, t in self.texts.items():
            cfg[k] = t.get("1.0", "end-1c")
        return cfg

    # ---- actions
    def pick_color(self):
        c = colorchooser.askcolor(color=self.v["accent"].get() or "#d4881c", parent=self)[1]
        if c:
            self.v["accent"].set(c)

    def save(self):
        name = self.v["name"].get().strip()
        if not name or self.current_id is None:
            self.msg.config(text="Pick or create a layout and give it a name.")
            return
        body = {"name": name, "config": self.collect()}
        self.app.bg(lambda: self.app.api.put(f"/api/templates/{self.current_id}", body),
                    lambda _r: (self.msg.config(text="Saved."), self.app.reload_lookups()),
                    lambda e: self.msg.config(text=str(e)))

    def new(self):
        name = f"New layout {len(self.app.templates) + 1}"
        self.app.bg(lambda: self.app.api.post("/api/templates", {"name": name, "config": dict(self.app.default_layout)}),
                    self._select_new)

    def duplicate(self):
        if self.current_id is None:
            return
        body = {"name": self.v["name"].get().strip() + " (copy)", "config": self.collect()}
        self.app.bg(lambda: self.app.api.post("/api/templates", body), self._select_new)

    def _select_new(self, t):
        self.current_id = t["id"]
        self.app.reload_lookups()

    def delete(self):
        if self.current_id is None:
            return
        cur = next((t for t in self.app.templates if t["id"] == self.current_id), None)
        if cur and cur["is_default"]:
            messagebox.showinfo("Newspaper", "This is the default layout, so it can't be deleted.\n"
                                "Make another layout the default first.", parent=self)
            return
        if messagebox.askyesno("Newspaper", "Delete this layout? Jobs using it will fall back to the default layout.",
                               parent=self):
            tid, self.current_id = self.current_id, None
            self.app.bg(lambda: self.app.api.delete(f"/api/templates/{tid}"), lambda _r: self.app.reload_lookups())

    def set_default(self):
        if self.current_id is None:
            return
        tid = self.current_id
        self.app.bg(lambda: self.app.api.post(f"/api/templates/{tid}/default"), lambda _r: self.app.reload_lookups())

    def reset_factory(self):
        self.load(self.v["name"].get(), self.app.default_layout)
        self.msg.config(text="Editors reset to the factory default. Press 'Save layout' to keep it.")

    def preview(self):
        body = {"layout": self.collect(), "filters": {"limit": 8}}
        self.msg.config(text="Rendering…")
        self.app.bg(lambda: self.app.api.post("/api/render", body),
                    lambda r: (self.app.show_html(r["html"]),
                               self.msg.config(text=f"Preview opened ({r['count']} articles).")),
                    lambda e: self.msg.config(text=str(e)))


# ===================================================================== Jobs
MODES = ("Every day at", "Weekdays (Mon–Fri) at", "On selected days at", "Every N hours", "Every N minutes",
         "Custom cron expression")
DAYS = ((1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"), (5, "Fri"), (6, "Sat"), (0, "Sun"))


def parse_simple_cron(expr):
    """Map common cron strings back to the schedule builder; anything else is 'custom'."""
    m = re.fullmatch(r"(\d+) (\d+) \* \* \*", expr)
    if m:
        return MODES[0], int(m[2]), int(m[1]), [], 1
    m = re.fullmatch(r"(\d+) (\d+) \* \* 1-5", expr)
    if m:
        return MODES[1], int(m[2]), int(m[1]), [], 1
    m = re.fullmatch(r"(\d+) (\d+) \* \* ([0-6](?:,[0-6])*)", expr)
    if m:
        return MODES[2], int(m[2]), int(m[1]), [int(d) for d in m[3].split(",")], 1
    m = re.fullmatch(r"(\d+) \*/(\d+) \* \* \*", expr)
    if m:
        return MODES[3], 0, int(m[1]), [], int(m[2])
    m = re.fullmatch(r"\*/(\d+) \* \* \* \*", expr)
    if m:
        return MODES[4], 0, 0, [], int(m[1])
    return MODES[5], 7, 0, [], 1


class JobsTab(Tab):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        for text, cmd in (("Add job", lambda: JobDialog(app, self)), ("Edit", self.edit), ("Delete", self.delete),
                          ("Enable / disable", self.toggle), ("Run now", self.run_now), ("Refresh", self.reload)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Label(bar, text="Jobs run inside the service, so they fire even when this window is closed.",
                  foreground="#6b7280").pack(side="right")
        cols = (("name", "Job", 190), ("cron", "Schedule (cron)", 110), ("next", "Next run", 120),
                ("last", "Last run", 120), ("status", "Status", 80), ("msg", "Last result", 380),
                ("tpl", "Layout", 130), ("limit", "Page limit", 100), ("action", "Action", 60), ("on", "On", 40))
        self.jobs = self._tree(cols, 8)
        ttk.Label(self, text="Run history", font=app.f_bold).pack(anchor="w", padx=10)
        rcols = (("when", "Started", 130), ("job", "Job", 190), ("trigger", "Trigger", 70), ("status", "Status", 80),
                 ("msg", "Result", 520), ("out", "Output file", 220))
        self.runs = self._tree(rcols, 9)
        self.runs.bind("<Double-1>", self.open_output)
        self.jrows: dict[str, dict] = {}
        self.rrows: dict[str, dict] = {}
        self.jobs.bind("<Double-1>", lambda _e: self.edit())
        self.after(1500, self.poll)

    def _tree(self, cols, height):
        t = ttk.Treeview(self, columns=[c[0] for c in cols], show="headings", selectmode="browse", height=height)
        for key, label, w in cols:
            t.heading(key, text=label)
            t.column(key, width=w, anchor="w")
        for tag, col in STATUS_COLORS.items():
            t.tag_configure(tag, foreground=col)
        t.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        return t

    def poll(self):
        if self.app.online and self.winfo_ismapped():
            self.reload()
        self.after(4000, self.poll)

    def on_lookups(self):
        self.reload()

    def reload(self):
        self.app.bg(lambda: (self.app.api.get("/api/jobs"), self.app.api.get("/api/runs", params={"limit": 40})),
                    self.fill, lambda e: None)

    def fill(self, res):
        jobs, runs = res
        sel = self.jobs.selection()
        self.jobs.delete(*self.jobs.get_children())
        self.jrows = {str(j["id"]): j for j in jobs}
        for j in jobs:
            st = j["last_status"] or ""
            self.jobs.insert("", "end", iid=str(j["id"]), tags=(st,) if st else (), values=(
                self.app.display(j["name"]), j["cron"],
                fmt_ts(j["next_run"], "%a %d %b %H:%M") if j["enabled"] else "disabled",
                fmt_ts(j["last_run"], "%d %b %H:%M"), st or "—", self.app.display(truncate(j["last_message"] or "", 80)),
                self.app.display(j["template_name"] or "default"), limit_text(j), j["action"],
                "✔" if j["enabled"] else "✖"))
        if sel and self.jobs.exists(sel[0]):
            self.jobs.selection_set(sel[0])
        self.runs.delete(*self.runs.get_children())
        self.rrows = {str(r["id"]): r for r in runs}
        for r in runs:
            self.runs.insert("", "end", iid=str(r["id"]), tags=(r["status"],), values=(
                fmt_ts(r["started"], "%d %b %H:%M:%S"), self.app.display(r["job_name"]), r["trigger"], r["status"],
                self.app.display(truncate(r["message"] or "", 110)), Path(r["output"]).name if r["output"] else ""))

    def current(self):
        sel = self.jobs.selection()
        if not sel:
            messagebox.showinfo("Newspaper", "Select a job first.", parent=self)
            return None
        return self.jrows[sel[0]]

    def edit(self):
        j = self.current()
        if j:
            JobDialog(self.app, self, job=j)

    def delete(self):
        j = self.current()
        if j and messagebox.askyesno("Newspaper", f"Delete job '{j['name']}'?", parent=self):
            self.app.bg(lambda: self.app.api.delete(f"/api/jobs/{j['id']}"), lambda _r: self.reload())

    def toggle(self):
        j = self.current()
        if j:
            body = {**j, "enabled": not j["enabled"]}
            self.app.bg(lambda: self.app.api.put(f"/api/jobs/{j['id']}", body), lambda _r: self.reload())

    def run_now(self):
        j = self.current()
        if j:
            self.app.bg(lambda: self.app.api.post(f"/api/jobs/{j['id']}/run"),
                        lambda _r: self.after(1500, self.reload))

    def open_output(self, _e):
        sel = self.runs.selection()
        if sel and self.rrows[sel[0]].get("output") and Path(self.rrows[sel[0]]["output"]).exists():
            open_path(self.rrows[sel[0]]["output"])


class JobDialog(tk.Toplevel):
    def __init__(self, app, tab, job=None):
        super().__init__(app)
        self.app, self.tab, self.job = app, tab, job
        self.title("Edit print job" if job else "New print job")
        self.transient(app)
        self.resizable(False, False)
        self._preview_gen = 0
        mode, hh, mm, days, n = parse_simple_cron(job["cron"]) if job else (MODES[0], 7, 0, [1, 2, 3, 4, 5], 1)
        flt = (job or {}).get("filters", {})

        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        self.name = tk.StringVar(value=job["name"] if job else "Morning news")
        form_row(f, 0, "Job name", ttk.Entry(f, textvariable=self.name, width=50))

        # ---- schedule builder
        sb = ttk.LabelFrame(f, text="Schedule", padding=8)
        sb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        self.mode = tk.StringVar(value=mode)
        self.hh, self.mm, self.n = tk.StringVar(value=f"{hh:02d}"), tk.StringVar(value=f"{mm:02d}"), tk.StringVar(value=str(n))
        self.custom = tk.StringVar(value=job["cron"] if job else "0 7 * * *")
        self.day_vars = {d: tk.BooleanVar(value=d in days) for d, _ in DAYS}
        mc = ttk.Combobox(sb, textvariable=self.mode, values=MODES, state="readonly", width=26)
        mc.grid(row=0, column=0, sticky="w")
        mc.bind("<<ComboboxSelected>>", lambda _e: self.mode_changed())
        self.time_f = ttk.Frame(sb)
        ttk.Label(self.time_f, text="at").pack(side="left", padx=(0, 4))
        self.hh_sp = ttk.Spinbox(self.time_f, from_=0, to=23, width=4, format="%02.0f", textvariable=self.hh, wrap=True)
        self.hh_sp.pack(side="left")
        ttk.Label(self.time_f, text=":").pack(side="left")
        ttk.Spinbox(self.time_f, from_=0, to=59, width=4, format="%02.0f", textvariable=self.mm, wrap=True).pack(side="left")
        self.time_f.grid(row=0, column=1, padx=10)
        self.days_f = ttk.Frame(sb)
        for d, label in DAYS:
            ttk.Checkbutton(self.days_f, text=label, variable=self.day_vars[d]).pack(side="left", padx=(0, 6))
        self.days_f.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        self.every_f = ttk.Frame(sb)
        ttk.Label(self.every_f, text="N =").pack(side="left")
        ttk.Spinbox(self.every_f, from_=1, to=59, width=4, textvariable=self.n).pack(side="left", padx=4)
        self.every_note = ttk.Label(self.every_f, text="", foreground="#6b7280")
        self.every_note.pack(side="left", padx=6)
        self.every_f.grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        self.custom_f = ttk.Frame(sb)
        ttk.Label(self.custom_f, text="min hour day month weekday:").pack(side="left")
        ttk.Entry(self.custom_f, textvariable=self.custom, width=24).pack(side="left", padx=6)
        self.custom_f.grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        self.cron_lbl = ttk.Label(sb, text="", font=app.f_bold)
        self.cron_lbl.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.next_lbl = ttk.Label(sb, text="", wraplength=520, foreground="#374151")
        self.next_lbl.grid(row=5, column=0, columnspan=2, sticky="w")
        for var in (self.mode, self.hh, self.mm, self.n, self.custom, *self.day_vars.values()):
            var.trace_add("write", lambda *_: self.schedule_changed())

        # ---- what to print
        self.tpl_by_name, default = template_choices(app)
        self.tpl = ttk.Combobox(f, state="readonly", values=list(self.tpl_by_name), width=34)
        cur = next((k for k, t in self.tpl_by_name.items() if job and t["id"] == job["template_id"]), None)
        self.tpl.set(cur or default)
        form_row(f, 2, "Print layout", self.tpl)

        sf = ttk.Frame(f)
        self.src_lb = tk.Listbox(sf, selectmode="extended", height=4, exportselection=False)
        for i, s in enumerate(app.sources):
            self.src_lb.insert("end", app.display(s["name"]))
            if s["id"] in flt.get("source_ids", []):
                self.src_lb.selection_set(i)
        self.src_lb.pack(fill="x")
        ttk.Label(sf, text="none selected = all enabled sources", foreground="#6b7280").pack(anchor="w")
        form_row(f, 3, "Sources", sf)

        ff = ttk.Frame(f)
        self.lang = tk.StringVar(value=flt.get("lang", ""))
        self.hours = tk.IntVar(value=flt.get("hours", 24))
        self.limit = tk.IntVar(value=flt.get("limit", 20))
        ttk.Label(ff, text="Language").pack(side="left")
        ttk.Combobox(ff, textvariable=self.lang, values=["", "en", "ar"], width=5, state="readonly").pack(side="left", padx=(4, 12))
        ttk.Label(ff, text="Last").pack(side="left")
        ttk.Spinbox(ff, from_=0, to=720, width=5, textvariable=self.hours).pack(side="left", padx=4)
        ttk.Label(ff, text="hours (0 = any)   Max articles").pack(side="left")
        ttk.Spinbox(ff, from_=1, to=200, width=5, textvariable=self.limit).pack(side="left", padx=4)
        form_row(f, 4, "Articles", ff)
        kf = ttk.Frame(f)
        self.keyword = tk.StringVar(value=flt.get("keyword", ""))
        # jobs saved by older versions have a single "category"; it becomes a one-item selection
        sel = flt.get("categories")
        self.cat_sel = [str(c) for c in sel] if isinstance(sel, list) else ([flt["category"]] if flt.get("category") else None)
        ttk.Label(kf, text="Keyword").pack(side="left")
        ttk.Entry(kf, textvariable=self.keyword, width=18).pack(side="left", padx=(4, 12))
        self.cat_btn = ttk.Button(kf, text=categories_button_text(app, self.cat_sel), command=self.pick_categories)
        self.cat_btn.pack(side="left")
        form_row(f, 5, "Optional filters", kf)

        # ---- output
        self.action = tk.StringVar(value=job["action"] if job else "print")
        af = ttk.Frame(f)
        ttk.Radiobutton(af, text="Print", variable=self.action, value="print").pack(side="left")
        ttk.Radiobutton(af, text="Save PDF/HTML only", variable=self.action, value="save").pack(side="left", padx=12)
        form_row(f, 6, "Action", af)
        self.printer = tk.StringVar(value=job["printer"] if job else "")
        form_row(f, 7, "Printer name", ttk.Entry(f, textvariable=self.printer))
        self.max_pages, self.over_limit = add_limit_rows(f, 8, job["max_pages"] if job else 0,
                                                         job["over_limit"] if job else "fit")
        self.refresh_first = tk.BooleanVar(value=job["refresh_first"] if job else True)
        self.enabled = tk.BooleanVar(value=job["enabled"] if job else True)
        cf = ttk.Frame(f)
        ttk.Checkbutton(cf, text="Fetch the sources first (recommended)", variable=self.refresh_first).pack(side="left")
        ttk.Checkbutton(cf, text="Enabled", variable=self.enabled).pack(side="left", padx=14)
        form_row(f, 10, "", cf)
        self.msg = ttk.Label(f, text="", foreground="#b91c1c", wraplength=520)
        self.msg.grid(row=11, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(f)
        bar.grid(row=12, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(bar, text="Save", command=self.save).pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        self.mode_changed()

    def pick_categories(self):
        CategoryPicker(self.app, self.cat_sel, self.set_categories, "Categories for this job")

    def set_categories(self, selected):
        self.cat_sel = selected
        self.cat_btn.config(text=categories_button_text(self.app, selected))

    def mode_changed(self):
        m = self.mode.get()
        show = {self.time_f: m in MODES[:3], self.days_f: m == MODES[2],
                self.every_f: m in MODES[3:5], self.custom_f: m == MODES[5]}
        for w, on in show.items():
            w.grid() if on else w.grid_remove()
        self.every_note.config(text={MODES[3]: "hours, at minute →", MODES[4]: "minutes"}.get(m, ""))
        if m == MODES[3]:
            self.time_f.grid()
            self.hh_sp.state(["disabled"])
        else:
            self.hh_sp.state(["!disabled"])
        self.schedule_changed()

    def build_cron(self):
        m = self.mode.get()
        hh, mm, n = to_int(self.hh, 7), to_int(self.mm, 0), max(1, to_int(self.n, 1))
        if m == MODES[0]:
            return f"{mm} {hh} * * *"
        if m == MODES[1]:
            return f"{mm} {hh} * * 1-5"
        if m == MODES[2]:
            days = ",".join(str(d) for d, _ in DAYS if self.day_vars[d].get())
            return f"{mm} {hh} * * {days}" if days else ""
        if m == MODES[3]:
            return f"{mm} */{n} * * *"
        if m == MODES[4]:
            return f"*/{n} * * * *"
        return self.custom.get().strip()

    def schedule_changed(self):
        expr = self.build_cron()
        self.cron_lbl.config(text=f"Cron: {expr or '(choose at least one day)'}")
        self._preview_gen += 1
        gen = self._preview_gen
        if not expr:
            self.next_lbl.config(text="")
            return

        def ok(r):
            if gen == self._preview_gen:
                self.next_lbl.config(text="Next runs: " + "   |   ".join(r["text"]), foreground="#374151")

        def err(e):
            if gen == self._preview_gen:
                self.next_lbl.config(text=str(e), foreground="#b91c1c")
        self.after(250, lambda: gen == self._preview_gen and self.app.bg(
            lambda: self.app.api.get("/api/cron/preview", params={"expr": expr, "count": 3}), ok, err))

    def save(self):
        expr = self.build_cron()
        if not self.name.get().strip() or not expr:
            self.msg.config(text="Give the job a name and a valid schedule.")
            return
        tpl = self.tpl_by_name.get(self.tpl.get())
        filters = {"hours": to_int(self.hours, 0), "limit": max(1, to_int(self.limit, 20))}
        ids = [self.app.sources[i]["id"] for i in self.src_lb.curselection()]
        if ids:
            filters["source_ids"] = ids
        for key, var in (("lang", self.lang), ("keyword", self.keyword)):
            if var.get().strip():
                filters[key] = var.get().strip()
        if self.cat_sel is not None:
            if not self.cat_sel:
                self.msg.config(text="No category is ticked, so nothing would be printed. Tick some, or choose all.")
                return
            filters["categories"] = list(self.cat_sel)
        body = {"name": self.name.get().strip(), "cron": expr, "enabled": self.enabled.get(),
                "template_id": tpl["id"] if tpl else None, "filters": filters, "action": self.action.get(),
                "printer": self.printer.get().strip(), "refresh_first": self.refresh_first.get(),
                "max_pages": max(0, to_int(self.max_pages, 0)), "over_limit": policy_of(self.over_limit.get())}
        call = (lambda: self.app.api.put(f"/api/jobs/{self.job['id']}", body)) if self.job \
            else (lambda: self.app.api.post("/api/jobs", body))
        self.app.bg(call, lambda _r: (self.tab.reload(), self.destroy()), lambda e: self.msg.config(text=str(e)))


# ================================================================== Service
class ServiceTab(Tab):
    FIX_LABELS = {"auto": "Automatic (fixed on Linux/BSD, untouched on Windows/macOS)",
                  "on": "Always fix (Tk shows Arabic joined wrongly or reversed)",
                  "off": "Never fix (Tk already shows Arabic correctly)"}

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.data_dir = None
        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        self.state_lbl = tk.Label(top, text="Checking…", font=app.f_title, anchor="w")
        self.state_lbl.pack(anchor="w")
        self.detail = ttk.Label(top, text="", justify="left", wraplength=1050)
        self.detail.pack(anchor="w", pady=(0, 4))
        ttk.Label(top, foreground="#6b7280", text="The service runs your print schedule (fetching the sources first) in "
                  "the background. Closing this window does not stop it.").pack(anchor="w", pady=(0, 8))
        bar = ttk.Frame(top)
        bar.pack(anchor="w")
        for text, cmd in (("Start service", self.start_service), ("Stop service", self.stop_service),
                          ("Restart", self.restart), ("Connect…", lambda: ConnectDialog(app)),
                          ("Detect again", self.detect_again)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        bar2 = ttk.Frame(top)
        bar2.pack(anchor="w", pady=6)
        ttk.Label(bar2, text="Start my own service with this computer:").pack(side="left")
        for text, act in (("Enable", "install"), ("Disable", "remove"), ("Check", "status")):
            ttk.Button(bar2, text=text, command=lambda a=act: self.autostart(a)).pack(side="left", padx=(6, 0))
        bar4 = ttk.Frame(top)
        bar4.pack(anchor="w", pady=(2, 4))
        ttk.Label(bar4, text="Arabic text in this window:").pack(side="left")
        self.fix_var = tk.StringVar(value=self.FIX_LABELS[app.arabic_mode])
        fix_cb = ttk.Combobox(bar4, textvariable=self.fix_var, state="readonly", width=46,
                              values=list(self.FIX_LABELS.values()))
        fix_cb.pack(side="left", padx=6)
        fix_cb.bind("<<ComboboxSelected>>", lambda _e: app.set_arabic_mode(
            next(k for k, v in self.FIX_LABELS.items() if v == self.fix_var.get())))
        bar5 = ttk.Frame(top)
        bar5.pack(anchor="w", pady=(2, 4))
        ttk.Label(bar5, text="Keep articles for").pack(side="left")
        self.keep_days = tk.IntVar(value=7)
        self.keep_spin = ttk.Spinbox(bar5, from_=1, to=3650, width=6, textvariable=self.keep_days)
        self.keep_spin.pack(side="left", padx=6)
        ttk.Label(bar5, text="days").pack(side="left")
        ttk.Button(bar5, text="Apply", command=self.apply_retention).pack(side="left", padx=(8, 0))
        self.keep_msg = ttk.Label(bar5, text="", foreground="#6b7280")
        self.keep_msg.pack(side="left", padx=8)
        self.kept_days = None  # the service's current value (None until the first status arrives)
        self.info = ttk.Label(top, justify="left", text="")
        self.info.pack(anchor="w", pady=6)
        bar3 = ttk.Frame(top)
        bar3.pack(anchor="w")
        ttk.Button(bar3, text="Open data folder", command=lambda: open_path(self.data_dir or DATA_DIR)).pack(side="left")
        ttk.Button(bar3, text="Open printed files folder",
                   command=lambda: open_path(Path(self.data_dir or DATA_DIR) / "prints")).pack(side="left", padx=6)
        ttk.Label(self, text="Service log", font=app.f_bold).pack(anchor="w", padx=12)
        self.log = scrolledtext.ScrolledText(self, height=14, font="TkFixedFont", state="disabled")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.after(2000, self.poll_log)

    # ---- helpers
    def is_system(self) -> bool:
        c, rep = self.app.api.conn, self.app.api.report or {}
        return bool((c and c["label"] == "system service") or rep.get("denied")
                    or any(l == "system service" for l, _ in rep.get("not_running", [])))

    def set_log(self, text):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll_log(self):
        if self.winfo_ismapped():
            if self.app.online:
                self.app.bg(lambda: self.app.api.get("/api/log", params={"lines": 200}),
                            lambda r: self.set_log("\n".join(r["lines"])), lambda e: None)
            else:
                try:
                    lines = (Path(self.data_dir or DATA_DIR) / "service.log").read_text("utf-8", errors="replace")
                    self.set_log("\n".join(lines.splitlines()[-200:]))
                except OSError:
                    self.set_log("(the service is not reachable and its log cannot be read from here)")
        self.after(4000, self.poll_log)

    def show_status(self, st):
        if st is None:
            self.state_lbl.config(text="●  Not reachable", fg="#b91c1c")
            self.detail.config(text=self.app.down_reason)
            self.info.config(text="")
            return
        c, p = self.app.api.conn or {}, st["printing"]
        self.data_dir = st["data_dir"]
        eng = p.get("pdf_engine")
        pdf = (f"{eng['name']} {eng['version']}" if eng else
               f"NOT AVAILABLE ({p.get('pdf_error') or 'not installed'}). Install: {p.get('pdf_hint', 'pip install weasyprint')}")
        plat = p.get("platform", "")
        if plat.startswith("win"):
            send = "SumatraPDF" if p.get("sumatra") else "Windows default PDF printer"
        elif plat.startswith("freebsd"):
            send = "CUPS 'lpr-cups'" if p.get("lpr_cups") else "NOT FOUND ('lpr-cups' is missing: pkg install cups)"
        else:
            send = ("CUPS 'lp'" if p.get("lp") else "'lpr'" if p.get("lpr")
                    else "NOT FOUND (needs 'lp' or 'lpr', e.g. CUPS)")
        ceiling = (st.get("limits") or {}).get("max_pages_per_job", 0)
        ceiling_txt = (f"{ceiling} pages per job (max_pages_per_job in config.json)" if ceiling
                       else "none (set max_pages_per_job in config.json to add one)")
        keep = (st.get("limits") or {}).get("retention_days")
        if keep and keep != self.kept_days:
            if self.focus_get() is not self.keep_spin:  # don't overwrite what the user is typing
                self.keep_days.set(keep)
            self.kept_days = keep
        keep_txt = f"{keep} days (older articles are deleted)" if keep else "unknown (older service version)"
        up = st["uptime_s"]
        self.state_lbl.config(text="●  Running", fg="#15803d")
        self.detail.config(text=f"Connected to the {c.get('label', 'service')}  ({c.get('base', '')}, from {c.get('where', '')})")
        self.info.config(text=(f"Uptime: {up // 3600}h {up % 3600 // 60}m\n"
                               f"Data folder: {st['data_dir']}\nPrinted files: {st['output_dir']}\n"
                               f"PDF engine: {pdf}\nSending to printer via: {send}\n"
                               f"Service-wide page ceiling: {ceiling_txt}\nArticles kept: {keep_txt}"))

    # ---- actions
    def apply_retention(self):
        days = to_int(self.keep_days, 0)
        if not 1 <= days <= 3650:
            messagebox.showerror("Newspaper", "Enter a number of days from 1 to 3650.", parent=self)
            return
        if not self.app.online:
            messagebox.showerror("Newspaper", "The service is not running, so the setting cannot be changed.",
                                 parent=self)
            return
        if self.kept_days and days < self.kept_days and not messagebox.askyesno(
                "Newspaper", f"Articles older than {days} days will be deleted now.\nContinue?", parent=self):
            return
        self.keep_msg.config(text="Saving…")

        def ok(r):
            self.kept_days = r["retention_days"]
            self.keep_days.set(r["retention_days"])
            n = r.get("pruned", 0)
            self.keep_msg.config(text=f"Saved. Deleted {n} old article{'s' if n != 1 else ''}." if n else "Saved.")
            self.app.check_status()
            if n:
                self.app.reload_lookups(self.app.tabs["News"].load)

        def err(e):
            self.keep_msg.config(text="")
            self.app.report(e)
        self.app.bg(lambda: self.app.api.put("/api/settings", {"retention_days": days}), ok, err)

    def start_service(self):
        if self.is_system():
            messagebox.showinfo("Newspaper", "This computer runs Newspaper as a system service, which the system starts "
                                "(not this window). As root:\n\n    service newspaper start\n    service newspaper status\n\n"
                                "If it will not start, that command prints the reason.", parent=self)
            return
        script = SERVICE_DIR / "newspaper_service.py"
        if not script.exists():
            messagebox.showerror("Newspaper", f"Cannot find {script}\nKeep the 'service' and 'ui' folders together.")
            return
        self.state_lbl.config(text="●  Starting…", fg="#b45309")
        if WIN:
            exe = sys.executable
            pyw = Path(exe).with_name("pythonw.exe")
            try:
                subprocess.Popen([str(pyw) if pyw.exists() else exe, str(script), "run"], cwd=str(SERVICE_DIR),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=0x00000008 | 0x00000200)
            except OSError as exc:
                messagebox.showerror("Newspaper", f"Could not start the service:\n{exc}")
                return
            self.after(2500, self.app.check_status)
            return

        def run():  # `start` daemonises and reports why it could not
            r = subprocess.run([sys.executable, str(script), "start"], cwd=str(SERVICE_DIR), capture_output=True,
                               text=True, timeout=90)
            return r.returncode, (r.stdout + r.stderr).strip()

        def done(res):
            if res[0] != 0:
                messagebox.showerror("Could not start the service", res[1], parent=self)
            self.app.api.conn = None
            self.app.check_status()
        self.app.bg(run, done)

    def stop_service(self):
        self.app.bg(lambda: self.app.api.post("/api/shutdown"), lambda _r: self.after(1500, self.app.check_status))

    def restart(self):
        if self.is_system():
            messagebox.showinfo("Newspaper", "As root:   service newspaper restart", parent=self)
            return
        self.app.bg(lambda: self.app.api.post("/api/shutdown"), lambda _r: self.after(2000, self.start_service),
                    lambda _e: self.start_service())

    def detect_again(self):
        self.app.api.conn = None
        self.state_lbl.config(text="Checking…", fg="#374151")
        self.app.check_status()

    def autostart(self, action):
        if self.is_system():
            messagebox.showinfo("Newspaper", "The system service starts at boot through /etc/rc.conf "
                                "(sysrc newspaper_enable=YES). Nothing to do here.", parent=self)
            return

        def run():
            r = subprocess.run([sys.executable, str(SERVICE_DIR / "autostart.py"), action],
                               capture_output=True, text=True, timeout=60)
            return (r.stdout + r.stderr).strip() or "Done."
        self.app.bg(run, lambda out: messagebox.showinfo("Start with my computer", out, parent=self))


class ConnectDialog(tk.Toplevel):
    """Connect to a service explicitly: one whose token file this account cannot read, or on another machine."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Connect to a Newspaper service")
        self.transient(app)
        self.resizable(False, False)
        cur = app.api.manual() or {}
        self.url = tk.StringVar(value=cur.get("url", "http://127.0.0.1:8765"))
        self.token = tk.StringVar(value=cur.get("token", ""))
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, wraplength=480, justify="left", foreground="#374151", text=(
            "Normally the UI finds the service by itself. Use this if the service's access token is not readable by "
            "your account (e.g. a FreeBSD system service), or the service runs on another machine.\n\n"
            "Get the details on the service machine with:   service newspaper token")
                  ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        form_row(f, 1, "Address", ttk.Entry(f, textvariable=self.url, width=44))
        form_row(f, 2, "Access token", ttk.Entry(f, textvariable=self.token, show="•"))
        self.msg = ttk.Label(f, text="", wraplength=480)
        self.msg.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        bar = ttk.Frame(f)
        bar.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="Test", command=self.test).pack(side="left")
        ttk.Button(bar, text="Save and connect", command=self.save).pack(side="left", padx=6)
        ttk.Button(bar, text="Forget saved", command=self.forget).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="left", padx=(6, 0))

    def test(self):
        url, token = self.url.get().strip().rstrip("/"), self.token.get().strip()

        def probe():
            r = requests.get(url + "/api/status", headers={"X-Newspaper-Token": token}, timeout=5)
            if r.status_code == 401:
                raise ApiError("The service is there but the token is wrong.")
            r.raise_for_status()
            return r.json()
        self.msg.config(text="Testing…")
        self.app.bg(probe, lambda st: self.msg.config(text=f"Connected: {st['counts']['articles']} articles, "
                                                           f"{st['counts']['sources']} sources."),
                    lambda e: self.msg.config(text=f"Failed: {e}"))

    def save(self):
        if not self.url.get().strip() or not self.token.get().strip():
            self.msg.config(text="Enter the address and the token.")
            return
        self.app.api.save_manual(self.url.get(), self.token.get())
        self.app.api.conn = None
        self.app.check_status()
        self.destroy()

    def forget(self):
        self.app.api.forget_manual()
        self.app.api.conn = None
        self.app.check_status()
        self.msg.config(text="Saved connection removed; the UI will look for a service by itself.")


if __name__ == "__main__":
    App().mainloop()
