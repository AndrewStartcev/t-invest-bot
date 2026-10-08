"""Set the owner's quick-access PIN without echoing it or saving it in Git."""
import getpass
import os
import re
import tempfile
from pathlib import Path


def main():
    if os.geteuid() != 0:
        raise SystemExit("Запусти через sudo python3 scripts/configure_pulse_login.py")
    path = Path("/etc/t-invest-bot/server.env")
    original = path.read_text(encoding="utf-8")
    pin = getpass.getpass("Код быстрого доступа Т-Банка (4 цифры, ввод скрыт): ")
    if not re.fullmatch(r"[0-9]{4}", pin):
        raise SystemExit("Нужно ровно 4 цифры; настройки не изменены")
    if pin != getpass.getpass("Повтори код: "):
        raise SystemExit("Коды не совпадают; настройки не изменены")
    lines = [line for line in original.splitlines() if not line.startswith("PULSE_QUICK_PIN=")]
    lines.append("PULSE_QUICK_PIN=" + pin)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pulse-login-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    # Explicit reconfiguration authorizes one new attempt after an incorrect PIN.
    data_line = next((line for line in lines if line.startswith("TINVEST_DATA_DIR=")), "")
    if data_line:
        root = Path(data_line.partition("=")[2].strip().strip('\"'))
        for directory in (root / "pulse-browser", root / "source" / "pulse-browser"):
            (directory / "quick-login-attempted").unlink(missing_ok=True)
    print("Код сохранён. Выполни: sudo systemctl restart t-invest-bot")


if __name__ == "__main__":
    main()
