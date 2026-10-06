#!/usr/bin/env bash
set -euo pipefail
PROJECT=/opt/t-invest-bot
if [[ $EUID -ne 0 ]]; then echo "Запусти через sudo" >&2; exit 1; fi
cd "$PROJECT"
if [[ -n $(git status --porcelain) ]]; then
    echo "Есть локальные изменения. Разбери git status перед обновлением" >&2; exit 1
fi
trap 'echo "Обновление остановилось: бот выключен. Проверь ошибку и systemctl status t-invest-bot." >&2' ERR
systemctl stop t-invest-bot.service
# Fetch/pull cannot overwrite local changes or rewrite branch history.
git pull --ff-only
"$PROJECT/.venv/bin/python" -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PROJECT/.playwright" "$PROJECT/.venv/bin/python" -m playwright install chromium
"$PROJECT/.venv/bin/python" -m unittest discover -s tests -q
systemctl restart t-invest-bot.service
trap - ERR
echo "Код обновлён; данные /var/lib/t-invest-bot и HTTPS-настройки сохранены."
