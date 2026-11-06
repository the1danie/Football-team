#!/usr/bin/env bash
# Обновить бота до свежей версии из GitHub:  bash /opt/football/deploy/update.sh
set -euo pipefail
cd /opt/football
git config --global --add safe.directory /opt/football
BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$BRANCH" && git reset -q --hard "origin/$BRANCH"
.venv/bin/pip install -q -r deploy/requirements-server.txt
chown -R football:football /opt/football
systemctl restart football
sleep 3
systemctl --no-pager --lines=3 status football | tail -4
systemctl is-active --quiet football || { echo "Бот не запустился: journalctl -u football -n 50"; exit 1; }
echo "Обновлено до $(git log -1 --format='%h %s')"
