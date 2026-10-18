"""Find Throne processes and restart the proxy core (Windows, ctypes only).

Throne 1.2.x does not watch its DB, so after an edit the running sing-box config
is stale. Terminating ThroneCore.exe is what the tray item "Restart Proxy" does:
Throne notices the core exit, starts a new core and restarts the same profile,
rebuilding the config from the DB — without any dialog.

Throne stops auto-restarting if the core exits twice within 10 s, so restarts
are spaced at least MIN_RESTART_INTERVAL apart (state kept in a small JSON file).

LEGACY, only used with "restartMethod": "kill" in config.json (Throne without the
API). The core has no signal handler, so a killed core never closes its wintun
adapter, and in Tun mode the new core may fail with "configure tun interface:
Cannot create a file when that file already exists". Throne 1.4+ should use the
API restart (throne_api.restart_connection) instead.
"""

import ctypes
import json
import os
import time
from ctypes import wintypes

MIN_RESTART_INTERVAL = 11.5
TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.OpenProcess.restype = wintypes.HANDLE


def _image_path(pid):
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _k32.CloseHandle(h)


def find_processes(exe_name, throne_dir=None):
    """PIDs of exe_name, restricted to throne_dir when the image path is readable."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name.lower():
                path = _image_path(entry.th32ProcessID)
                if throne_dir is None or path is None or \
                        os.path.normcase(os.path.dirname(path)) == os.path.normcase(os.path.normpath(throne_dir)):
                    pids.append(entry.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return pids


def _terminate(pid):
    h = _k32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        ok = bool(_k32.TerminateProcess(h, 1))
        _k32.WaitForSingleObject(h, 3000)
        return ok
    finally:
        _k32.CloseHandle(h)


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def throne_status(throne_dir):
    return {
        "throneRunning": bool(find_processes("Throne.exe", throne_dir)),
        "coreRunning": bool(find_processes("ThroneCore.exe", throne_dir)),
    }


def restart_core(throne_dir, state_path, wait_new=8.0, sleep=time.sleep, now=time.time):
    """Restart the proxy core. Returns a status dict for the popup.

    restarted=False with reason: 'throne-off' (Throne closed: the change applies at
    its next start), 'core-off' (no proxy running: nothing to apply), 'denied'.
    """
    if not find_processes("Throne.exe", throne_dir):
        return {"restarted": False, "reason": "throne-off"}
    cores = find_processes("ThroneCore.exe", throne_dir)
    if not cores:
        return {"restarted": False, "reason": "core-off"}

    state = _read_state(state_path)
    wait = state.get("lastRestart", 0) + MIN_RESTART_INTERVAL - now()
    if wait > 0:
        sleep(min(wait, MIN_RESTART_INTERVAL))
        cores = find_processes("ThroneCore.exe", throne_dir)
        if not cores:
            return {"restarted": False, "reason": "core-off"}

    killed = [pid for pid in cores if _terminate(pid)]
    if not killed:
        return {"restarted": False, "reason": "denied"}
    state["lastRestart"] = now()
    _write_state(state_path, state)

    deadline = now() + wait_new
    while now() < deadline:
        new = [p for p in find_processes("ThroneCore.exe", throne_dir) if p not in killed]
        if new:
            return {"restarted": True, "waited": round(max(wait, 0), 1)}
        sleep(0.25)
    return {"restarted": True, "waited": round(max(wait, 0), 1), "warning": "Ядро ещё не поднялось — проверьте Throne"}
