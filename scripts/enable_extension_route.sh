#!/usr/bin/env bash
# Install the extension ingress route while preserving the current credentials.
# The rest of the app and brokerage process are not restarted by this script.
set -euo pipefail

PROJECT=/opt/t-invest-bot
DOMAIN=${1:-invest.argokov.ru}
CONFIG=/etc/nginx/sites-available/t-invest-bot.conf
ENV_FILE=/etc/t-invest-bot/server.env

if [[ ${EUID} -ne 0 ]]; then
  echo "Запусти через sudo." >&2
  exit 1
fi
if [[ ! -f "$CONFIG" || ! -f "$ENV_FILE" ]]; then
  echo "Нет существующей серверной настройки T-Invest Bot. Сначала проверь сервер." >&2
  exit 1
fi
if ! grep -Fxq "TINVEST_PUBLIC_ORIGIN=https://$DOMAIN" "$ENV_FILE"; then
  echo "Домен не совпадает с серверной настройкой." >&2
  exit 1
fi
if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
  echo "HTTPS-сертификат не найден. Внешний маршрут расширения не будет открыт." >&2
  exit 1
fi
if ! nginx -t; then
  echo "Существующий nginx не проходит проверку; конфигурация не менялась." >&2
  exit 1
fi
if grep -Fq 'location = /api/extension/report {' "$CONFIG"; then
  echo "Маршрут расширения уже присутствует в nginx. Перезагрузка не нужна."
  exit 0
fi

backup=$(mktemp /etc/nginx/sites-available/t-invest-bot.backup.XXXXXXXX)
temp=$(mktemp /etc/nginx/sites-available/t-invest-bot.new.XXXXXXXX)
chmod 600 "$backup" "$temp"
cp "$CONFIG" "$backup"
chmod 600 "$backup"

rollback() {
  cp -p "$backup" "$CONFIG"
  if nginx -t; then systemctl reload nginx || true; fi
  rm -f "$temp"
  echo "Изменения откатили. Резервная копия: $backup" >&2
}
trap rollback ERR

python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" --https > "$temp"
if ! grep -Fq 'location = /api/extension/report {' "$temp"; then
  echo "Маршрут расширения отсутствует в новой конфигурации." >&2
  false
fi

# Replace only the project's known vhost. Do not touch other domains.
chmod 644 "$temp"
mv -f "$temp" "$CONFIG"
nginx -t
systemctl reload nginx
trap - ERR
echo "Готово: маршрут расширения включён для https://$DOMAIN/"
echo "Копия предыдущего конфига: $backup"
