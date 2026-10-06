"""Authenticated routing to isolated client processes and one private Pulse source."""
import argparse
import hmac
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from client_accounts import Accounts
from telegram_router import run_router
from server_config import public_origin, source_key, validate_source_config

ROOT = Path(__file__).resolve().parent


def matches(value, expected):
    return len(expected) >= 32 and hmac.compare_digest(value.encode(), expected.encode())


class Worker:
    def __init__(self, role, directory, env, runtime):
        self.role, self.directory = role, Path(directory)
        self.env = env.copy()
        self.key = secrets.token_hex(32)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.ready = Path(tempfile.mkdtemp(prefix=role + "-", dir=runtime)) / "ready.json"
        self.process = None
        self.lock = threading.Lock()
        self.last_start = 0

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return
            if time.monotonic() - self.last_start < 5:
                raise RuntimeError("Процесс восстанавливается")
            if self.process:
                self.stop()
            self.last_start = time.monotonic()
            self.ready.unlink(missing_ok=True)
            env = {**self.env, "TINVEST_WORKER_ROLE": self.role, "TINVEST_DATA_DIR": str(self.directory),
                   "TINVEST_BACKEND_KEY": self.key, "TINVEST_READY_FILE": str(self.ready)}
            self.process = subprocess.Popen([sys.executable, "-u", str(ROOT / "demo_admin.py"),
                                             "--port", str(self.port)], env=env, cwd=ROOT, start_new_session=True)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and self.process.poll() is None:
                if self.ready.exists():
                    data = json.loads(self.ready.read_text())
                    if data == {"port": self.port, "pid": self.process.pid}:
                        return
                time.sleep(0.05)
            self.stop()
            raise RuntimeError("Не удалось запустить клиентский процесс")

    def stop(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)


class Manager:
    def __init__(self, accounts, runtime):
        self.accounts = accounts
        self.runtime = runtime
        self.lock = threading.RLock()
        self.workers = {}
        self.source = Worker("source", accounts.root / "source", dict(os.environ), runtime)
        try:
            self.source.start()
            for user in accounts.users():
                self.client(user)
        except Exception:
            self.stop()
            raise

    def client(self, user):
        with self.lock:
            directory = self.accounts.directory(user)
            if user not in self.workers:
                env = {**os.environ, "TINVEST_SOURCE_RPC_URL": f"http://127.0.0.1:{self.source.port}",
                       "TINVEST_SOURCE_RPC_KEY": self.source.key,
                       "TINVEST_SOURCE_KEY": secrets.token_hex(32)}
                self.workers[user] = Worker("client", directory, env, self.runtime)
            self.workers[user].start()
            return self.workers[user]

    def supervise(self):
        while True:
            with self.lock:
                workers = [self.source, *self.workers.values()]
            for worker in workers:
                try:
                    worker.start()
                except Exception:
                    print("Клиентский процесс недоступен; повторим запуск", flush=True)
            time.sleep(5)

    def stop(self):
        for worker in [*self.workers.values(), self.source]:
            worker.stop()

    def internal(self, worker, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", worker.port, timeout=30)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body).encode() if body is not None else None,
                               {"X-TInvest-Backend-Key": worker.key, "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("Клиентский обработчик недоступен")
            return json.loads(response.read(256 * 1024))
        finally:
            connection.close()

    def telegram_configs(self):
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            try:
                yield worker, self.internal(worker, "/_telegram/config")
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                continue


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_request(self, post=False):
        if self.path == "/api/health" and not post:
            self.reply(200, {"status": "ok", "server_mode": True, "multi_user": True})
            return
        owner = self.path.startswith("/source-admin")
        expected = source_key() if owner else os.environ.get("TINVEST_GATEWAY_KEY", "")
        header = "X-TInvest-Source-Key" if owner else "X-TInvest-Gateway-Key"
        if not matches(self.headers.get(header, ""), expected):
            self.reply(403, {"error": "Доступ отклонён"})
            return
        manager = self.server.manager
        user = self.headers.get("X-TInvest-User", "")
        try:
            if not owner:
                manager.accounts.directory(user)
                if self.path != "/" and not self.path.startswith("/api/"):
                    self.reply(404, {"error": "Не найдено"})
                    return
            data, body = {}, None
            if post:
                if self.headers.get("Origin") != public_origin():
                    self.reply(403, {"error": "Запрос из другого источника отклонён"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.reply(415, {"error": "Нужен JSON"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Некорректный размер запроса")
                body = self.rfile.read(length)
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("Нужен объект JSON")
            if owner and self.path == "/source-admin/api/clients":
                if post:
                    manager.accounts.create(data.get("username"), data.get("password"))
                    manager.client(data["username"])
                self.reply(200, {"clients": [{"username": name, **metadata}
                                            for name, metadata in manager.accounts.users().items()]})
                return
            if not owner and self.path == "/api/account/password" and post:
                manager.accounts.change_password(user, data.get("current_password"), data.get("new_password"))
                self.reply(200, {"ok": True, "message": "Пароль изменён. Обнови страницу и войди с новым паролем."})
                return
            worker = manager.source if owner else manager.client(user)
            worker.start()
            connection = http.client.HTTPConnection("127.0.0.1", worker.port, timeout=65)
            try:
                # User-supplied identity, authorization, cookies and private headers are not forwarded.
                headers = {"X-TInvest-Backend-Key": worker.key}
                for name in ["Content-Type", "Origin"]:
                    if self.headers.get(name):
                        headers[name] = self.headers[name]
                if owner:
                    headers["X-TInvest-Source-Key"] = source_key()
                connection.request("POST" if post else "GET", self.path, body, headers)
                response = connection.getresponse()
                output = response.read(8 * 1024 * 1024 + 1)
                if len(output) > 8 * 1024 * 1024:
                    raise RuntimeError("Слишком большой ответ")
                if not owner and self.path == "/api/state" and response.status == 200:
                    state = json.loads(output)
                    state["account"] = {"username": user, "password_change": True, "multi_user": True}
                    output = json.dumps(state, ensure_ascii=False).encode()
                elif owner and self.path == "/source-admin/api/state" and response.status == 200:
                    state = json.loads(output)
                    state["multi_user"] = True
                    output = json.dumps(state, ensure_ascii=False).encode()
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(output)))
                if response.getheader("Location"):
                    self.send_header("Location", response.getheader("Location"))
                self.end_headers()
                self.wfile.write(output)
            finally:
                connection.close()
        except (ValueError, json.JSONDecodeError) as error:
            self.reply(400, {"error": str(error)})
        except (OSError, RuntimeError, subprocess.SubprocessError, http.client.HTTPException):
            # Brokerage POST is never automatically repeated after a transport failure.
            self.reply(503, {"error": "Кабинет временно недоступен. Отправленные заявки проверь после восстановления; не повторяй их вслепую."})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request(True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    validate_source_config()
    if len(os.environ.get("TINVEST_GATEWAY_KEY", "")) < 32:
        raise SystemExit("Не настроен ключ клиентского шлюза")
    os.umask(0o077)
    root = Path(os.environ["TINVEST_DATA_DIR"])
    accounts = Accounts(root, os.environ["TINVEST_CLIENT_AUTH_FILE"])
    runtime = Path(os.environ.get("TMPDIR", tempfile.gettempdir()))
    manager = Manager(accounts, runtime)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Gateway)
    except Exception:
        manager.stop()
        raise
    server.manager = manager
    threading.Thread(target=manager.supervise, daemon=True).start()
    threading.Thread(target=run_router, args=(manager.telegram_configs,
                     lambda worker, query: manager.internal(worker, "/_telegram/callback", query),
                     root / "telegram-router-state.json"), daemon=True).start()
    def shutdown(*_):
        manager.stop()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    finally:
        manager.stop()
        server.server_close()
