#!/usr/bin/env bash
set -euo pipefail
umask 022
DOMAIN=${1:-invest.argokov.ru}
PROJECT=/opt/t-invest-bot
if [[ $EUID -ne 0 ]]; then echo "Запусти установку через sudo" >&2; exit 1; fi
if [[ "$(cd -- "$(dirname -- "$0")/.." && pwd)" != "$PROJECT" ]]; then
    echo "Помести проект в /opt/t-invest-bot и запусти скрипт оттуда" >&2; exit 1
fi
python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" >/dev/null
source /etc/os-release
if [[ ! ( $ID == ubuntu && ( $VERSION_ID == 22.04 || $VERSION_ID == 24.04 ) ) && ! ( $ID == debian && $VERSION_ID == 12 ) ]]; then
    echo "Установщик рассчитан на Ubuntu 22.04/24.04 или Debian 12" >&2; exit 1
fi
if [[ -f /etc/t-invest-bot/server.env ]] && ! grep -Fxq "TINVEST_PUBLIC_ORIGIN=https://$DOMAIN" /etc/t-invest-bot/server.env; then
    echo "Уже настроен другой домен. Проверь /etc/t-invest-bot/server.env" >&2; exit 1
fi
apt-get update
apt-get install -y python3 python3-venv git nginx apache2-utils ca-certificates \
    xvfb x11vnc openbox novnc websockify x11-utils certbot python3-certbot-nginx
update-ca-certificates
if ! id t-invest-bot >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/t-invest-bot --shell /usr/sbin/nologin t-invest-bot
fi
install -d -m 700 -o t-invest-bot -g t-invest-bot /var/lib/t-invest-bot
install -d -m 750 /etc/t-invest-bot
install -d -m 755 /var/www/t-invest-bot-acme
python3 -m venv "$PROJECT/.venv"
"$PROJECT/.venv/bin/python" -m pip install -r "$PROJECT/requirements.txt"
PLAYWRIGHT_BROWSERS_PATH="$PROJECT/.playwright" "$PROJECT/.venv/bin/python" -m playwright install --with-deps chromium
cat > /etc/t-invest-bot/server.env <<EOF
TINVEST_PUBLIC_ORIGIN=https://$DOMAIN
TINVEST_DATA_DIR=/var/lib/t-invest-bot
PLAYWRIGHT_BROWSERS_PATH=$PROJECT/.playwright
DISPLAY=:99
PYTHONUNBUFFERED=1
EOF
chmod 600 /etc/t-invest-bot/server.env
if [[ ! -f /etc/nginx/t-invest-bot.htpasswd ]]; then
    echo "Задай пароль входа в панель для пользователя admin:"
    htpasswd -cB /etc/nginx/t-invest-bot.htpasswd admin
fi
chown root:www-data /etc/nginx/t-invest-bot.htpasswd
chmod 640 /etc/nginx/t-invest-bot.htpasswd
if [[ ! -f /etc/nginx/sites-available/t-invest-bot.conf ]]; then
    python3 "$PROJECT/scripts/render_nginx.py" "$DOMAIN" > /etc/nginx/sites-available/t-invest-bot.conf
fi
if [[ -e /etc/nginx/sites-enabled/t-invest-bot.conf && ! -L /etc/nginx/sites-enabled/t-invest-bot.conf ]]; then
    echo "sites-enabled/t-invest-bot.conf уже существует и не является ссылкой" >&2; exit 1
fi
ln -sfn /etc/nginx/sites-available/t-invest-bot.conf /etc/nginx/sites-enabled/t-invest-bot.conf
nginx -t
install -m 644 "$PROJECT"/deploy/t-invest-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now t-invest-display.service t-invest-window-manager.service t-invest-vnc.service t-invest-desktop.service
systemctl enable t-invest-bot.service
systemctl restart t-invest-bot.service
systemctl enable --now nginx
systemctl reload nginx
echo "Сервисы установлены. Направь A-запись $DOMAIN на этот сервер, затем выполни:"
echo "sudo bash /opt/t-invest-bot/scripts/enable_https.sh $DOMAIN ТВОЙ_EMAIL"
echo "До выпуска HTTPS публичная панель возвращает 503 и не принимает токены."
