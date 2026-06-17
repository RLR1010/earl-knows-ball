#!/bin/bash
# Daily PostgreSQL backup for Earl Knows Ball
# Runs from cron — dumps DB, compresses, prunes older than 30 days

BACKUP_DIR="$HOME/earl-backups"
DB_NAME="earl_knows_football"
DB_USER="earl"
CONTAINER="earl-knows-football-db-1"
RETENTION_DAYS=30

mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql.gz"

docker exec "$CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_FILE"

# Check backup was created successfully and has content
if [ -s "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "$(date) — Backup saved: $BACKUP_FILE ($SIZE)"
else
    echo "$(date) — ERROR: Backup failed or empty" >&2
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Prune old backups
find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime +$RETENTION_DAYS -delete
