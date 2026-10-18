"""Native Messaging host "com.throne.site_adder".

The browser starts this process for each message (runtime.sendNativeMessage),
writes one length-prefixed JSON request to stdin and reads one reply.

Requests:  {"cmd": "status"} | {"cmd": "list"} | {"cmd": "add", "value": "x.com"}
           {"cmd": "remove", "value": "x.com"} | {"cmd": "import", "values": [...]}
Replies:   {"ok": true, ...} or {"ok": false, "code": "...", "message": "..."}
"""

import json
import msvcrt
import os
import struct
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import throne_api  # noqa: E402
import throne_db  # noqa: E402
import throne_proc  # noqa: E402

VERSION = "1.0.1"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "ThroneSiteAdder")
MAX_IMPORT = 5000


def load_config():
    """config.json written by install.ps1:
    {"throneDir": "...", "restartMethod": "api" | "kill" | "none", "apiKey"?: "...", "apiPort"?: 9095}
    """
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise throne_db.ThroneError("config-error", f"Не прочитан {path}: {e}. Запустите install.ps1 заново")
    if not os.path.isdir(cfg.get("throneDir", "")):
        raise throne_db.ThroneError("config-error", f"Папка Throne не найдена: {cfg.get('throneDir')}")
    return cfg


def apply_change(cfg, db_path, state_path):
    """Make the running Throne pick up the new rules."""
    throne_dir = cfg["throneDir"]
    if not throne_proc.find_processes("Throne.exe", throne_dir):
        return {"restarted": False, "reason": "throne-off"}
    method = cfg.get("restartMethod", "api")
    if method == "none":
        return {"restarted": False, "reason": "manual"}
    if method == "kill":
        return throne_proc.restart_core(throne_dir, state_path)
    try:
        return throne_api.restart_connection(db_path, cfg.get("apiKey"), cfg.get("apiPort"))
    except throne_api.ApiError as e:
        return {"restarted": False, "via": "api", "reason": e.code, "message": e.message}
    except (OSError, ValueError) as e:
        return {"restarted": False, "via": "api", "reason": "error", "message": str(e)}


def log(text, data_dir=DATA_DIR):
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "host.log")
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n")
    except OSError:
        pass


class FileLock:
    """Serializes concurrent host processes (e.g. import + add) on this machine."""

    def __init__(self, path, timeout=40.0):
        self.path, self.timeout, self.f = path, timeout, None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a+b")
        deadline = time.time() + self.timeout
        while True:
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.time() > deadline:
                    raise throne_db.ThroneError("busy", "Другая операция ещё выполняется, попробуйте позже")
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.f.close()


def handle(request, cfg=None, data_dir=DATA_DIR):
    if not isinstance(request, dict):
        raise throne_db.ThroneError("invalid", "Некорректный запрос")
    cmd = request.get("cmd")
    cfg = cfg or load_config()
    throne_dir = cfg["throneDir"]
    db_path = os.path.join(throne_dir, "config", "throne.db")
    backup_dir = os.path.join(data_dir, "backups")
    state_path = os.path.join(data_dir, "state.json")

    if cmd == "status":
        info = throne_db.list_entries(db_path)
        info.update(throne_proc.throne_status(throne_dir), version=VERSION, throneDir=throne_dir,
                    restartMethod=cfg.get("restartMethod", "api"))
        try:
            enabled, port, key = throne_api.settings_from_db(db_path)
            info["api"] = {"enabled": bool(enabled or cfg.get("apiKey")), "supported": enabled is not None,
                           "port": cfg.get("apiPort") or port or throne_api.DEFAULT_PORT}
        except Exception:  # noqa: BLE001 — informational only
            info["api"] = {"enabled": False, "supported": False}
        return info
    if cmd == "list":
        info = throne_db.list_entries(db_path)
        info.update(throne_proc.throne_status(throne_dir))
        return info
    if cmd in ("add", "remove", "import"):
        if cmd == "import":
            values = request.get("values")
            if not isinstance(values, list) or len(values) > MAX_IMPORT:
                raise throne_db.ThroneError("invalid", f"Ожидался список до {MAX_IMPORT} записей")
            add, remove = values, []
        else:
            value = request.get("value")
            add, remove = ([value], []) if cmd == "add" else ([], [value])
        with FileLock(os.path.join(data_dir, "host.lock")):
            result = throne_db.modify(db_path, backup_dir, add=add, remove=remove)
            if result["changed"]:
                log(f"{cmd}: +{result['added']} -{result['removed']} profile={result['profile']['name']} "
                    f"backup={result['backup']}", data_dir)
                result["restart"] = apply_change(cfg, db_path, state_path)
                log(f"restart: {result['restart']}", data_dir)
            else:
                result["restart"] = {"restarted": False, "reason": "unchanged"}
                log(f"{cmd}: no change (exists={result['exists']} covered={result['covered']} "
                    f"missing={result['missing']})", data_dir)
        result.update(throne_proc.throne_status(throne_dir))
        return result
    raise throne_db.ThroneError("invalid", f"Неизвестная команда: {cmd!r}")


def read_message(stream):
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > 4 * 1024 * 1024:
        raise throne_db.ThroneError("invalid", "Слишком большое сообщение")
    return json.loads(stream.read(length).decode("utf-8"))


def write_message(stream, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


def main():
    for stream in (sys.stdin, sys.stdout):
        msvcrt.setmode(stream.fileno(), os.O_BINARY)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        request = read_message(stdin)
        if request is None:
            return
        brief = {k: (v if k != "values" else f"<{len(v)} values>") for k, v in request.items()}             if isinstance(request, dict) else request
        log(f"request {json.dumps(brief, ensure_ascii=False)} from {sys.argv[1] if len(sys.argv) > 1 else '?'}")
        reply = {"ok": True, **handle(request)}
    except throne_db.ThroneError as e:
        reply = {"ok": False, "code": e.code, "message": e.message}
        log(f"error {e.code}: {e.message}")
    except Exception as e:  # never leave the popup without an answer
        log("crash: " + traceback.format_exc())
        reply = {"ok": False, "code": "internal", "message": f"Внутренняя ошибка host: {e}"}
    write_message(stdout, reply)


if __name__ == "__main__":
    main()
