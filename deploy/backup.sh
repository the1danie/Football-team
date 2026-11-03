#!/usr/bin/env bash
# Бэкап базы: /var/backups/football/football-ГГГГ-ММ-ДД.sql.gz, старше 14 дней удаляются.
# Восстановить:  gunzip -c файл.sql.gz | sudo -u postgres psql football
set -euo pipefail
DIR=/var/backups/football
mkdir -p "$DIR" && chmod 700 "$DIR"
FILE="$DIR/football-$(date +%F).sql.gz"
sudo -u postgres pg_dump --no-owner --no-acl football | gzip -9 > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"
find "$DIR" -name 'football-*.sql.gz' -mtime +14 -delete
echo "$(date '+%F %T') ok $(du -h "$FILE" | cut -f1) $FILE"
