#!/usr/bin/env bash
# Separate the source owner's bank session from the single client's panel.
set -euo pipefail
umask 077
DOMAIN=${1:-invest.argokov.ru}
PROJECT=/opt/t-invest-bot
ENV_FILE=/etc/t-invest-bot/server.env
CONFIG=/etc/nginx/sites-available/t-invest-bot.conf
OWNER_FILE=/etc/nginx/t-invest-source.htpasswd
PROXY_FILE=/etc/t-invest-bot/source-proxy.conf
if [[ $EUID -ne 0 ]]; then echo "Запусти через sudo" >&2; exit 1; fi
python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" >/dev/null
if [[ ! -f $ENV_FILE ]] || ! grep -Fxq "TINVEST_PUBLIC_ORIGIN=https://$DOMAIN" "$ENV_FILE"; then
    echo "Сначала установи сервер для этого домена" >&2; exit 1
fi
if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
    echo "Сначала выпусти HTTPS: bash $PROJECT/scripts/enable_https.sh $DOMAIN ТВОЙ_EMAIL" >&2; exit 1
fi
cd "$PROJECT"
"$PROJECT/.venv/bin/python" -m unittest discover -s "$PROJECT/tests" -q
if [[ ! -f $OWNER_FILE ]]; then
    echo "Задай отдельный пароль владельца источника (source-owner). Не используй пароль клиентской панели:"
    htpasswd -cB "$OWNER_FILE" source-owner
fi
chown root:www-data "$OWNER_FILE"
chmod 640 "$OWNER_FILE"
BACKUP=$(mktemp -d)
cp -p "$ENV_FILE" "$BACKUP/server.env"
cp -p "$CONFIG" "$BACKUP/nginx.conf"
if [[ -f $PROXY_FILE ]]; then cp -p "$PROXY_FILE" "$BACKUP/source-proxy.conf"; fi
rollback() {
    local code=$?
    trap - ERR
    cp -p "$BACKUP/server.env" "$ENV_FILE"
    cp -p "$BACKUP/nginx.conf" "$CONFIG"
    if [[ -f $BACKUP/source-proxy.conf ]]; then cp -p "$BACKUP/source-proxy.conf" "$PROXY_FILE"; else rm -f "$PROXY_FILE"; fi
    nginx -t && systemctl reload nginx
    systemctl restart t-invest-bot.service || true
    rm -rf "$BACKUP"
    echo "Настройка не завершилась; прежние env и nginx восстановлены. Проверь journalctl -u t-invest-bot -n 50." >&2
    exit "$code"
}
trap rollback ERR
systemctl stop t-invest-bot.service
python3 - "$ENV_FILE" "$PROXY_FILE" <<'PY'
import os
import re
import secrets
import sys
from pathlib import Path
path, proxy = map(Path, sys.argv[1:])
lines = path.read_text().splitlines()
values = dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('#'))
key = values.get('TINVEST_SOURCE_KEY') or secrets.token_hex(32)
if not re.fullmatch(r'[a-f0-9]{64}', key):
    raise SystemExit('Некорректный служебный ключ источника; проверь server.env')
lines = [line for line in lines if not line.startswith(('TINVEST_PULSE_SOURCE=', 'TINVEST_SOURCE_KEY='))]
path.write_text('\n'.join(lines + ['TINVEST_PULSE_SOURCE=shared', 'TINVEST_SOURCE_KEY=' + key]) + '\n')
proxy.write_text('proxy_set_header X-TInvest-Source-Key "' + key + '";\n')
os.chmod(path, 0o600)
os.chmod(proxy, 0o600)
PY
python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" --https --shared-source > "$CONFIG"
chmod 644 "$CONFIG"
nginx -t
install -m 644 "$PROJECT"/deploy/t-invest-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl reset-failed t-invest-window-manager t-invest-vnc t-invest-desktop
systemctl restart t-invest-window-manager t-invest-vnc t-invest-desktop
systemctl reload nginx
systemctl restart t-invest-bot.service
ready=0
for attempt in {1..20}; do
    if curl --fail --silent http://127.0.0.1:8765/api/health >/dev/null; then ready=1; break; fi
    sleep 1
done
[[ $ready == 1 ]]
systemctl is-active t-invest-window-manager t-invest-vnc t-invest-desktop t-invest-bot
trap - ERR
rm -rf "$BACKUP"
echo "Владелец: https://$DOMAIN/source-admin/ · логин source-owner, отдельный пароль."
echo "Клиент: https://$DOMAIN/ · логин admin и прежний пароль панели."
echo "Войди в банк сам на странице владельца. Клиент подключает только свои API-токены."
