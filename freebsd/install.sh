#!/bin/sh
# Install Newspaper as a FreeBSD rc.d service.
# Run as root from the unpacked project folder:   sh freebsd/install.sh
#
# Overridable: NEWSPAPER_DIR NEWSPAPER_DATA NEWSPAPER_USER NEWSPAPER_GROUP NEWSPAPER_PYTHON NEWSPAPER_RCDIR
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (su, or doas sh $0)." >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(dirname "$HERE")
PREFIX=${NEWSPAPER_DIR:-/usr/local/share/newspaper}
DATA=${NEWSPAPER_DATA:-/var/db/newspaper}
SVC_USER=${NEWSPAPER_USER:-newspaper}
SVC_GROUP=${NEWSPAPER_GROUP:-newspaper}
RCDIR=${NEWSPAPER_RCDIR:-/usr/local/etc/rc.d}
PYTHON=${NEWSPAPER_PYTHON:-$(command -v python3 || true)}

[ -n "$PYTHON" ] || { echo "python3 not found. Install it: pkg install python3" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' ||
	{ echo "Python 3.10 or newer is required ($PYTHON)." >&2; exit 1; }
"$PYTHON" -c 'import requests' 2>/dev/null || {
	echo "Python module 'requests' is missing. Install it, e.g.: pkg install py311-requests" >&2
	echo "(use the package matching your Python version: $("$PYTHON" -V))" >&2
	exit 1
}

"$PYTHON" -c 'import weasyprint' 2>/dev/null || [ -n "${NEWSPAPER_SKIP_PDF_CHECK:-}" ] || {
	echo "Python module 'weasyprint' is missing (needed to make the PDFs that get printed)." >&2
	echo "Install it, e.g.: pkg install py311-weasyprint   (matching your Python: $("$PYTHON" -V))" >&2
	echo "To install Newspaper anyway (printing then saves HTML only): NEWSPAPER_SKIP_PDF_CHECK=1 sh $0" >&2
	exit 1
}

echo "==> account $SVC_USER:$SVC_GROUP"
pw groupshow "$SVC_GROUP" >/dev/null 2>&1 || pw groupadd "$SVC_GROUP"
pw usershow "$SVC_USER" >/dev/null 2>&1 ||
	pw useradd "$SVC_USER" -g "$SVC_GROUP" -d "$DATA" -s /usr/sbin/nologin -c "Newspaper service"

echo "==> data folder $DATA"
install -d -m 750 -o "$SVC_USER" -g "$SVC_GROUP" "$DATA"

echo "==> program files in $PREFIX"
install -d -m 755 "$PREFIX"
rm -rf "$PREFIX/service" "$PREFIX/ui"
cp -R "$SRC/service" "$SRC/ui" "$PREFIX/"
cp "$SRC/README.md" "$SRC/requirements.txt" "$PREFIX/"
find "$PREFIX" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
chown -R "root:$(id -gn 0)" "$PREFIX"
chmod -R go-w "$PREFIX"

echo "==> rc.d script"
install -d -m 755 "$RCDIR"
install -m 555 "$HERE/newspaper" "$RCDIR/newspaper"

echo "==> /etc/rc.conf"
sysrc newspaper_enable=YES newspaper_user="$SVC_USER" newspaper_group="$SVC_GROUP" \
	newspaper_dir="$PREFIX" newspaper_data="$DATA" newspaper_python="$PYTHON"

cat << MSG

Installed. Next steps:

  service newspaper start         # prints the reason if it cannot start
  service newspaper status

To use the UI on this machine, either add your login to the service group (then log out and in again):

  pw groupmod $SVC_GROUP -m YOURLOGIN

or print the connection details and paste them into the UI (Service tab -> Connect...):

  service newspaper token

Run the UI:   $PYTHON $PREFIX/ui/newspaper_ui.py      (needs: pkg install py-tkinter matching your Python)
For printing install CUPS and add your printer:   pkg install cups
Arabic text needs an Arabic-capable font installed (any Noto/DejaVu font package).
MSG
