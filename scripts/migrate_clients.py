"""Explicit, recoverable single-client migration; run only with the bot stopped."""
import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client_accounts import atomic_text

FILES = ["demo-settings.json", "pulse-live-state.json", "events.json", "demo-positions.json",
         "telegram-token.txt", "broker-read-token.txt", "broker-account.txt", "broker-trade-token.txt",
         "trade-account.txt", "real-orders.json"]


def migrate(root, auth_file, legacy_auth):
    root, auth_file, legacy_auth = map(Path, (root, auth_file, legacy_auth))
    if (root / "clients.json").exists():
        if not auth_file.exists():
            raise ValueError("Есть реестр, но отсутствует файл клиентских паролей")
        return False
    rows = [line for line in legacy_auth.read_text().splitlines() if line]
    if len(rows) != 1 or not rows[0].startswith("admin:"):
        raise ValueError("Автоматический перенос рассчитан на один прежний логин admin")
    settings_path = root / "demo-settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if settings.get("real_mode", "off") != "off":
        raise ValueError("Перед переносом выключи реальную торговлю в панели")
    client = root / "clients/admin"
    source = root / "source"
    if client.exists() or source.exists():
        raise ValueError("Каталоги переноса уже существуют без реестра; проверь прежнюю попытку")
    client.mkdir(parents=True, mode=0o700)
    source.mkdir(mode=0o700)
    os.chmod(root / "clients", 0o700)
    for name in FILES:
        file = root / name
        if file.is_symlink():
            raise ValueError("Файл данных не должен быть символической ссылкой")
        if file.exists():
            file.rename(client / name)
    browser = root / "pulse-browser"
    if browser.is_symlink():
        raise ValueError("Профиль браузера не должен быть символической ссылкой")
    if browser.exists():
        browser.rename(source / "pulse-browser")
    profile = settings.get("profile_url", "https://www.tbank-online.com/invest/social/profile/LinMath/")
    atomic_text(source / "demo-settings.json", json.dumps({"profile_url": profile,
                "real_mode": "off", "auto_demo_buy": False, "monitoring_enabled": False}))
    atomic_text(auth_file, rows[0] + "\n", 0o640)
    atomic_text(root / "clients.json", json.dumps({"admin": {"created_at": datetime.now(timezone.utc).isoformat()}}))
    return True


def configure(env_path, proxy_path, auth_file):
    env_path, proxy_path = Path(env_path), Path(proxy_path)
    lines = env_path.read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    key = values.get("TINVEST_GATEWAY_KEY") or secrets.token_hex(32)
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Неверный служебный ключ клиентского шлюза")
    prefixes = ("TINVEST_MULTIUSER=", "TINVEST_GATEWAY_KEY=", "TINVEST_CLIENT_AUTH_FILE=")
    lines = [line for line in lines if not line.startswith(prefixes)]
    atomic_text(env_path, "\n".join(lines + ["TINVEST_MULTIUSER=1", "TINVEST_GATEWAY_KEY=" + key,
                "TINVEST_CLIENT_AUTH_FILE=" + str(auth_file)]) + "\n")
    atomic_text(proxy_path, 'proxy_set_header X-TInvest-Gateway-Key "' + key + '";\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/var/lib/t-invest-bot")
    args = parser.parse_args()
    root = Path(args.root)
    migrate(root, "/var/lib/t-invest-auth/clients.htpasswd", "/etc/nginx/t-invest-bot.htpasswd")
    configure("/etc/t-invest-bot/server.env", "/etc/t-invest-bot/client-proxy.conf",
              "/var/lib/t-invest-auth/clients.htpasswd")
