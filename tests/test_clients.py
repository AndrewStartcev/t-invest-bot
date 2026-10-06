import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from client_accounts import Accounts, atomic_text, hash_password, username, password
from scripts.migrate_clients import migrate
from shared_pulse import read_source
from tenant_gateway import Gateway
from scripts.render_nginx import render

ROOT = Path(__file__).resolve().parents[1]


class ClientTests(unittest.TestCase):
    def test_password_hashing_does_not_put_the_password_in_process_arguments(self):
        secret = 'test-password-only-for-fixture'
        result = SimpleNamespace(returncode=0, stdout='client1:$2y$12$' + 'a' * 53 + '\n')
        with patch('client_accounts.subprocess.run', return_value=result) as run:
            hash_password('client1', secret)
        self.assertNotIn(secret, repr(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['input'], secret + '\n')

    @unittest.skipUnless(shutil.which('nginx') and shutil.which('htpasswd'), 'nginx/apache2-utils required')
    def test_nginx_uses_authenticated_login_not_a_client_supplied_identity_header(self):
        import base64
        import socket
        from http.server import BaseHTTPRequestHandler
        class Echo(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                body = json.dumps({'user': self.headers.get('X-TInvest-User'),
                                   'key': self.headers.get('X-TInvest-Gateway-Key')}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            auth = root / 'auth'
            auth.write_text(hash_password('client1', 'first-password-long') + '\n' +
                            hash_password('client2', 'second-password-long') + '\n')
            proxy = root / 'client-proxy'
            proxy.write_text('proxy_set_header X-TInvest-Gateway-Key "trusted-fixture-key";\n')
            source_proxy = root / 'source-proxy'
            source_proxy.write_text('proxy_set_header X-TInvest-Source-Key "source-fixture-key";\n')
            echo = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
            threading.Thread(target=echo.serve_forever, daemon=True).start()
            ports = []
            for _ in range(2):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); ports.append(sock.getsockname()[1])
            config = render('invest.argokov.ru', True, True, True)
            config = config.replace('listen 80;', f'listen 127.0.0.1:{ports[0]};')
            config = config.replace('listen 443 ssl;', f'listen 127.0.0.1:{ports[1]};')
            config = '\n'.join(line for line in config.splitlines() if not line.strip().startswith('ssl_'))
            config = config.replace('/var/lib/t-invest-auth/clients.htpasswd', str(auth))
            config = config.replace('/etc/nginx/t-invest-source.htpasswd', str(root / 'owner-auth'))
            (root / 'owner-auth').write_text(hash_password('source-owner', 'owner-password-long') + '\n')
            config = config.replace('/etc/t-invest-bot/client-proxy.conf', str(proxy))
            config = config.replace('/etc/t-invest-bot/source-proxy.conf', str(source_proxy))
            config = config.replace('http://127.0.0.1:8765', f'http://127.0.0.1:{echo.server_port}')
            path = root / 'nginx.conf'
            path.write_text(f'pid {root}/pid; error_log {root}/error.log; events {{}} http {{ access_log off; {config} }}')
            process = subprocess.Popen(['nginx', '-p', str(root), '-c', str(path), '-g', 'daemon off; master_process off;'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                base = f'http://127.0.0.1:{ports[1]}'
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(('127.0.0.1', ports[1]), timeout=.1): break
                    except OSError: time.sleep(.05)
                for user, value in [('client1', 'first-password-long'), ('client2', 'second-password-long')]:
                    credentials = base64.b64encode((user+':'+value).encode()).decode()
                    headers = {'Authorization': 'Basic '+credentials, 'X-TInvest-User': 'forged-user',
                               'X-TInvest-Gateway-Key': 'forged-key', 'X-TInvest-Source-Key': 'source-fixture-key'}
                    with urlopen(Request(base+'/', headers=headers)) as response:
                        data = json.load(response)
                    self.assertEqual(data, {'user': user, 'key': 'trusted-fixture-key'})
                    for protected in ['/source-admin/', '/desktop/vnc.html', '/desktop/websockify']:
                        with self.assertRaises(HTTPError) as failure:
                            urlopen(Request(base+protected, headers=headers))
                        self.assertEqual(failure.exception.code, 401)
            finally:
                process.terminate()
                process.communicate(timeout=5)
                echo.shutdown(); echo.server_close()

    def test_profile_switch_reads_only_the_requested_author(self):
        class Browser:
            profile_name = 'Alpha'
            list_url = 'private-url-never-in-response'
            def refresh(self, url):
                self.profile_name = url.rstrip('/').split('/')[-1]
                self.list_url = None
            def snapshot(self, url, counts):
                self.list_url = 'private-url-never-in-response'
                return self.profile_name, [{'ticker': self.profile_name, 'history': []}]
            def history(self, ticker, class_code, cursor):
                return {'items': [{'author': self.profile_name}], 'hasNext': False}
        browser = Browser()
        beta = 'https://www.tbank-online.com/invest/social/profile/Beta/'
        result = read_source(browser, {'profile_url': beta, 'action': 'snapshot', 'counts': {}})
        self.assertEqual(result['profile'], 'Beta')
        self.assertNotIn('private-url', json.dumps(result))
        alpha = 'https://www.tbank-online.com/invest/social/profile/Alpha/'
        history = read_source(browser, {'profile_url': alpha, 'action': 'history', 'ticker': 'X', 'class_code': 'TQBR'})
        self.assertEqual(history['items'][0]['author'], 'Alpha')
        with self.assertRaises(Exception):
            read_source(browser, {'profile_url': 'http://evil.example/', 'action': 'snapshot'})

    def test_migration_preserves_tokens_journal_and_separates_bank_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'data'
            root.mkdir()
            auth = Path(folder) / 'clients.htpasswd'
            legacy = Path(folder) / 'old.htpasswd'
            legacy.write_text('admin:existing-password-hash\n')
            (root / 'broker-trade-token.txt').write_text('private-client-token')
            (root / 'real-orders.json').write_text('{"order1":{"id":"unchanged"}}')
            (root / 'pulse-browser').mkdir()
            (root / 'pulse-browser/pulse-session.json').write_text('private-bank-session')
            (root / 'demo-settings.json').write_text('{"real_mode":"off"}')
            self.assertTrue(migrate(root, auth, legacy))
            self.assertEqual((root / 'clients/admin/broker-trade-token.txt').read_text(), 'private-client-token')
            self.assertEqual((root / 'clients/admin/real-orders.json').read_text(), '{"order1":{"id":"unchanged"}}')
            self.assertFalse((root / 'clients/admin/pulse-browser').exists())
            self.assertEqual((root / 'source/pulse-browser/pulse-session.json').read_text(), 'private-bank-session')
            self.assertEqual(auth.read_text(), legacy.read_text())
            self.assertFalse(migrate(root, auth, legacy))

    def test_migration_refuses_enabled_trading_before_moving_any_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'demo-settings.json').write_text('{"real_mode":"auto"}')
            (root / 'legacy').write_text('admin:hash\n')
            with self.assertRaisesRegex(ValueError, 'выключи реальную'):
                migrate(root, root / 'auth', root / 'legacy')
            self.assertFalse((root / 'clients').exists())

    def test_usernames_and_passwords_do_not_allow_paths_or_stdin_injection(self):
        for value in ['../admin', 'source-owner', 'admin:other', 'Admin', '/tmp/data']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                username(value)
        for value in ['short', 'new-password\nother-user', 'Ж' * 40]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                password(value)

    @unittest.skipUnless(shutil.which('htpasswd'), 'apache2-utils required')
    def test_password_change_preserves_other_clients_and_rejects_wrong_current(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            auth = root / 'auth'
            auth.write_text(hash_password('admin', 'old-password-long') + '\n')
            (root / 'clients.json').write_text('{"admin":{}}')
            accounts = Accounts(root, auth)
            accounts.create('client2', 'second-password-long')
            other_before = auth.read_text().splitlines()[1]
            with self.assertRaisesRegex(ValueError, 'Текущий пароль неверен'):
                accounts.change_password('admin', 'wrong-password-long', 'new-password-long')
            accounts.change_password('admin', 'old-password-long', 'new-password-long')
            self.assertEqual(auth.read_text().splitlines()[1], other_before)
            for value, expected in [('old-password-long', False), ('new-password-long', True)]:
                result = subprocess.run(['htpasswd', '-vi', str(auth), 'admin'], input=value+'\n',
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, expected)
            self.assertNotIn('new-password-long', auth.read_text())

    def test_gateway_routes_real_isolated_processes_and_rejects_forged_identity(self):
        # Two independent Python processes, each running the actual HTTP Handler with its own files.
        # Background workers are not started: no external API or real order can be sent by this test.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            processes, workers = [], {}
            gateway = None
            for user in ['client1', 'client2']:
                directory = root / 'clients' / user
                directory.mkdir(parents=True)
                (directory / 'broker-read-token.txt').write_text(user + '-private-token')
                (directory / 'events.json').write_text(json.dumps([{'owner': user}]))
                (directory / 'real-orders.json').write_text(json.dumps({user: {'id': user}}))
                ready = root / (user + '.ready')
                key = user + '-backend-key-' + 'a' * 32
                code = ('import demo_admin,json; from pathlib import Path; '
                        'server=demo_admin.LocalHTTPServer(("127.0.0.1",0),demo_admin.Handler); '
                        f'Path({str(ready)!r}).write_text(str(server.server_port)); server.serve_forever()')
                env = {**os.environ, 'TINVEST_DATA_DIR': str(directory), 'TINVEST_BACKEND_KEY': key,
                       'TINVEST_PULSE_SOURCE': 'shared', 'TINVEST_SOURCE_KEY': 's' * 64,
                       'TINVEST_PUBLIC_ORIGIN': 'https://invest.argokov.ru'}
                processes.append(subprocess.Popen([sys.executable, '-c', code], env=env, cwd=ROOT))
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(ready.exists())
                workers[user] = SimpleNamespace(port=int(ready.read_text()), key=key, start=lambda: None)
            (root / 'clients.json').write_text('{"client1":{},"client2":{}}')
            accounts = Accounts(root, root / 'auth')
            manager = SimpleNamespace(accounts=accounts, client=lambda user: workers[user])
            env = {'TINVEST_GATEWAY_KEY': 'g'*64, 'TINVEST_SOURCE_KEY': 's'*64,
                   'TINVEST_PUBLIC_ORIGIN': 'https://invest.argokov.ru'}
            try:
                with patch.dict(os.environ, env):
                    gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
                    gateway.manager = manager
                    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
                    thread.start()
                    base = f'http://127.0.0.1:{gateway.server_port}'
                    def request(user, path='/api/state', key='g'*64, payload=None):
                        return urlopen(Request(base+path, data=json.dumps(payload).encode() if payload else None,
                            headers={'X-TInvest-Gateway-Key': key, 'X-TInvest-User': user,
                                     'X-TInvest-Backend-Key': workers['client2'].key,
                                     'Content-Type': 'application/json', 'Origin': env['TINVEST_PUBLIC_ORIGIN']}))
                    for user in workers:
                        with request(user) as response:
                            state = json.load(response)
                        self.assertEqual(state['account']['username'], user)
                        self.assertEqual(state['events'][0]['owner'], user)
                        self.assertEqual(state['real_orders'][0]['id'], user)
                        self.assertNotIn('-private-token', json.dumps(state))
                    with self.assertRaises(HTTPError) as failure:
                        request('client2', key='forged')
                    self.assertEqual(failure.exception.code, 403)
                    with self.assertRaises(HTTPError) as failure:
                        request('client1', '/source-admin/api/clients')
                    self.assertEqual(failure.exception.code, 403)
                    with self.assertRaises(HTTPError) as failure:
                        request('client1', '/api/auth/start', payload={"x": 1})
                    self.assertEqual(failure.exception.code, 403)
                    with self.assertRaises(HTTPError) as failure:
                        urlopen(f'http://127.0.0.1:{workers["client1"].port}/api/state')
                    self.assertEqual(failure.exception.code, 403)
                    # A request-body username is never a routing authority.
                    with request('client1', '/api/demo/scenario', payload={'scenario': 'stock_buy', 'username': 'client2'}) as response:
                        self.assertEqual(response.status, 200)
                    self.assertFalse((root / 'clients/client2/demo-positions.json').exists())
            finally:
                if gateway:
                    gateway.shutdown()
                    gateway.server_close()
                for process in processes:
                    process.terminate()
                    process.wait(timeout=5)
