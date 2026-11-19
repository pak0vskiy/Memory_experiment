#!/usr/bin/env bash
# Start the study from an empty database (e.g. after piloting). The old database is MOVED to
# data/backups/, not deleted, so nothing is lost by accident.
#   sudo bash deploy/reset-data.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "Current timings in .env:"; grep -E '^(READ_SEC|HIGHLIGHT_SEC|TEST_MAX_SEC|BLOCK_SEC|FEEDBACK_MIN_SEC|DAY2_GAP_SEC|DAY9_GAP_SEC)=' .env | sed 's/^/  /'
if ! grep -q '^DAY2_GAP_SEC=172800$' .env || ! grep -q '^DAY9_GAP_SEC=604800$' .env || ! grep -qE '^READ_SEC=(210|240)$' .env; then
  echo "WARNING: these are NOT the real study timings (READ_SEC=210, DAY2_GAP_SEC=172800, DAY9_GAP_SEC=604800)."
fi
read -r -p "Move the current database to data/backups and start empty? [y/N] " a
[[ "$a" =~ ^[Yy] ]] || exit 1
docker compose down
mkdir -p data/backups
STAMP=$(date +%F_%H%M)
for f in data/study.db data/study.db-wal data/study.db-shm; do
  if [ -f "$f" ]; then mv "$f" "data/backups/$(basename "$f" | sed "s/study/study-before-reset-$STAMP/")"; fi
done
docker compose up -d
echo "Done. Old database saved as data/backups/study-before-reset-$STAMP.db"
