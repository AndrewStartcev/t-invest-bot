#!/usr/bin/env bash
# Update the exact extension ingress without changing the rest of an existing vhost.
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
# Existing ingress is patched in place; never regenerate a customized vhost.
# The fresh-location fallback below keeps legacy installation behavior.

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

if grep -Fq 'location = /api/extension/report {' "$CONFIG"; then
  python3 "$PROJECT/scripts/patch_extension_ingress.py" "$CONFIG" > "$temp"
else
  python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" --https > "$temp"
fi
if ! grep -Fq 'location = /api/extension/report {' "$temp" || \
   ! grep -Fq 'proxy_request_buffering off;' "$temp" || \
   ! grep -Fq 'proxy_http_version 1.1;' "$temp"; then
  echo "Нужные настройки потоковой передачи расширения отсутствуют." >&2
  false
fi
if cmp -s "$temp" "$CONFIG"; then
  rm -f "$temp" "$backup"
  trap - ERR
  echo "Маршрут расширения уже настроен для потоковой передачи. Перезагрузка не нужна."
  exit 0
fi

# Replace only the project's known vhost. Do not touch other domains.
chmod 644 "$temp"
mv -f "$temp" "$CONFIG"
nginx -t
systemctl reload nginx
trap - ERR
echo "Готово: потоковая передача отчётов расширения включена для https://$DOMAIN/"
echo "Копия предыдущего конфига: $backup"
