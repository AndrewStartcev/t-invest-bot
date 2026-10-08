#!/usr/bin/env bash
# Trust verified bank CAs in Chromium's service-user NSS store, not the OS store.
set -euo pipefail
umask 077
PROJECT=$(cd -- "$(dirname -- "$0")/.." && pwd)
if [[ ${1:-} == --check-only && $# == 1 ]]; then
    exec bash "$PROJECT/scripts/install_broker_ca.sh" --check-only
fi
if [[ $# != 0 || $EUID -ne 0 ]]; then
    echo "Использование: sudo bash $0 [--check-only]" >&2; exit 1
fi
# Fingerprints, expiry and issuer signatures are verified before any import.
bash "$PROJECT/scripts/install_broker_ca.sh" --check-only
PULSE_SERVICE_HOME=$(getent passwd t-invest-bot | cut -d: -f6)
if [[ $PULSE_SERVICE_HOME != /var/lib/t-invest-bot ]]; then
    echo "Ожидался служебный пользователь с домашней папкой /var/lib/t-invest-bot" >&2; exit 1
fi
if ! command -v certutil >/dev/null; then
    apt-get update
    apt-get install -y libnss3-tools
fi
install -d -m 700 -o t-invest-bot -g t-invest-bot \
    "$PULSE_SERVICE_HOME/.pki" "$PULSE_SERVICE_HOME/.local" \
    "$PULSE_SERVICE_HOME/.local/share" "$PULSE_SERVICE_HOME/.local/share/pki"
# M146+ uses .local/share/pki; older Chromium and existing installs use .pki.
for database in "$PULSE_SERVICE_HOME/.pki/nssdb" "$PULSE_SERVICE_HOME/.local/share/pki/nssdb"; do
    install -d -m 700 -o t-invest-bot -g t-invest-bot "$database"
    if [[ ! -f $database/cert9.db ]]; then
        runuser -u t-invest-bot -- certutil -N -d "sql:$database" --empty-password
    fi
    for kind in root sub; do
        nickname="T-Invest Russian Trusted $kind CA"
        trust=",,"; [[ $kind == root ]] && trust="C,,"
        if runuser -u t-invest-bot -- certutil -L -d "sql:$database" -n "$nickname" >/dev/null 2>&1; then
            runuser -u t-invest-bot -- certutil -D -d "sql:$database" -n "$nickname"
        fi
        runuser -u t-invest-bot -- certutil -A -d "sql:$database" -n "$nickname" -t "$trust" \
            -i "$PROJECT/deploy/certs/russian_trusted_${kind}_ca.crt"
        runuser -u t-invest-bot -- certutil -L -d "sql:$database" -n "$nickname" -a | \
            openssl x509 -noout -subject -fingerprint -sha256
    done
done
echo "CA установлены в NSS служебного пользователя. Проверка TLS остаётся включённой."
if systemctl is-active --quiet t-invest-bot.service; then systemctl restart t-invest-bot.service; fi
if [[ -x $PROJECT/.venv/bin/python ]]; then
    runuser -u t-invest-bot -- env PLAYWRIGHT_BROWSERS_PATH="$PROJECT/.playwright" \
        "$PROJECT/.venv/bin/python" "$PROJECT/scripts/diagnose_pulse_tls.py"
fi
