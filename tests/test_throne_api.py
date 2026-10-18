"""Tests for the Throne API client: RFC 8439 vectors, the protocol's official vectors
(tests/fixtures/throne_api_v1.json, from github.com/throneproj/Throne-api, MIT) and an
end-to-end exchange with a fake Throne server.
"""

import base64
import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import throne_api as api  # noqa: E402
from test_host import make_db, DNS, PROXY  # noqa: E402

VECTORS = json.load(open(os.path.join(ROOT, "tests", "fixtures", "throne_api_v1.json"), encoding="utf-8"))


class Rfc8439Tests(unittest.TestCase):
    def test_chacha20_block(self):
        block = api.chacha20_block(bytes(range(32)), 1, bytes.fromhex("000000090000004a00000000"))
        self.assertTrue(block.hex().startswith("10f1e7e4d13b5915500fdd1fa32071c4"))

    def test_poly1305(self):
        tag = api.poly1305(bytes.fromhex("85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b"),
                           b"Cryptographic Forum Research Group")
        self.assertEqual(tag.hex(), "a8061dc1305136c6c22b8baf0c0127a9")

    def test_aead(self):
        pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip "
              b"for the future, sunscreen would be it.")
        key, nonce, aad = bytes(range(0x80, 0xA0)), bytes.fromhex("070000004041424344454647"), \
            bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        sealed = api.aead_seal(key, nonce, aad, pt)
        self.assertEqual(sealed[:16].hex(), "d31a8d34648e60db7b86afbc53ef7ec2")
        self.assertEqual(sealed[-16:].hex(), "1ae10b594f09e26a7e902ecbd0600691")
        self.assertEqual(api.aead_open(key, nonce, aad, sealed), pt)
        with self.assertRaises(ValueError):
            api.aead_open(key, nonce, aad + b"x", sealed)


class ProtocolVectorTests(unittest.TestCase):
    def test_official_vectors(self):
        for case in VECTORS["cases"]:
            with self.subTest(case=case["name"]):
                key = api.key_from_text(case["key"])
                s, c = bytes.fromhex(case["server_hello"]), bytes.fromhex(case["client_hello"])
                k_c2s, k_s2c = api.derive(key, s, c)
                self.assertEqual((k_c2s.hex(), k_s2c.hex()), (case["k_c2s"], case["k_s2c"]))
                keys = {"c2s": k_c2s, "s2c": k_s2c}
                for f in case["frames"]:
                    wire = api.seal_frame(keys[f["dir"]], f["counter"], f["plaintext"].encode())
                    self.assertEqual(wire.hex(), f["wire"])
                    self.assertEqual(api.open_frame(keys[f["dir"]], f["counter"], bytes.fromhex(f["wire"])),
                                     f["plaintext"].encode())
                for bad in case.get("invalid", []):
                    with self.assertRaises(ValueError, msg=bad["name"]):
                        limit = api.MAX_C2S if bad["dir"] == "c2s" else api.MAX_S2C
                        api.open_frame(keys[bad["dir"]], bad["counter"], bytes.fromhex(bad["wire"]), limit)

    def test_key_text(self):
        self.assertEqual(api.key_from_text(" AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8\n"), bytes(range(32)))
        for bad in ["", "abc", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", "!" * 43]:
            with self.subTest(bad=bad), self.assertRaises(api.ApiError):
                api.key_from_text(bad)


class FakeThrone(threading.Thread):
    """Speaks the server side of throne-api/1 and answers from a script of responses."""

    def __init__(self, key, responses):
        super().__init__(daemon=True)
        self.key, self.responses, self.requests = key, list(responses), []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]

    def run(self):
        conn, _ = self.srv.accept()
        self.srv.close()
        with conn:
            s = os.urandom(16)
            conn.sendall(s)
            hello = self._read(conn, 17)
            if hello[:1] != b"\x01":
                return
            k_c2s, k_s2c = api.derive(self.key, s, hello)
            n = 0
            while self.responses:
                head = self._read(conn, 4)
                if not head:
                    return
                (size,) = struct.unpack(">I", head)
                try:
                    req = api.open_frame(k_c2s, n, head + self._read(conn, size), api.MAX_C2S)
                except ValueError:
                    return  # wrong key: close silently, like Throne
                self.requests.append(json.loads(req))
                conn.sendall(api.seal_frame(k_s2c, n, json.dumps(self.responses.pop(0)).encode()))
                n += 1

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return buf
            buf += chunk
        return buf


class RestartTests(unittest.TestCase):
    KEY = bytes(range(32))
    KEY_TEXT = base64.urlsafe_b64encode(KEY).rstrip(b"=").decode()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "throne.db")
        make_db(self.db, [DNS, PROXY])

    def tearDown(self):
        self.tmp.cleanup()

    def set_api(self, enabled, port, key):
        con = sqlite3.connect(self.db)
        con.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                        [("remote_api_enable", enabled), ("remote_api_port", str(port)), ("remote_api_key", key)])
        con.commit()
        con.close()

    def test_restart_running_profile(self):
        fake = FakeThrone(self.KEY, [
            {"status": 200, "body": {"state": "running", "profile": {"id": 12, "name": "DE"}}},
            {"status": 200, "body": {"state": "running"}},
        ])
        fake.start()
        self.set_api("true", fake.port, self.KEY_TEXT)
        r = api.restart_connection(self.db)
        fake.join(5)
        self.assertEqual(r["restarted"], True)
        self.assertEqual(fake.requests[0], {"method": "GET", "path": "/v1/status"})
        self.assertEqual(fake.requests[1]["method"], "PUT")
        self.assertEqual(fake.requests[1]["path"], "/v1/connection")
        self.assertEqual(fake.requests[1]["body"]["profile"], 12)
        self.assertIs(fake.requests[1]["body"]["restart"], True)

    def test_nothing_running(self):
        fake = FakeThrone(self.KEY, [{"status": 200, "body": {"state": "idle", "profile": None}}])
        fake.start()
        self.set_api("true", fake.port, self.KEY_TEXT)
        self.assertEqual(api.restart_connection(self.db)["reason"], "not-running")
        self.assertEqual(len(fake.requests), 1)

    def test_api_error_is_raised_with_code(self):
        fake = FakeThrone(self.KEY, [
            {"status": 200, "body": {"state": "running", "profile": {"id": 1, "name": "x"}}},
            {"status": 422, "error": {"code": "tun_failed", "message": "The Tun interface could not be configured"}},
        ])
        fake.start()
        self.set_api("true", fake.port, self.KEY_TEXT)
        with self.assertRaises(api.ApiError) as cm:
            api.restart_connection(self.db)
        self.assertEqual((cm.exception.code, cm.exception.status), ("tun_failed", 422))

    def test_wrong_key(self):
        fake = FakeThrone(self.KEY, [{"status": 200, "body": {}}])
        fake.start()
        other = base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()
        self.set_api("true", fake.port, other)
        with self.assertRaises(api.ApiError) as cm:
            api.restart_connection(self.db)
        self.assertEqual(cm.exception.code, "auth")

    def test_api_disabled_or_missing(self):
        self.set_api("false", 9, self.KEY_TEXT)
        self.assertEqual(api.restart_connection(self.db)["reason"], "api-off")
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM settings WHERE key LIKE 'remote_api_%'")
        con.commit()
        con.close()
        self.assertEqual(api.restart_connection(self.db)["reason"], "api-off")  # Throne < 1.4

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens there now
        self.set_api("true", port, self.KEY_TEXT)
        with self.assertRaises(api.ApiError) as cm:
            api.restart_connection(self.db)
        self.assertEqual(cm.exception.code, "unreachable")


if __name__ == "__main__":
    unittest.main()
