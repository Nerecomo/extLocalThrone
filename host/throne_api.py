"""Minimal client for the Throne API, protocol throne-api/1 (Throne 1.4.0-beta.2+).

Spec: https://github.com/throneproj/Throne-api/blob/main/spec/protocol.md
Pure standard library: ChaCha20-Poly1305 (RFC 8439) is implemented here; messages
are tiny, so speed does not matter. Checked against the RFC and the protocol's
official test vectors in tests/.

The connection settings are read from Throne's own DB (settings remote_api_enable,
remote_api_port, remote_api_key), so the user only has to switch the API on.
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import struct

VERSION_BYTE = b"\x01"
LABEL_C2S = b"throne-api/1 c2s"
LABEL_S2C = b"throne-api/1 s2c"
MAX_S2C = 64 * 1024 * 1024
MAX_C2S = 1024 * 1024
DEFAULT_PORT = 9095

# ---------------------------------------------------------------- ChaCha20-Poly1305

_M32 = 0xFFFFFFFF


def _rotl(v, c):
    return ((v << c) & _M32) | (v >> (32 - c))


def _qr(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _M32; s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & _M32; s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & _M32; s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & _M32; s[b] = _rotl(s[b] ^ s[c], 7)


def chacha20_block(key, counter, nonce):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574,
             *struct.unpack("<8I", key), counter & _M32, *struct.unpack("<3I", nonce)]
    w = list(state)
    for _ in range(10):
        _qr(w, 0, 4, 8, 12); _qr(w, 1, 5, 9, 13); _qr(w, 2, 6, 10, 14); _qr(w, 3, 7, 11, 15)
        _qr(w, 0, 5, 10, 15); _qr(w, 1, 6, 11, 12); _qr(w, 2, 7, 8, 13); _qr(w, 3, 4, 9, 14)
    return struct.pack("<16I", *((x + y) & _M32 for x, y in zip(w, state)))


def chacha20_xor(key, counter, nonce, data):
    out = bytearray()
    for i in range(0, len(data), 64):
        block = chacha20_block(key, counter + i // 64, nonce)
        out += bytes(a ^ b for a, b in zip(data[i:i + 64], block))
    return bytes(out)


def poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        n = int.from_bytes(msg[i:i + 16] + b"\x01", "little")
        acc = ((acc + n) * r) % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(b):
    return b"\x00" * (-len(b) % 16)


def _tag(key, nonce, aad, ct):
    otk = chacha20_block(key, 0, nonce)[:32]
    mac = aad + _pad16(aad) + ct + _pad16(ct) + struct.pack("<QQ", len(aad), len(ct))
    return poly1305(otk, mac)


def aead_seal(key, nonce, aad, plaintext):
    ct = chacha20_xor(key, 1, nonce, plaintext)
    return ct + _tag(key, nonce, aad, ct)


def aead_open(key, nonce, aad, sealed):
    if len(sealed) < 16:
        raise ValueError("frame too short")
    ct, tag = sealed[:-16], sealed[-16:]
    if not hmac.compare_digest(_tag(key, nonce, aad, ct), tag):
        raise ValueError("authentication failed")
    return chacha20_xor(key, 1, nonce, ct)

# ---------------------------------------------------------------- protocol


def key_from_text(text):
    text = (text or "").strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32 or "=" in text:
        raise ApiError("bad_key", "Ключ Throne API должен быть 43 символа base64url (32 байта)")
    return key


def derive(key, server_hello, client_hello):
    c = client_hello[1:] if len(client_hello) == 17 else client_hello
    k_c2s = hmac.new(key, LABEL_C2S + server_hello + c, hashlib.sha256).digest()
    k_s2c = hmac.new(key, LABEL_S2C + server_hello + c, hashlib.sha256).digest()
    return k_c2s, k_s2c


def _nonce(counter):
    return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)


def seal_frame(key, counter, plaintext):
    length = struct.pack(">I", len(plaintext) + 16)
    return length + aead_seal(key, _nonce(counter), length, plaintext)


def open_frame(key, counter, wire, limit=MAX_S2C):
    if len(wire) < 4:
        raise ValueError("frame too short")
    (n,) = struct.unpack(">I", wire[:4])
    if n < 16 or n > limit or len(wire) != 4 + n:
        raise ValueError("bad frame length")
    return aead_open(key, _nonce(counter), wire[:4], wire[4:])


class ApiError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Client:
    """One connection; requests and responses alternate."""

    def __init__(self, key, host="127.0.0.1", port=DEFAULT_PORT, timeout=10.0):
        self.key, self.host, self.port, self.timeout = key, host, port, timeout
        self.sock = None

    def __enter__(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ApiError("unreachable", f"Throne API не отвечает на {self.host}:{self.port} ({e})")
        c = os.urandom(16)
        self.sock.sendall(VERSION_BYTE + c)
        s = self._recv(16)
        self.k_c2s, self.k_s2c = derive(self.key, s, c)
        self.n_c2s = self.n_s2c = 0
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                raise ApiError("timeout", "Throne API не ответил вовремя")
            except OSError as e:
                raise ApiError("unreachable", f"Соединение с Throne API прервано ({e})")
            if not chunk:
                # Throne closes silently on a wrong key (authentication failure).
                raise ApiError("auth", "Throne API закрыл соединение — вероятно, неверный ключ")
            buf += chunk
        return buf

    def request(self, method, path, body=None, timeout=None):
        msg = {"method": method, "path": path}
        if body is not None:
            msg["body"] = body
        data = json.dumps(msg, separators=(",", ":")).encode()
        self.sock.sendall(seal_frame(self.k_c2s, self.n_c2s, data))
        self.n_c2s += 1
        if timeout:
            self.sock.settimeout(timeout)
        head = self._recv(4)
        (n,) = struct.unpack(">I", head)
        if n < 16 or n > MAX_S2C:
            raise ApiError("protocol", "Некорректный ответ Throne API")
        try:
            plain = open_frame(self.k_s2c, self.n_s2c, head + self._recv(n))
        except ValueError:
            raise ApiError("auth", "Ответ Throne API не прошёл проверку подлинности")
        self.n_s2c += 1
        reply = json.loads(plain.decode("utf-8"))
        status = reply.get("status", 0)
        if 200 <= status < 300:
            return status, reply.get("body")
        err = reply.get("error") or {}
        raise ApiError(err.get("code", "error"), err.get("message", f"HTTP {status}"), status)

# ---------------------------------------------------------------- settings from Throne


def settings_from_db(db_path):
    """(enabled, port, key_text) from Throne's settings table; None values when absent."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = dict(con.execute("SELECT key, value FROM settings WHERE key IN "
                                "('remote_api_enable','remote_api_port','remote_api_key')").fetchall())
    finally:
        con.close()
    enabled = rows.get("remote_api_enable")
    port = rows.get("remote_api_port")
    return (None if enabled is None else enabled.lower() == "true",
            int(port) if port and port.lstrip("-").isdigit() else None,
            rows.get("remote_api_key"))


def restart_connection(db_path, override_key=None, override_port=None, wait_timeout=60):
    """Restart the running profile through the API so the new rules take effect.

    Returns {"restarted": bool, "via": "api", "reason"?: str, "message"?: str}.
    reason: 'api-off' (API disabled or not supported by this Throne), 'not-running'
    (no profile runs: nothing to apply), or an API error code.
    """
    enabled, port, key_text = settings_from_db(db_path)
    if override_key:
        key_text = override_key
    if enabled is False and not override_key:
        return {"restarted": False, "via": "api", "reason": "api-off"}
    if enabled is None and not key_text:
        return {"restarted": False, "via": "api", "reason": "api-off"}
    if not key_text:
        return {"restarted": False, "via": "api", "reason": "api-off",
                "message": "В Throne нет ключа API — включите API в настройках"}
    key = key_from_text(key_text)
    with Client(key, port=override_port or port or DEFAULT_PORT) as api:
        _, status = api.request("GET", "/v1/status")
        profile = (status or {}).get("profile")
        if (status or {}).get("state") not in ("running", "connecting") or not profile:
            return {"restarted": False, "via": "api", "reason": "not-running"}
        _, after = api.request("PUT", "/v1/connection",
                               {"profile": profile["id"], "restart": True, "wait": True,
                                "timeout": wait_timeout}, timeout=wait_timeout + 10)
        return {"restarted": True, "via": "api", "state": (after or {}).get("state"),
                "profile": profile.get("name")}
