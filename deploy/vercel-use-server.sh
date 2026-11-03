#!/usr/bin/env bash
# Переключить Vercel на свой сервер. Запускать в репозитории (не на сервере), потом закоммитить и запушить.
#
#   bash deploy/vercel-use-server.sh proxy    1-2-3-4.sslip.io   # адрес …vercel.app остаётся, Vercel пересылает всё на сервер
#   bash deploy/vercel-use-server.sh redirect 1-2-3-4.sslip.io   # старые ссылки перекидывают на адрес сервера
#
# В обоих случаях функции Vercel отключаются (.vercelignore), Neon и cron Vercel больше не используются.
set -euo pipefail
MODE="${1:?proxy или redirect}"
HOST="${2:?адрес сервера, например 1-2-3-4.sslip.io}"
HOST="${HOST#https://}"; HOST="${HOST%/}"
cd "$(dirname "$0")/.."

case "$MODE" in
  proxy) RULE='"rewrites": [{ "source": "/(.*)", "destination": "https://'"$HOST"'/$1" }]' ;;
  redirect) RULE='"redirects": [{ "source": "/(.*)", "destination": "https://'"$HOST"'/$1", "permanent": false }]' ;;
  *) echo "Режим: proxy или redirect"; exit 1 ;;
esac

[ -f deploy/vercel.json.vercel-functions ] || cp vercel.json deploy/vercel.json.vercel-functions
cat > vercel.json <<EOF
{
  "\$schema": "https://openapi.vercel.sh/vercel.json",
  $RULE
}
EOF
# Без этого Vercel продолжил бы запускать свои функции (api/*.py) со старой базой.
printf 'api/\nbot/\ntests/\n' > .vercelignore
echo "Готово: vercel.json → $MODE на https://$HOST. Вернуть как было: cp deploy/vercel.json.vercel-functions vercel.json && rm .vercelignore"
