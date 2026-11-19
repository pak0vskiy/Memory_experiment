#!/usr/bin/env bash
# Consistent copy of the live database (safe while participants are active), kept for 30 days.
#   bash deploy/backup.sh            -> data/backups/study-YYYY-MM-DD_HHMM.db
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
STAMP=$(date +%F_%H%M)
docker exec memory-study python -c "
import sqlite3
src = sqlite3.connect('/data/study.db'); dst = sqlite3.connect('/data/backups/study-$STAMP.db')
src.backup(dst); dst.close(); src.close()
"
find data/backups -name 'study-*.db' -mtime +30 -delete
echo "$(date '+%F %T') backup ok: data/backups/study-$STAMP.db ($(du -h "data/backups/study-$STAMP.db" | cut -f1))"
