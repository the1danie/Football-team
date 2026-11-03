#!/usr/bin/env bash
# Перенос всех данных из Neon в базу на сервере (игры, игроки, минусы, фото, настройки, push-подписки).
# Запуск от root:  bash /opt/football/deploy/migrate_from_neon.sh 'postgresql://…neon.tech/neondb?sslmode=require'
# Строка — DATABASE_URL из Vercel (Settings → Environment Variables).
# ВНИМАНИЕ: база на сервере заменяется копией из Neon.
set -euo pipefail
SRC="${1:?Укажите строку подключения Neon в кавычках}"
SRC="${SRC/postgresql+asyncpg:\/\//postgresql://}"
SRC="${SRC/postgres:\/\//postgresql://}"
DUMP=/root/neon-$(date +%F-%H%M).dump

echo "==> Выгружаю из Neon…"
pg_dump --no-owner --no-acl -Fc -f "$DUMP" "$SRC"
echo "    $(du -h "$DUMP" | cut -f1) → $DUMP"

echo "==> Останавливаю бота и заменяю базу на сервере…"
systemctl stop football
sudo -u postgres dropdb --if-exists football
sudo -u postgres createdb -O football football
# Через stdin: у пользователя postgres нет доступа к /root. Предупреждения (например, про схему public) не страшны.
sudo -u postgres pg_restore --no-owner --no-acl --role=football -d football < "$DUMP" \
  || echo "    (pg_restore сообщил о предупреждениях — проверяю данные)"
USERS=$(sudo -u postgres psql -tA -d football -c "SELECT count(*) FROM users" 2>/dev/null || echo 0)
GAMES=$(sudo -u postgres psql -tA -d football -c "SELECT count(*) FROM games" 2>/dev/null || echo 0)
echo "    Перенесено: игроков $USERS, игр $GAMES"
if [ "${USERS:-0}" -lt 1 ]; then
  echo "!!! Игроков 0 — что-то пошло не так. Бот остаётся на Vercel, ничего не переключаю."
  systemctl start football
  exit 1
fi
sudo -u postgres psql -q -d football -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO football; GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO football;"

echo "==> Переключаю Telegram и напоминания на этот сервер…"
sed -i 's/^RUN_SCHEDULER=.*/RUN_SCHEDULER=1/; s/^MANAGE_WEBHOOK=.*/MANAGE_WEBHOOK=1/' /etc/football.env
systemctl start football
sleep 4
systemctl --no-pager --lines=5 status football | tail -6
echo
echo "Готово: бот работает с этого сервера. Проверьте приложение в Telegram."
echo "Копия из Neon сохранена: $DUMP"
echo "Не забудьте: выключить задание в cron-job.org и переключить Vercel (см. deploy/README.md, шаг 5)."
