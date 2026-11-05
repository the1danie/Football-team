#!/usr/bin/env bash
# Установка бота на чистый сервер Ubuntu 22.04/24.04 (например, ps.kz Basic-1/Basic-2).
# Запуск от root:  bash /opt/football/deploy/install.sh
# Повторный запуск безопасен: обновит код и настройки, данные не трогает.
set -euo pipefail

APP_DIR=/opt/football
ENV_FILE=/etc/football.env
APP_USER=football
DB_NAME=football
DB_USER=football
PG_VERSION=18  # как в Neon (проект Qalamger): pg_dump не выгружает базу новее себя

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Запустите от root (sudo -i)"; exit 1; }
cd "$APP_DIR"

say "1/9 Swap (запас памяти для маленького сервера)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -q vm.swappiness=10 && echo 'vm.swappiness=10' > /etc/sysctl.d/90-football.conf
fi

say "2/9 Пакеты: Python, PostgreSQL $PG_VERSION, Caddy (HTTPS), файрвол"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git python3 python3-venv python3-pip ufw debian-keyring debian-archive-keyring apt-transport-https
install -d /usr/share/postgresql-common/pgdg
if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
fi
if [ ! -f /etc/apt/sources.list.d/caddy-stable.list ]; then
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
fi
apt-get update -q
apt-get install -y -q "postgresql-$PG_VERSION" caddy

say "3/9 PostgreSQL под маленький сервер"
cat > "/etc/postgresql/$PG_VERSION/main/conf.d/football.conf" <<'EOF'
# Бот на ~30 человек: немного памяти и соединений.
shared_buffers = 64MB
effective_cache_size = 256MB
work_mem = 4MB
maintenance_work_mem = 32MB
max_connections = 30
EOF
systemctl restart postgresql

say "4/9 Пользователь и база"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  DB_PASS=$(openssl rand -hex 16)
  sudo -u postgres psql -q -c "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS'"
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
  echo "$DB_PASS" > /root/.football-db-password && chmod 600 /root/.football-db-password
fi
DB_PASS=$(cat /root/.football-db-password)

say "5/9 Python-окружение"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/deploy/requirements-server.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "6/9 Настройки ($ENV_FILE)"
IP=$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="${HOST:-${IP//./-}.sslip.io}"
if [ ! -f "$ENV_FILE" ]; then
  read -rp "BOT_TOKEN (от @BotFather, тот же, что в Vercel): " BOT_TOKEN
  read -rp "ADMIN_IDS (ваш Telegram ID, как в Vercel): " ADMIN_IDS
  read -rp "Адрес сайта для игроков [https://$HOST] (если оставляете адрес Vercel — впишите его): " PUBLIC_URL
  PUBLIC_URL="${PUBLIC_URL:-https://$HOST}"
  cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME
PUBLIC_URL=${PUBLIC_URL%/}
TZ_NAME=Asia/Almaty
CRON_SECRET=$(openssl rand -hex 24)
FOOTBALL_SERVER=1
# До переноса базы сервер не трогает Telegram и не шлёт напоминаний (бот пока работает на Vercel).
# migrate_from_neon.sh включит оба в конце.
RUN_SCHEDULER=0
MANAGE_WEBHOOK=0
EOF
  echo "Остальные переменные из Vercel (если меняли) допишите в $ENV_FILE — см. .env.example"
fi
chmod 640 "$ENV_FILE" && chown root:"$APP_USER" "$ENV_FILE"

say "7/9 Служба football (systemd)"
cat > /etc/systemd/system/football.service <<EOF
[Unit]
Description=Football team bot (Telegram + сайт + планировщик)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn bot.server:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --no-access-log
Restart=always
RestartSec=3
MemoryMax=600M

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q football
systemctl restart football

say "8/9 HTTPS (Caddy) для $HOST"
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}
EOF
systemctl reload caddy || systemctl restart caddy
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

say "9/9 Бэкап базы каждую ночь (03:30, хранится 14 дней)"
install -m 755 "$APP_DIR/deploy/backup.sh" /usr/local/bin/football-backup
echo "30 3 * * * root /usr/local/bin/football-backup >/var/log/football-backup.log 2>&1" > /etc/cron.d/football-backup

sleep 3
CRON_SECRET=$(grep '^CRON_SECRET=' "$ENV_FILE" | cut -d= -f2)
say "Готово"
echo "Сервер:      https://$HOST"
echo "Проверка:    https://$HOST/api/health?secret=$CRON_SECRET"
echo "Логи бота:   journalctl -u football -f"
echo "Сейчас сервер работает «в тишине»: бот по-прежнему на Vercel."
echo "Дальше — перенос базы и переключение: bash $APP_DIR/deploy/migrate_from_neon.sh 'строка подключения Neon'"
