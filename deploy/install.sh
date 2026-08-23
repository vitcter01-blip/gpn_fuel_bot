#!/usr/bin/env bash
# Установка бота как systemd-сервиса. Запускать от root на Debian/Ubuntu.
# Скрипт идемпотентен: повторный запуск обновляет код и перезапускает сервис.
set -euo pipefail

APP_DIR=/opt/gpn_fuel_bot
DATA_DIR=/var/lib/gpn
SERVICE=gpn-bot
USER_NAME=gpn

die() { echo "Ошибка: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "запустите от root (sudo bash deploy/install.sh)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SRC_DIR/bot.py" ]] || die "не найден bot.py рядом со скриптом"

echo "==> Проверяю Python"
command -v python3 >/dev/null || die "python3 не установлен"
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
[[ "$PY_OK" == "1" ]] || die "нужен Python 3.11+, найден $(python3 -V)"
python3 -c 'import venv' 2>/dev/null || die "нет модуля venv: apt install python3-venv"

echo "==> Создаю пользователя $USER_NAME"
id -u "$USER_NAME" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"

echo "==> Копирую файлы в $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
cp "$SRC_DIR"/*.py "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Ставлю зависимости в виртуальное окружение"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "==> Ставлю Chromium для автоматического обнаружения API"
"$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium

if [[ ! -f "$APP_DIR/.env" ]]; then
    if [[ -f "$SRC_DIR/.env" ]]; then
        cp "$SRC_DIR/.env" "$APP_DIR/.env"
    else
        cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
        echo "!!! Впишите BOT_TOKEN в $APP_DIR/.env и запустите скрипт повторно"
    fi
fi
chmod 600 "$APP_DIR/.env"

echo "==> Проверяю настройки"
# штатная проверка: разбирает .env так же, как это сделает сам бот
set -a; source "$APP_DIR/.env"; set +a
if ! (cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" config.py); then
    die "исправьте настройки в $APP_DIR/.env и запустите скрипт повторно"
fi

chown -R "$USER_NAME:$USER_NAME" "$APP_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Ставлю сервис"
cp "$SRC_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
    echo "==> Готово. Бот работает."
    echo "    Логи:      journalctl -u $SERVICE -f"
    echo "    Остановка: systemctl stop $SERVICE"
else
    echo "!!! Сервис не поднялся. Последние строки лога:"
    journalctl -u "$SERVICE" -n 30 --no-pager
    exit 1
fi
