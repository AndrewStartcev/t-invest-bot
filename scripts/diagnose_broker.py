"""Read-only diagnostics; never prints/stores tokens or places orders."""
import argparse
import getpass
from pathlib import Path
import os
import socket
import ssl
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from broker_http import APIError, network_error_message, normalize_token, request_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", action="store_true", help="Запросить токен скрытым вводом и проверить GetAccounts")
    args = parser.parse_args()
    print("UTC:", datetime.now(timezone.utc).isoformat())
    print("Python:", sys.version.split()[0])
    print("Прокси в окружении:", "есть" if any(os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")) else "нет")
    host = "invest-public-api.tbank.ru"
    failed = False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        print("DNS:", ", ".join(sorted(addresses)))
        with socket.create_connection((host, 443), timeout=12) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as secure:
                print("Прямое TLS-соединение:", secure.version(), "· сертификат проверен")
    except (OSError, TimeoutError) as error:
        print("Сеть:", network_error_message(error))
        failed = True
    if args.token:
        try:
            token = normalize_token(getpass.getpass("API-токен (ввод не отображается): "))
            result = request_json("UsersService/GetAccounts", token, {})
            accounts = result.get("accounts")
            if not isinstance(accounts, list):
                raise APIError("Ответ не содержит список accounts")
            print("API: токен принят; счетов:", len(accounts))
            for index, item in enumerate(accounts, 1):
                if isinstance(item, dict):
                    print(f"Счёт {index}: права={item.get('accessLevel')}, статус={item.get('status')}")
            print("ID счетов и токен не выводятся. Заявки не отправлялись.")
        except (APIError, ValueError) as error:
            print("API:", error)
            return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
