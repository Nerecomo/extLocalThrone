"""Work with the Throne 1.2.x routing database (config/throne.db, SQLite in WAL mode).

The active routing profile is settings.current_route_id. Domains go to the
domain_suffix list of the first "route -> proxy" rule of that profile; subnets
and IPs go to ip_cidr of the same rule.

Writes are done inside one short IMMEDIATE transaction: SQLite itself makes them
atomic and crash-safe, even if Throne or this process dies in the middle. A
temp-file + rename would be wrong here: Throne keeps the DB and its WAL open.
"""

import ipaddress
import json
import os
import re
import sqlite3
import time

PROXY_OUTBOUND_ID = -1
SIMPLE_ADDRESS_PROXY = 1   # RouteRule::ruleType::simpleAddressProxy
CUSTOM_RULE = 0
BUSY_TIMEOUT_MS = 5000
KEEP_BACKUPS = 30

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class ThroneError(Exception):
    """An error the popup can show; code is a stable machine-readable id."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_entry(raw):
    """Validate user input. Returns (kind, value) with kind 'domain' or 'ip'.

    Accepted: example.com, *.example.com (stored as .example.com = subdomains only),
    IPv4/IPv6 addresses and subnets (10.0.0.0/8, 2001:db8::/32).
    """
    if not isinstance(raw, str):
        raise ThroneError("invalid", "Ожидалась строка")
    s = raw.strip().lower().rstrip(".")
    if not s or len(s) > 253:
        raise ThroneError("invalid", "Пустое или слишком длинное значение")

    # Anything with a colon, or made only of digits/dots/slash, must be an IP or a subnet.
    if ":" in s or re.fullmatch(r"[\d./]+", s):
        try:
            if "/" in s:
                return "ip", str(ipaddress.ip_network(s, strict=False))
            return "ip", str(ipaddress.ip_address(s))
        except ValueError:
            raise ThroneError("invalid", f"Некорректный IP-адрес или подсеть: {raw.strip()}")

    wildcard = False
    if s.startswith("*."):
        wildcard, s = True, s[2:]
    elif s.startswith("."):
        wildcard, s = True, s[1:]
    if not re.fullmatch(r"[a-z0-9.-]+", s):
        raise ThroneError("invalid", "Допустимы только латиница, цифры, точка и дефис (a-z 0-9 . -)")
    labels = s.split(".")
    if len(labels) < 2 or not all(_LABEL.match(l) for l in labels) or labels[-1].isdigit():
        raise ThroneError("invalid", f"Некорректный домен: {raw.strip()}")
    return "domain", ("." + s) if wildcard else s


def display(kind, value):
    return "*" + value if kind == "domain" and value.startswith(".") else value


def _covered_by(domain, suffixes):
    """Existing suffix entry that already makes `domain` go through the proxy."""
    bare = domain.lstrip(".")
    for sfx in suffixes:
        if sfx.startswith("."):
            if bare.endswith(sfx):
                return sfx
        elif bare == sfx or bare.endswith("." + sfx):
            return sfx
    return None


def connect(db_path):
    if not os.path.isfile(db_path):
        raise ThroneError("config-error", f"Не найден файл конфига Throne: {db_path}")
    try:
        con = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error as e:
        raise ThroneError("config-error", f"Не удалось открыть конфиг: {e}")


def _check_schema(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"settings", "route_profiles", "route_rules"} - tables
    if missing:
        raise ThroneError("config-error", "Неподдерживаемая версия Throne: нет таблиц " + ", ".join(sorted(missing)))


def active_profile(con):
    _check_schema(con)
    row = con.execute("SELECT value FROM settings WHERE key='current_route_id'").fetchone()
    if row is None:
        raise ThroneError("config-error", "В настройках Throne нет активного профиля маршрутизации")
    try:
        pid = int(row[0])
    except ValueError:
        raise ThroneError("config-error", f"Странное значение current_route_id: {row[0]!r}")
    prof = con.execute("SELECT * FROM route_profiles WHERE id=?", (pid,)).fetchone()
    if prof is None:
        raise ThroneError("config-error", f"Активный профиль маршрутизации #{pid} не найден")
    return prof


def _profile_info(prof):
    keys = prof.keys()
    return {
        "id": prof["id"],
        "name": prof["name"],
        "remote": bool(prof["is_remote"]) if "is_remote" in keys else False,
    }


def _find_proxy_rule(con, profile_id):
    rows = con.execute(
        "SELECT * FROM route_rules WHERE route_profile_id=? AND action='route' AND outbound_id=? "
        "AND invert=0 ORDER BY rule_order", (profile_id, PROXY_OUTBOUND_ID)).fetchall()
    for want in (SIMPLE_ADDRESS_PROXY, CUSTOM_RULE):
        for r in rows:
            if r["type"] == want:
                return r
    return None


def _create_proxy_rule(con, profile_id):
    order = con.execute("SELECT COALESCE(MAX(rule_order), -1) + 1 FROM route_rules WHERE route_profile_id=?",
                        (profile_id,)).fetchone()[0]
    cols = {r[1] for r in con.execute("PRAGMA table_info(route_rules)")}
    values = {
        "route_profile_id": profile_id, "rule_order": order, "name": "Simple Address Proxy",
        "type": SIMPLE_ADDRESS_PROXY, "outbound_id": PROXY_OUTBOUND_ID, "action": "route",
    }
    for c in cols:
        if c.endswith("_json"):
            values.setdefault(c, "[]")
        elif c in ("ip_version", "network", "protocol", "reject_method", "override_address", "override_port", "strategy"):
            values.setdefault(c, "")
    names = [c for c in values if c in cols]
    con.execute(f"INSERT INTO route_rules ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [values[c] for c in names])
    return con.execute("SELECT * FROM route_rules WHERE route_profile_id=? AND rule_order=?",
                       (profile_id, order)).fetchone()


def _load_list(text):
    try:
        data = json.loads(text or "[]")
        return [str(x) for x in data] if isinstance(data, list) else []
    except ValueError:
        return []


def _dump_list(items):
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _entries(rule):
    if rule is None:
        return []
    return ([display("domain", d) for d in _load_list(rule["domain_suffix_json"])]
            + _load_list(rule["ip_cidr_json"]))


def list_entries(db_path):
    con = connect(db_path)
    try:
        prof = active_profile(con)
        return {"profile": _profile_info(prof), "entries": _entries(_find_proxy_rule(con, prof["id"]))}
    except sqlite3.Error as e:
        raise ThroneError("config-error", f"Ошибка чтения конфига: {e}")
    finally:
        con.close()


def backup(db_path, backup_dir, keep=KEEP_BACKUPS):
    """Consistent online copy of the DB (includes WAL contents) via the SQLite backup API."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"throne-{stamp}.db")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(backup_dir, f"throne-{stamp}-{n}.db")
    src = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    old = sorted(f for f in os.listdir(backup_dir) if f.startswith("throne-") and f.endswith(".db"))
    for f in old[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(backup_dir, f))
        except OSError:
            pass
    return dest


def _apply(rule, to_add, to_remove):
    """Pure list editing. Returns (suffixes, cidrs, report)."""
    suffixes = _load_list(rule["domain_suffix_json"]) if rule is not None else []
    cidrs = _load_list(rule["ip_cidr_json"]) if rule is not None else []
    report = {"added": [], "exists": [], "covered": {}, "removed": [], "missing": []}
    for kind, value in to_remove:
        lst = suffixes if kind == "domain" else cidrs
        if value in lst:
            lst[:] = [x for x in lst if x != value]
            report["removed"].append(display(kind, value))
        else:
            report["missing"].append(display(kind, value))
    for kind, value in to_add:
        shown = display(kind, value)
        lst = suffixes if kind == "domain" else cidrs
        if value in lst:
            report["exists"].append(shown)
            continue
        if kind == "domain":
            cover = _covered_by(value, suffixes)
            if cover:
                report["covered"][shown] = display("domain", cover)
                continue
        lst.append(value)  # the end of the list, as required
        report["added"].append(shown)
    return suffixes, cidrs, report


def modify(db_path, backup_dir, add=(), remove=()):
    """Add and/or remove entries of the active profile.

    Invalid input raises ThroneError before anything is touched. When something
    changes, a backup is taken first, then the change is recomputed and written
    inside one IMMEDIATE transaction (so a concurrent writer cannot be lost).
    Returns the report plus "profile", "entries", "changed", "backup".
    """
    to_add = [normalize_entry(x) for x in add]
    to_remove = [normalize_entry(x) for x in remove]

    con = connect(db_path)
    try:
        prof = active_profile(con)
        if "is_raw" in prof.keys() and prof["is_raw"]:
            raise ThroneError("config-error",
                              f"Профиль «{prof['name']}» задан сырым JSON (raw), его правила нельзя менять отсюда")
        *_, report = _apply(_find_proxy_rule(con, prof["id"]), to_add, to_remove)
        backup_path = None
        if report["added"] or report["removed"]:
            backup_path = backup(db_path, backup_dir)

        con.execute("BEGIN IMMEDIATE")
        try:
            rule = _find_proxy_rule(con, prof["id"])
            suffixes, cidrs, report = _apply(rule, to_add, to_remove)
            changed = bool(report["added"] or report["removed"])
            if changed:
                if backup_path is None:  # the DB changed between the two passes
                    con.execute("ROLLBACK")
                    raise ThroneError("config-error", "Конфиг Throne изменился во время записи, повторите попытку")
                if rule is None:
                    rule = _create_proxy_rule(con, prof["id"])
                con.execute("UPDATE route_rules SET domain_suffix_json=?, ip_cidr_json=? "
                            "WHERE route_profile_id=? AND rule_order=?",
                            (_dump_list(suffixes), _dump_list(cidrs), prof["id"], rule["rule_order"]))
                con.execute("UPDATE route_profiles SET updated_at=strftime('%s','now') WHERE id=?", (prof["id"],))
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        report.update(profile=_profile_info(prof), changed=changed, backup=backup_path,
                      entries=[display("domain", d) for d in suffixes] + cidrs)
        return report
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "locked" in msg or "busy" in msg:
            raise ThroneError("config-error", "Конфиг Throne занят (database is locked), повторите через пару секунд")
        raise ThroneError("config-error", f"Ошибка записи конфига: {msg}")
    except sqlite3.Error as e:
        raise ThroneError("config-error", f"Ошибка конфига: {e}")
    finally:
        con.close()
