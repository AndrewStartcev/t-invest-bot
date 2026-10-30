"""Pairing credentials identify the customer's cabinet; a user hint never does."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from tenant_gateway import Gateway, Manager


class Accounts:
    def __init__(self, directory):
        self.root=Path(directory)
        for name in ("admin","client_one","client_two"):
            (self.root/"clients"/name).mkdir(parents=True)
    def users(self):
        return {name:{} for name in ("admin","client_one","client_two")}
    def directory(self,user):
        if user not in self.users(): raise ValueError("Unknown user")
        return self.root/"clients"/user


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        data=self.rfile.read(int(self.headers["Content-Length"]))
        assert json.loads(data)["kind"]=="config"
        assert self.headers["X-TInvest-Extension-Key"]==self.server.secret
        result=json.dumps({"ok":True,"account_login":self.server.username}).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(result)))
        self.end_headers()
        self.wfile.write(result)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.accounts=Accounts(self.temp.name)
        self.keys={"admin":"A"*48,"client_one":"B"*48,"client_two":"C"*48}
        for name,secret in self.keys.items():
            (self.accounts.directory(name)/"extension-pairing-key.txt").write_text(secret+"\n")
        self.manager=type("ManagerFixture",(),{})()
        self.manager.accounts=self.accounts
        self.manager.extension_client=lambda key: Manager.extension_client(self.manager,key)

    def test_identify_by_key_only(self):
        for name,key in self.keys.items():
            self.assertEqual(name,self.manager.extension_client(key))
        for bad in ("","D"*48,"client_one","🔑"*35):
            self.assertIsNone(self.manager.extension_client(bad))
        (self.accounts.directory("client_two")/"extension-pairing-key.txt").write_text(self.keys["client_one"])
        with self.assertRaisesRegex(RuntimeError,"Повторяющийся"):
            self.manager.extension_client(self.keys["client_one"])

    def test_gateway_ignores_wrong_admin_hint(self):
        services=[];threads=[];workers={}
        for name,secret in self.keys.items():
            server=ThreadingHTTPServer(("127.0.0.1",0),Endpoint)
            server.username=name;server.secret=secret
            services.append(server)
            worker=type("Worker",(),{})()
            worker.port=server.server_port;worker.key="internal";worker.start=lambda:None
            workers[name]=worker
            th=threading.Thread(target=server.serve_forever,daemon=True);th.start();threads.append(th)
        self.manager.client=lambda user: workers[user]
        gateway=ThreadingHTTPServer(("127.0.0.1",0),Gateway)
        gateway.manager=self.manager
        gateway_thread=threading.Thread(target=gateway.serve_forever,daemon=True)
        gateway_thread.start()
        try:
            base=f"http://127.0.0.1:{gateway.server_port}/api/extension/report"
            def send(key,hint=None):
                headers={"Content-Type":"application/json","X-TInvest-Extension-Key":key}
                if hint:headers["X-TInvest-Extension-Client"]=hint
                req=Request(base,data=b'{"kind":"config"}',headers=headers)
                with urlopen(req,timeout=4) as response:return json.load(response)
            self.assertEqual("client_one",send(self.keys["client_one"])["account_login"])
            self.assertEqual("client_two",send(self.keys["client_two"],"admin")["account_login"])
            with self.assertRaises(HTTPError) as failed:send("D"*48,"admin")
            self.assertEqual(401,failed.exception.code)
        finally:
            gateway.shutdown();gateway.server_close();gateway_thread.join(timeout=3)
            for server,thread in zip(services,threads):
                server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=="__main__":unittest.main()
