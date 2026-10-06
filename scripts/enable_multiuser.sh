#!/usr/bin/env bash
set -euo pipefail
umask 077
DOMAIN=${1:-invest.argokov.ru}
PROJECT=/opt/t-invest-bot
DATA=/var/lib/t-invest-bot
ENV_FILE=/etc/t-invest-bot/server.env
CONFIG=/etc/nginx/sites-available/t-invest-bot.conf
if [[ $EUID != 0 ]]; then echo "Запусти через sudo" >&2; exit 1; fi
cd "$PROJECT"
python3 scripts/render_nginx.py "$DOMAIN" >/dev/null
grep -Fxq "TINVEST_PUBLIC_ORIGIN=https://$DOMAIN" "$ENV_FILE"
if ! grep -Fxq TINVEST_PULSE_SOURCE=shared "$ENV_FILE"; then
    echo "Сначала настрой общий источник через configure_shared_source.sh" >&2; exit 1
fi
if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then echo "Сначала настрой HTTPS" >&2; exit 1; fi
"$PROJECT/.venv/bin/python" -m unittest discover -s tests -q
# Prevent financial side effects while migration/rollback is in progress.
python3 - "$DATA" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
paths = [root / 'demo-settings.json', *root.glob('clients/*/demo-settings.json')]
for path in paths:
    if path.exists() and json.loads(path.read_text()).get('real_mode', 'off') != 'off':
        raise SystemExit('Сначала выключи реальную торговлю во всех кабинетах')
PY
BACKUP=$(mktemp -d /var/lib/t-invest-migration-XXXXXXXX)
trap 'systemctl start t-invest-bot || true; echo "Подготовка копии не завершена; сервис запущен обратно." >&2' ERR
systemctl stop t-invest-bot
cp -a "$DATA" "$BACKUP/data"
cp -p "$ENV_FILE" "$BACKUP/server.env"
cp -p "$CONFIG" "$BACKUP/nginx.conf"
cp -p /etc/systemd/system/t-invest-bot.service "$BACKUP/bot.service"
if [[ -f /etc/t-invest-bot/client-proxy.conf ]]; then cp -p /etc/t-invest-bot/client-proxy.conf "$BACKUP/client-proxy.conf"; fi
if [[ -d /var/lib/t-invest-auth ]]; then cp -a /var/lib/t-invest-auth "$BACKUP/auth"; fi
rollback() {
    local code=$?
    trap - ERR
    systemctl stop t-invest-bot || true
    # Keep the failed attempt intact: no client data or order journals are deleted.
    mv "$DATA" "$BACKUP/failed-data"
    cp -a "$BACKUP/data" "$DATA"
    cp -p "$BACKUP/server.env" "$ENV_FILE"
    cp -p "$BACKUP/nginx.conf" "$CONFIG"
    cp -p "$BACKUP/bot.service" /etc/systemd/system/t-invest-bot.service
    if [[ -f $BACKUP/client-proxy.conf ]]; then cp -p "$BACKUP/client-proxy.conf" /etc/t-invest-bot/client-proxy.conf; else rm -f /etc/t-invest-bot/client-proxy.conf; fi
    if [[ -d $BACKUP/auth ]]; then cp -a "$BACKUP/auth/." /var/lib/t-invest-auth/; fi
    systemctl daemon-reload
    nginx -t && systemctl reload nginx
    systemctl start t-invest-bot || true
    echo "Перенос остановлен, прежняя конфигурация восстановлена. Защищённая копия: $BACKUP" >&2
    exit "$code"
}
trap rollback ERR
install -d -m 2750 -o t-invest-bot -g www-data /var/lib/t-invest-auth
python3 scripts/migrate_clients.py
chown -R t-invest-bot:t-invest-bot "$DATA"
chown t-invest-bot:www-data /var/lib/t-invest-auth/clients.htpasswd
chmod 640 /var/lib/t-invest-auth/clients.htpasswd
python3 scripts/render_nginx.py "$DOMAIN" --https --shared-source --multi-user > "$CONFIG"
chmod 644 "$CONFIG"
nginx -t
cp -p "$CONFIG" "$BACKUP/new-nginx.conf"
# Do not allow client mutations until the complete new process set is healthy.
python3 - "$CONFIG" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
path.write_text(path.read_text().replace('listen 443 ssl;', 'listen 443 ssl;\n    return 503;', 1))
PY
nginx -t
systemctl reload nginx
install -m 644 deploy/t-invest-bot.service /etc/systemd/system/t-invest-bot.service
systemctl daemon-reload
systemctl restart t-invest-bot
ready=0
for attempt in {1..40}; do
    if curl --fail --silent --max-time 2 http://127.0.0.1:8765/api/health >/dev/null; then ready=1; break; fi
    sleep 1
done
[[ $ready == 1 ]]
cp -p "$BACKUP/new-nginx.conf" "$CONFIG"
nginx -t
systemctl reload nginx
trap - ERR
echo "Готово. admin сохранён. Создавай клиентов на https://$DOMAIN/source-admin/"
echo "Резервная копия с секретами (только root): $BACKUP — не публикуй и не раздавай."
