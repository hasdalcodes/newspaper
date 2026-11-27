#!/bin/sh
# Remove the Newspaper rc.d service. Your data in /var/db/newspaper is kept.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Please run as root." >&2; exit 1; }
RCDIR=${NEWSPAPER_RCDIR:-/usr/local/etc/rc.d}
service newspaper stop 2>/dev/null || true
sysrc -x newspaper_enable newspaper_user newspaper_group newspaper_dir newspaper_data newspaper_python 2>/dev/null || true
rm -f "$RCDIR/newspaper"
rm -rf "${NEWSPAPER_DIR:-/usr/local/share/newspaper}"
echo "Removed. Data kept in ${NEWSPAPER_DATA:-/var/db/newspaper} (delete it yourself if you want it gone)."
