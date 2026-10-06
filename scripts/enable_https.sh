#!/usr/bin/env bash
set -euo pipefail
DOMAIN=${1:-invest.argokov.ru}
EMAIL=${2:?Укажи email для сертификата вторым аргументом}
PROJECT=/opt/t-invest-bot
if [[ $EUID -ne 0 ]]; then echo "Запусти через sudo" >&2; exit 1; fi
python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" >/dev/null
if ! grep -Fxq "TINVEST_PUBLIC_ORIGIN=https://$DOMAIN" /etc/t-invest-bot/server.env; then
    echo "Домен не совпадает с настройками приложения" >&2; exit 1
fi
nginx -t
certbot certonly --webroot -w /var/www/t-invest-bot-acme --non-interactive --agree-tos --email "$EMAIL" -d "$DOMAIN"
CONFIG=/etc/nginx/sites-available/t-invest-bot.conf
BACKUP=$(mktemp)
cp "$CONFIG" "$BACKUP"
python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" --https > "$CONFIG"
if ! nginx -t; then
    cp "$BACKUP" "$CONFIG"
    rm -f "$BACKUP"
    echo "Новый конфиг не принят, прежний восстановлен" >&2; exit 1
fi
rm -f "$BACKUP"
systemctl reload nginx
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/t-invest-bot-reload <<'EOF'
#!/bin/sh
nginx -t && systemctl reload nginx
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/t-invest-bot-reload
systemctl enable --now certbot.timer
echo "Готово: https://$DOMAIN/ · логин admin, пароль задан при установке."
