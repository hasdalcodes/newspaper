#!/usr/bin/env python3
"""Make the Newspaper service start automatically.

Usage: autostart.py install|remove|status
       autostart.py rcd        (FreeBSD only: print a system-wide rc.d script, needs root to install)

Windows: scheduled task at logon.  macOS: LaunchAgent.  Linux: systemd user service.
FreeBSD/OpenBSD/NetBSD: a user crontab '@reboot' entry (no root needed).
"""
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).with_name("newspaper_service.py").resolve()
NAME = "NewspaperService"


def run(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{cmd[0]} failed")
    return r


def windows(action):
    if action == "install":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = pyw if pyw.exists() else Path(sys.executable)
        run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", NAME, "/TR", f'"{exe}" "{SERVICE}"'])
        return "Scheduled task created: the service will start at your next login."
    if action == "remove":
        run(["schtasks", "/Delete", "/F", "/TN", NAME])
        return "Scheduled task removed."
    r = run(["schtasks", "/Query", "/TN", NAME], check=False)
    return "Autostart is ON." if r.returncode == 0 else "Autostart is OFF."


def macos(action):
    plist = Path.home() / "Library/LaunchAgents/com.newspaper.service.plist"
    if action == "install":
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.newspaper.service</string>
<key>ProgramArguments</key><array><string>{sys.executable}</string><string>{SERVICE}</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>""")
        run(["launchctl", "load", str(plist)], check=False)
        return f"LaunchAgent installed: {plist}"
    if action == "remove":
        run(["launchctl", "unload", str(plist)], check=False)
        plist.unlink(missing_ok=True)
        return "LaunchAgent removed."
    return "Autostart is ON." if plist.exists() else "Autostart is OFF."


def linux(action):
    unit = Path.home() / ".config/systemd/user/newspaper.service"
    if action == "install":
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=Newspaper background service

[Service]
ExecStart={sys.executable} {SERVICE}
Restart=on-failure

[Install]
WantedBy=default.target
""")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", "newspaper.service"])
        return ("systemd user service enabled and started.\n"
                "To keep it running while you are logged out: sudo loginctl enable-linger $USER")
    if action == "remove":
        run(["systemctl", "--user", "disable", "--now", "newspaper.service"], check=False)
        unit.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], check=False)
        return "systemd user service removed."
    return "Autostart is ON." if unit.exists() else "Autostart is OFF."


CRON_MARK = "# newspaper-service"


def bsd(action):
    cur = run(["crontab", "-l"], check=False)
    lines = [ln for ln in (cur.stdout.splitlines() if cur.returncode == 0 else []) if CRON_MARK not in ln]
    has = cur.returncode == 0 and CRON_MARK in cur.stdout
    if action == "status":
        return "Autostart is ON (crontab @reboot)." if has else "Autostart is OFF."
    if action == "install":
        lines.append(f"@reboot {sys.executable} {SERVICE} start {CRON_MARK}")
    r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "crontab failed")
    if action == "install":
        return ("Added a crontab '@reboot' entry: the service starts at every boot.\n"
                "It is not started right now - use the UI's Start button, or run:  python3 service/newspaper_service.py start\n"
                "For a system-wide rc.d service instead (needs root): sh freebsd/install.sh")
    return "Crontab entry removed."


def rcd():
    f = SERVICE.parent.parent / "freebsd" / "newspaper"
    if not f.exists():
        return f"rc.d script not found at {f}"
    return f.read_text() + "\n# (Prefer: sh freebsd/install.sh as root - it also creates the service account and data folder.)\n"


def pick_backend():
    if sys.platform.startswith("win"):
        return windows
    if sys.platform == "darwin":
        return macos
    if sys.platform.startswith(("freebsd", "openbsd", "netbsd", "dragonfly")):
        return bsd
    return linux


if __name__ == "__main__":
    act = sys.argv[1] if len(sys.argv) > 1 else "status"
    if act == "rcd":
        print(rcd())
        sys.exit(0)
    if act not in ("install", "remove", "status"):
        sys.exit(__doc__)
    try:
        print(pick_backend()(act))
    except Exception as exc:
        print(f"Failed: {exc}")
        sys.exit(1)
