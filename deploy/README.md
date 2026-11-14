# Перенос на свой сервер (ps.kz)

Подходит тариф **Basic-1** (1 vCPU, 1 ГБ, 20 ГБ) — скрипт сам добавит swap 2 ГБ — или **Basic-2** (2 ГБ).
Система: **Ubuntu 24.04**. Покупать домен не нужно: адрес вида `1-2-3-4.sslip.io` с бесплатным HTTPS
появится сам, а снаружи можно оставить старый адрес `…vercel.app`.

На сервере работает одно приложение (`bot/server.py`): бот в Telegram, сайт и мини-приложение, фото, PDF,
push-уведомления. Планировщик (напоминания, распределение, отчёты) — внутри, cron-job.org не нужен.
База — PostgreSQL на этом же сервере, бэкап каждую ночь.

## 1. Сервер

Купите Basic-1 с Ubuntu 24.04. В письме/консоли ps.kz будут IP и пароль root.
Подключитесь: на Mac — «Терминал»: `ssh root@IP` (пароль из письма).

## 2. Код

Репозиторий закрытый — нужен токен GitHub только для чтения:
GitHub → Settings → Developer settings → Fine-grained tokens → *Generate new token* →
Repository access: только `Football-team`, Permissions → Contents: **Read-only**.

```bash
apt-get update && apt-get install -y git
git clone -b claude/telegram-duty-bot https://<ТОКЕН>@github.com/the1danie/Football-team.git /opt/football
```

## 3. Установка (≈ 5 минут)

```bash
bash /opt/football/deploy/install.sh
```

Скрипт спросит `BOT_TOKEN` и `ADMIN_IDS` — возьмите из Vercel (Settings → Environment Variables).
На вопрос про адрес сайта:
- **оставить адрес Vercel** (советую: у игроков не сломаются закладки, иконки на экране «Домой» и push) —
  впишите `https://<ваш-проект>.vercel.app`;
- **новый адрес** — просто Enter (будет `https://IP.sslip.io`).

После установки сервер работает **«в тишине»**: бот по-прежнему на Vercel, ничего не меняется.
Проверка: откройте `https://IP-через-дефисы.sslip.io/api/health?secret=…` (ссылку скрипт напечатает).

## 4. Перенос базы и переключение (≈ 1 минута простоя)

Лучше днём, когда нет сбора. Строка Neon — `DATABASE_URL` из Vercel.

```bash
bash /opt/football/deploy/migrate_from_neon.sh 'postgresql://…neon.tech/neondb?sslmode=require'
```

Скрипт выгрузит всё из Neon (игры, игроки, минусы, фото, push), загрузит на сервер, проверит, что игроки
на месте, и только тогда переключит Telegram и планировщик на сервер. Если что-то не так — ничего не переключит.

## 5. Сразу после переноса

1. **cron-job.org** — выключите задание (иначе Vercel будет пытаться забрать бота обратно).
2. **Vercel** — напишите Claude «переключи Vercel на сервер IP» (или сами в репозитории):
   ```bash
   bash deploy/vercel-use-server.sh proxy IP-через-дефисы.sslip.io     # если оставили адрес Vercel
   bash deploy/vercel-use-server.sh redirect IP-через-дефисы.sslip.io  # если перешли на новый адрес
   ```
   и закоммитьте/запушьте. Vercel перестанет работать со старой базой.
3. Проверьте в Telegram: меню, игры, отметка «Буду», фото, PDF.
4. Через неделю, если всё хорошо, — удалите проект в **Neon** (Settings → Delete project), чтобы не было счетов.

## Обслуживание

| Что | Команда |
|---|---|
| Логи бота | `journalctl -u football -f` |
| Перезапуск | `systemctl restart football` |
| Обновить до свежей версии | само после пуша (см. «Автообновление»), вручную — `bash /opt/football/deploy/update.sh` |
| Бэкап вручную | `football-backup` (файлы в `/var/backups/football`, 14 дней) |
| Восстановить из бэкапа | `systemctl stop football && gunzip -c ФАЙЛ.sql.gz \| sudo -u postgres psql football && systemctl start football` |
| Настройки | `/etc/football.env`, после правки — `systemctl restart football` |
| Память | `free -h` |

Бэкапы лежат на том же сервере. Раз в месяц полезно скачать свежий к себе:
`scp root@IP:/var/backups/football/football-$(date +%F).sql.gz .`

## Автообновление (CI/CD)

Каждый пуш в ветку `claude/telegram-duty-bot` → GitHub Actions прогоняет тесты → если всё зелёное,
заходит на сервер и запускает `update.sh` (`.github/workflows/deploy.yml`). Тесты упали — на сервер
ничего не уходит. Бот после обновления не запустился — выкладка в GitHub помечается красной.

Claude (в GitHub или на claude.ai) пушит в свою ветку вида `claude/…` — её workflow сам вливает в
`claude/telegram-duty-bot` (после тестов; если был merge — тесты ещё раз) и выкладывает. Руками
ничего переносить не нужно. Если ветки конфликтуют — шаг «promote» красный, решить конфликт вручную.

Настройка один раз (≈ 3 минуты):

1. На сервере:
   ```bash
   sudo bash /opt/football/deploy/setup-cicd.sh
   ```
   Скрипт создаст пользователя `deploy`, которому по ключу можно только одно — обновить бота,
   и напечатает три значения.
2. GitHub → репозиторий → **Settings → Secrets and variables → Actions → New repository secret** —
   добавьте `DEPLOY_HOST`, `DEPLOY_KNOWN_HOSTS`, `DEPLOY_SSH_KEY` (ключ целиком, со строками BEGIN/END).
3. Проверка: **Actions → «Тесты и выкладка на сервер» → Run workflow**.

Если поменялся сам `deploy/update.sh` — запустите `setup-cicd.sh` ещё раз (он берёт копию скрипта
и выдаёт новый ключ — обновите `DEPLOY_SSH_KEY`).

## Вернуться на Vercel

`cp deploy/vercel.json.vercel-functions vercel.json && rm .vercelignore`, запушить, включить cron-job.org.
База на Vercel — та, что была в Neon до переноса (новые данные с сервера — через `pg_dump` в Neon).
