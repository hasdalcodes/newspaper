# Newspaper

Collect news from RSS/Atom feeds (and the JSON files an Al Jazeera scraper commits to GitHub), read it in a
desktop app, and print it as a newspaper on a schedule.

It is two separate programs that talk over a local HTTP API:

```
┌──────────────────────┐   HTTP + token    ┌──────────────────────────────────────────────┐
│ ui/  (Tk desktop app) │ ───────────────▶ │ service/ (background service)                 │
│ a remote control:     │   127.0.0.1:8765  │ fetches feeds · stores articles (SQLite)      │
│ closing it does NOT   │                   │ renders HTML → PDF (WeasyPrint) → printer     │
│ stop the service      │                   │ runs the cron print jobs · prunes old articles │
└──────────────────────┘                   └──────────────────────────────────────────────┘
```

Pure Python. The only dependencies are `requests` and `weasyprint` (for PDFs and printing).

---

## Contents
- [Features](#features)
- [Quick start](#quick-start)
- [Requirements](#requirements)
- [FreeBSD system service](#freebsd-system-service)
- [Running the service](#running-the-service)
- [Configuration](#configuration)
- [Article retention](#article-retention)
- [Using the app](#using-the-app)
- [Arabic and right-to-left text](#arabic-and-right-to-left-text)
- [Files and data](#files-and-data)
- [HTTP API](#http-api)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

## Features
- **Sources.** Any RSS 2.0, RSS 1.0 or Atom feed can be a source, optionally with the full article text downloaded. The JSON files of an Al Jazeera scraper on GitHub can be one too, with a token for private repos.
- **Feed catalog.** A built-in catalog has 45 well-known feeds: BBC, Al Jazeera, Guardian, NYT, NPR, DW, France 24, Arabic-language outlets, business, tech and sport.
- **News tab.** Articles appear as cards. You can search, filter by source, language, time and categories, and open the full text.
- **Print layouts.** You choose the card fields, columns, paper, fonts and colours, or edit the HTML templates directly.
- **Scheduled print jobs.** Jobs use cron syntax and show a preview of the next runs, plus a run history. Each job and each manual print can have a page limit.
- **Automatic retention.** Articles older than *N* days (default 7) are deleted. You set the number in the app.
- **Arabic and RTL support** in the Tk UI on X11 (Linux and FreeBSD), with no extra packages.
- **Autostart.** It can start with the computer on Windows, macOS, Linux (systemd), BSD (crontab), or as a FreeBSD rc.d system service.

## Quick start
```sh
pip install -r requirements.txt
python ui/newspaper_ui.py          # finds a running service, or offers to start one
```
A fresh install adds the GitHub scraper source plus five starter feeds, fetches them once, and creates a default print layout.

## Requirements
| | |
|---|---|
| Python | 3.10+ (with the `sqlite3` module) |
| Python packages | `pip install -r requirements.txt` (`requests`, `weasyprint`) |
| UI | Tkinter. It is included on Windows and macOS. Debian/Ubuntu: `sudo apt install python3-tk`. FreeBSD: `pkg install py311-tkinter` |
| PDF and printing | WeasyPrint needs Pango. Linux: `apt install libpango-1.0-0`. macOS: `brew install pango`. Windows: the GTK3 runtime (see the WeasyPrint docs) |
| Fonts | A font for your language. Arabic needs an Arabic font such as Noto or DejaVu, or it prints as empty boxes |
| Printer | Linux/macOS: CUPS `lp` (else `lpr`). FreeBSD: `lpr-cups`. Windows: SumatraPDF if installed, otherwise the default PDF app |

Without WeasyPrint everything still works except PDF output, printing and page limits. The service tells you how to install it.

## FreeBSD system service
```sh
pkg install python3 py311-requests py311-weasyprint py311-sqlite3 cups   # py311-* must match your Python version
# as root, from the project folder:
sh freebsd/install.sh        # creates user+group "newspaper", /var/db/newspaper, installs the rc.d script, sets rc.conf
service newspaper start      # prints the reason if it cannot start
service newspaper status
```
- The service starts as root, drops to the `newspaper` account, and keeps its data in `/var/db/newspaper`.
- `service newspaper start|stop|restart|status|token` all work.
- Settings live in `/etc/rc.conf`: `newspaper_enable`, `newspaper_user`, `newspaper_group`, `newspaper_data`, `newspaper_python`, `newspaper_flags`.
- **UI on the same machine** (`pkg install py311-tkinter`):
  - The UI finds `/var/db/newspaper` by itself, but the access token can only be read by the `newspaper` group.
  - Either run `pw groupmod newspaper -m YOURLOGIN` and log in again,
  - or run `service newspaper token` and paste the result into **UI → Service → Connect…**.
- **UI on another machine:**
  - Run `sysrc newspaper_flags="--host 0.0.0.0"`, restart, then use **Connect…** with `http://SERVER:8765` and the token.
  - The API is plain HTTP protected only by the token. Keep it on a trusted network or tunnel it: `ssh -L 8765:127.0.0.1:8765 server`.
- **Printing:**
  - Pages are turned into PDFs and sent with `lpr-cups`, the CUPS client from `pkg install cups`. FreeBSD's own `lp`/`lpr` talk to the old BSD spooler and are not used.
  - Add your printer at `http://localhost:631`. If it is not the default printer, give its name in the job.
- **Remove** with `sh freebsd/uninstall.sh`. Your data is kept.
- **Without root,** `python3 service/newspaper_service.py start` runs a per-user service (`~/.newspaper`).

## Running the service
The service is a program of its own:
```sh
python service/newspaper_service.py start     # background; waits until it is up and prints why if it cannot
python service/newspaper_service.py status | stop | restart
python service/newspaper_service.py token     # URL + access token the UI needs
python service/newspaper_service.py run       # foreground (debugging, systemd, launchd)
```
| Option | Meaning |
|---|---|
| `--data-dir DIR` | where the database, config, log and printed files live (default `~/.newspaper`) |
| `--host H --port P` | listen address. It is saved into `config.json` so the UI can find it |
| `--user U --group G` | when started as root, switch to this account (system service) |
| `--pidfile FILE` | where `start` records the pid (default `DATA_DIR/service.pid`) |

**Per-user autostart:** UI → Service tab → "Start my own service with this computer: **Enable**". This uses a Windows scheduled task, a macOS LaunchAgent, a Linux systemd user service, or a BSD crontab `@reboot` entry.

The UI looks for a service in this order: a connection saved with **Connect…**, `$NEWSPAPER_HOME`, `~/.newspaper`, `/var/db/newspaper`.

## Configuration
`config.json` in the data folder is created on first start. Missing keys are filled in with defaults. Restart the service after editing it by hand. The retention setting can also be changed live from the UI.

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | listen address |
| `port` | `8765` | listen port |
| `token` | random | access token every API request must send |
| `retention_days` | `7` | delete articles older than this many days. See [Article retention](#article-retention) |
| `history_days` | `90` | keep job run history this many days |
| `misfire_grace_s` | `600` | a job that is late by more than this many seconds (service was off) is recorded as *skipped*, not run late |
| `max_pages_per_job` | `0` | service-wide page ceiling for any print job. `0` means none |

| Environment variable | Meaning |
|---|---|
| `NEWSPAPER_HOME` | data folder (same as `--data-dir`) |
| `NEWSPAPER_ARABIC_FIX` | `on` / `off` / `auto`. Arabic display fix in the UI |

## Article retention
- **Setting it:** the service keeps articles for **7 days** by default. Change it in **UI → Service → "Keep articles for N days" → Apply** (1–3650). You can also set it with `PUT /api/settings {"retention_days": N}` or with `retention_days` in `config.json`.
- **How age is counted:** by the article's *published* date. The fetch date is used when a feed gives no date.
- **When deletion happens:** old articles are deleted every 6 hours, and right away when you lower the limit. The UI asks first and then tells you how many were deleted.
- **Old feed items are never stored.** An item that is already older than the limit when fetched is skipped, and no full text is downloaded for it. So old items don't keep coming back after each cleanup.
- **Job history** is pruned separately (`history_days`, default 90).
- **Upgrading:** an install from an older version keeps the value already in its `config.json` (it used to be 90). Lower it in the UI if you want.

## Using the app
- **When things are fetched.** Nothing polls in the background.
  - A print job fetches its sources first, then prints. Fetches run in parallel with a time limit, so a dead feed cannot hold up the printout. The job result says what was fetched.
  - The News tab's **Fetch new articles now** and the Sources tab's **Fetch** buttons fetch on request.
  - A source can opt in to polling with "Auto-fetch every N minutes" (0, the default, means on demand).
- **Sources.**
  - Any RSS or Atom feed, optionally with the full article text downloaded.
  - The Al Jazeera scraper's JSON on GitHub. The default is the `data` folder of `hasdalcodes/news`. Add a token for private repos.
  - **Browse popular feeds** opens the built-in catalog. Tick feeds, press **Check** to test them live, then **Add**.
- **News tab.**
  - Cards show the title, source, summary and date. Click a card (or **Read**) for the full text.
  - Cards load a few at a time, so the window stays responsive. **Load more** adds the next page.
- **Categories** (News tab and print jobs).
  - The **Categories** button opens a checklist of every category with its article count, plus "(uncategorized)". Select all / none / invert and a filter box help with long lists.
  - With everything ticked there is no filter, so categories that appear later are included. Once you untick something, the list is explicit and new categories are left out.
  - The News tab remembers your choice between sessions. Each job stores its own list.
- **Page limit.** Each job and each manual print can have a maximum number of pages (0 means no limit). When a job would be longer, you choose what happens:
  - *print fewer articles* (the default): as many whole articles as fit, newest first, so nothing is cut mid-article.
  - *print only the first pages*.
  - *don't print*: the full PDF is kept and the job shows status "saved".

  Results state what was done, e.g. "Printed 4 articles (4 pages) [4 of 10 articles fit in 4 pages (limit 4)]". The service-wide `max_pages_per_job` applies whenever it is stricter than the job's own limit.
- **Print layouts.**
  - There is always a **default layout** (marked ★). It cannot be deleted, only replaced by making another layout the default. Anything that doesn't name a layout uses it.
  - *Page HTML*: `{title} {css} {header} {cards} {footer} {script}`. It must contain `{cards}`.
  - *Card HTML*: `{title} {source} {datetime} {category} {author} {summary} {content} {url} {meta} {dir} {index}`. Wrap a part in `[[if:summary]]…[[/if]]` to show it only when that field is ticked and has a value.
  - *Header / Footer HTML*: `{title} {subtitle} {date} {time} {count}`.
  - *Custom CSS* is added after the generated CSS.
  - Clear an editor to get its default back, or press **Reset to factory default**.
- **Scheduler.** Jobs use the builder or raw cron syntax, with a preview of the next runs, status and run history. Runs missed while the service was off are recorded as "skipped".
- **Printing.**
  - The steps are HTML → PDF (WeasyPrint) → printer.
  - If printing is impossible, the files are still saved and the job shows status "saved" with the reason.
  - "Save only" jobs never use a printer.

## Arabic and right-to-left text
Tk on Linux and FreeBSD (X11) cannot join Arabic letters or put them in right-to-left order. The UI therefore shapes, reorders and wraps the text itself (`ui/textshape.py`, standard library only). This applies to news cards, the article window, drop-downs, tables and lists. Windows and macOS Tk render Arabic correctly, so nothing is changed there.
- It is automatic. To override it, use **UI → Service → "Arabic text in this window"** (Automatic / Always fix / Never fix) or set `NEWSPAPER_ARABIC_FIX`.
- It needs a font that has the Arabic presentation forms. DejaVu Sans does (FreeBSD: `pkg install dejavu`).
- Text you *type* into entry boxes is still drawn by Tk and looks unjoined while you type. It is stored and searched correctly.
- PDFs and HTML pages are not affected by any of this.

## Files and data
Everything lives in the data folder: `~/.newspaper`, `$NEWSPAPER_HOME`, `--data-dir`, or `/var/db/newspaper` for the FreeBSD service.

| File | Contents |
|---|---|
| `newspaper.db` | SQLite database: sources, articles, layouts, jobs, run history |
| `config.json` | settings plus the access token (the UI reads it) |
| `service.log` | service log (also shown in the Service tab) |
| `prints/` | generated HTML/PDF output (the newest 200 are kept) |

UI preferences (saved connection, Arabic mode, chosen News categories) are in `~/.newspaper-ui.json`.

## HTTP API
JSON over HTTP, and every request needs the header `X-Newspaper-Token: <token>`. Main endpoints:

| Method | Path | |
|---|---|---|
| GET | `/api/status` | version, uptime, counts, printing capabilities, limits |
| GET/PUT | `/api/settings` | `retention_days`, `history_days` (PUT prunes right away and returns `pruned`) |
| GET | `/api/articles` | `q`, `source_id`, `lang`, `categories` (JSON list), `hours`, `limit`, `offset` |
| GET | `/api/articles/{id}` | one article with its full text |
| GET/POST/PUT/DELETE | `/api/sources[/{id}]` | feeds. `POST /api/sources/test` previews a feed |
| POST | `/api/refresh` | fetch all (or given) sources in the background |
| GET/POST/PUT/DELETE | `/api/templates[/{id}]` | print layouts. `POST /api/templates/{id}/default` |
| GET/POST/PUT/DELETE | `/api/jobs[/{id}]` | print jobs. `POST /api/jobs/{id}/run` runs one now |
| POST | `/api/render`, `/api/print` | preview HTML / print right away |
| GET | `/api/runs`, `/api/log`, `/api/categories`, `/api/catalog`, `/api/cron/preview` | |

```sh
TOKEN=$(python -c "import json,os;print(json.load(open(os.path.expanduser('~/.newspaper/config.json')))['token'])")
curl -s -H "X-Newspaper-Token: $TOKEN" -X PUT -d '{"retention_days": 14}' http://127.0.0.1:8765/api/settings
```

## Security
- The service listens on 127.0.0.1 only, unless you pass `--host`.
- Every request needs the token from `config.json`. That file can only be read by its owner, or by the service group for the FreeBSD system service.
- The API is plain HTTP. Do not expose it to untrusted networks. Use an SSH tunnel instead.

## Troubleshooting
- **"Service not reachable".**
  - Run `python service/newspaper_service.py status`, or `service newspaper status` for the system service.
  - `start` prints the reason when it cannot start, for example a busy port or an unwritable data folder. Details are in `service.log`.
- **"Rejected the access token".** The token changed. On FreeBSD run `service newspaper token`, then use **Connect…** in the UI.
- **No PDFs / "PDF engine NOT AVAILABLE".** Install WeasyPrint and Pango. The Service tab shows the exact hint for your system.
- **Arabic shows as boxes.** Install an Arabic-capable font (Noto or DejaVu).
- **Articles vanish after a week.** That is the retention limit. Raise it in the Service tab.

## Project layout
```
service/
  newspaper_service.py  entry point: CLI (start/stop/status/token/run), daemonising, privilege drop
  api.py                HTTP API (routes, token check)
  store.py              SQLite storage, config.json, data paths, retention
  sources.py            RSS/Atom + GitHub fetching, full-text extraction
  scheduler.py          cron job runner, optional source polling, pruning
  cron.py               cron expression parser + next-run calculation
  printing.py           layouts → HTML → PDF → printer, page limits
  catalog.py            built-in catalog of popular feeds
  autostart.py          per-user autostart (Windows / macOS / systemd / crontab)
ui/
  newspaper_ui.py       Tk desktop app
  textshape.py          Arabic shaping + bidi reordering + wrapping for Tk on X11
freebsd/
  install.sh, uninstall.sh, newspaper   rc.d system service
requirements.txt
```
