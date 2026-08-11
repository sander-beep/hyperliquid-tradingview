#!/usr/bin/env bash
# Nightly SQLite snapshot (safe under WAL via sqlite3 .backup).
# Keeps 14 nightly copies. For the weekly off-box copy, rsync/scp the newest
# file from backups/ to another machine (see README).
set -euo pipefail
cd "$(dirname "$0")/.."

DB=data/rsps.db
OUT=backups
mkdir -p "$OUT"
[ -f "$DB" ] || { echo "no database yet at $DB"; exit 0; }

STAMP=$(date -u +%Y%m%d-%H%M%S)
sqlite3 "$DB" ".backup '$OUT/rsps-$STAMP.db'"
ls -1t "$OUT"/rsps-*.db | tail -n +15 | xargs -r rm --
echo "backup written: $OUT/rsps-$STAMP.db"
