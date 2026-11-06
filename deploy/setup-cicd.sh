#!/usr/bin/env bash
# Один раз на сервере, чтобы GitHub сам выкладывал обновления:  sudo bash /opt/football/deploy/setup-cicd.sh
# Создаёт пользователя deploy, которому по SSH-ключу можно ТОЛЬКО одно — обновить бота (deploy/update.sh).
# В конце печатает три значения для GitHub → Settings → Secrets and variables → Actions.
# Повторный запуск безопасен: создаст новый ключ (старый перестанет работать) и обновит команду.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }
APP_DIR=/opt/football
RUNNER=/usr/local/sbin/football-deploy

# Копия скрипта обновления, принадлежащая root: код в $APP_DIR принадлежит службе football,
# и запускать его от root по ключу нельзя.
install -o root -g root -m 755 "$APP_DIR/deploy/update.sh" "$RUNNER"

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
passwd -l deploy >/dev/null  # вход только по ключу
echo "deploy ALL=(root) NOPASSWD: $RUNNER" > /etc/sudoers.d/football-deploy
chmod 440 /etc/sudoers.d/football-deploy && visudo -cqf /etc/sudoers.d/football-deploy

KEY_DIR=$(mktemp -d) && trap 'rm -rf "$KEY_DIR"' EXIT
ssh-keygen -q -t ed25519 -N '' -C "github-actions-deploy" -f "$KEY_DIR/key"
install -o deploy -g deploy -m 700 -d /home/deploy/.ssh
echo "command=\"sudo $RUNNER\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding $(cat "$KEY_DIR/key.pub")" \
  > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys && chmod 600 /home/deploy/.ssh/authorized_keys

IP=$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')
echo
echo "=== Готово. Добавьте в GitHub: репозиторий → Settings → Secrets and variables → Actions → New repository secret ==="
echo
echo "--- DEPLOY_HOST ---"
echo "$IP"
echo
echo "--- DEPLOY_KNOWN_HOSTS ---"
for f in /etc/ssh/ssh_host_*_key.pub; do echo "$IP $(cut -d' ' -f1,2 "$f")"; done
echo
echo "--- DEPLOY_SSH_KEY (весь блок, вместе со строками BEGIN/END) ---"
cat "$KEY_DIR/key"
echo
echo "Ключ нигде на сервере не сохранён — скопируйте сейчас. Если потеряли — запустите скрипт ещё раз."
