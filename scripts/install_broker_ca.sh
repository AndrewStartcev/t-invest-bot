#!/usr/bin/env bash
# App-scoped broker trust; does not add these CAs to the OS/browser trust store.
set -euo pipefail
umask 077
PROJECT=$(cd -- "$(dirname -- "$0")/.." && pwd)
CHECK_ONLY=0
if [[ ${1:-} == --check-only && $# == 1 ]]; then CHECK_ONLY=1
elif [[ $# != 0 ]]; then echo "Использование: bash $0 [--check-only]" >&2; exit 1; fi
if [[ $CHECK_ONLY == 0 && $EUID -ne 0 ]]; then echo "Запусти через sudo" >&2; exit 1; fi
command -v openssl >/dev/null
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
for kind in root sub; do
    cert="$PROJECT/deploy/certs/russian_trusted_${kind}_ca.crt"
    if [[ $kind == root ]]; then
        expected=D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
    else
        expected=BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B
    fi
    fingerprint=$(openssl x509 -in "$cert" -noout -fingerprint -sha256)
    if [[ ${fingerprint#*=} != "$expected" ]]; then
        echo "Отпечаток $kind CA не совпадает с проверенным. Установка остановлена." >&2; exit 1
    fi
    openssl x509 -in "$cert" -checkend 0 -noout
    openssl x509 -in "$cert" -out "$WORK/$kind.pem"
done
openssl verify -check_ss_sig -CAfile "$WORK/root.pem" "$WORK/root.pem" "$WORK/sub.pem"
cat "$WORK/root.pem" "$WORK/sub.pem" > "$WORK/broker-ca.pem"
if [[ $CHECK_ONLY == 1 ]]; then echo "Сертификаты и их цепочка проверены; настройки сервера не менялись."; exit 0; fi
ENV_FILE=/etc/t-invest-bot/server.env
if [[ ! -f $ENV_FILE ]]; then echo "Сначала установи сервер: отсутствует server.env" >&2; exit 1; fi
install -d -m 755 /etc/ssl/t-invest-bot
install -m 644 "$WORK/broker-ca.pem" /etc/ssl/t-invest-bot/broker-ca.pem
python3 - "$ENV_FILE" <<'PY'
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
lines = [line for line in path.read_text().splitlines() if not line.startswith('TINVEST_BROKER_CA_FILE=')]
tmp = path.with_suffix('.env.tmp')
with tmp.open('w') as file:
    os.chmod(tmp, 0o600)
    file.write('\n'.join(lines + ['TINVEST_BROKER_CA_FILE=/etc/ssl/t-invest-bot/broker-ca.pem']) + '\n')
os.replace(tmp, path)
PY
if systemctl is-active --quiet t-invest-bot.service; then systemctl restart t-invest-bot.service; fi
if [[ -x $PROJECT/.venv/bin/python ]]; then
    echo "Проверяем доступ к API без токенов и заявок:"
    sudo -u t-invest-bot env TINVEST_BROKER_CA_FILE=/etc/ssl/t-invest-bot/broker-ca.pem \
        "$PROJECT/.venv/bin/python" "$PROJECT/scripts/diagnose_broker.py"
fi
echo "Дополнительные CA настроены только для T-Invest HTTP-клиента. Проверка TLS и имени сервера включена."
