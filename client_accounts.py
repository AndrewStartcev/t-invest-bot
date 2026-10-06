"""Client identities; htpasswd receives passwords via stdin, never argv/logs."""
import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", value) or value == "source-owner":
        raise ValueError("Логин: 3–32 символа, латиница в нижнем регистре, цифры, _ или -")
    return value


def password(value, *, new=True):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Пароль содержит недопустимые символы")
    if len(value.encode("utf-8")) > 72 or (new and len(value) < 12):
        raise ValueError("Новый пароль: минимум 12 символов, максимум 72 байта UTF-8")
    return value


def atomic_text(path, text, mode=0o600):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), mode)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def hash_password(user, value):
    result = subprocess.run(["htpasswd", "-niB", "-C", "12", username(user)],
                            input=password(value) + "\n", text=True, capture_output=True, timeout=10)
    line = result.stdout.strip()
    if result.returncode or not line.startswith(user + ":$2") or "\n" in line:
        raise ValueError("Не удалось сохранить пароль; проверь apache2-utils")
    return line


class Accounts:
    def __init__(self, data_root, auth_file):
        self.root = Path(data_root)
        self.path = self.root / "clients.json"
        self.auth_file = Path(auth_file)
        self.lock = threading.RLock()

    def users(self):
        data = json.loads(self.path.read_text())
        if not isinstance(data, dict) or any(username(user) != user for user in data):
            raise ValueError("Некорректный реестр клиентов")
        return data

    def directory(self, user):
        username(user)
        if user not in self.users():
            raise ValueError("Клиент не зарегистрирован")
        return self.root / "clients" / user

    def create(self, user, new_password):
        user = username(user)
        record = hash_password(user, new_password)
        with self.lock:
            users = self.users()
            if user in users:
                raise ValueError("Такой логин уже существует")
            if len(users) >= 10:
                raise ValueError("Лимит этой установки — 10 клиентов; расширение требует оценки ресурсов")
            # A crash between writes fails closed: nginx may accept a login that the registry rejects.
            auth = self.auth_file.read_text()
            if any(line.split(":", 1)[0] == user for line in auth.splitlines()):
                raise ValueError("Логин уже присутствует в файле доступа")
            directory = self.root / "clients" / user
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            users[user] = {"created_at": datetime.now(timezone.utc).isoformat()}
            atomic_text(self.auth_file, auth.rstrip("\n") + "\n" + record + "\n", 0o640)
            atomic_text(self.path, json.dumps(users, ensure_ascii=False))

    def change_password(self, user, current, new):
        username(user)
        password(current, new=False)
        password(new)
        with self.lock:
            self.directory(user)
            result = subprocess.run(["htpasswd", "-vi", str(self.auth_file), user],
                                    input=current + "\n", text=True, capture_output=True, timeout=10)
            if result.returncode:
                raise ValueError("Текущий пароль неверен")
            record = hash_password(user, new)
            lines = self.auth_file.read_text().splitlines()
            atomic_text(self.auth_file, "\n".join(record if line.split(":", 1)[0] == user else line
                                                for line in lines) + "\n", 0o640)
