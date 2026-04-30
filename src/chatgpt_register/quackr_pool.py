#!/usr/bin/env python3
"""
quackr_pool.py - quackr.io 临时号码池 + 短信轮询。

- 从 https://quackr.io/numbers.json 抓号码入 SQLite (./data.db)
- claim-lease 模式 pick: 选中只占位不计数, mark-used 才 use_count+=1
- 外部反馈: mark-used / mark-dead / release
- 调本地 Turnstile-Solver 解 CF 挑战, 拉 /api/messages 抓 OTP

CLI 子命令: refresh / pick / mark-used / mark-dead / release / wait-otp / list
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

from curl_cffi import requests as http

from . import paths as _paths

QUACKR_API_KEY = "AIzaSyAxiKk4HSdhNYtVAIA2MFGJ7o2IjNGmAm0"
QUACKR_TOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={QUACKR_API_KEY}"
QUACKR_NUMBERS_URL = "https://quackr.io/numbers.json"
QUACKR_MESSAGES_TPL = "https://quackr.io/api/messages/{number}"
QUACKR_TURNSTILE_SITEKEY = "0x4AAAAAACgyuLaLScvBJq8u"
QUACKR_PAGE_TPL = "https://quackr.io/temporary-numbers/{country}/{number}"
QUACKR_ROOT_PAGE = "https://quackr.io/temporary-numbers/"

LOCALE_TO_COUNTRY = {
    "at": "austria", "au": "australia", "be": "belgium", "br": "brazil",
    "cn": "china", "de": "germany", "es": "spain", "fi": "finland",
    "fr": "france", "hu": "hungary", "id": "indonesia", "ie": "ireland",
    "in": "india", "kr": "korea", "lt": "lithuania", "ma": "morocco",
    "mx": "mexico", "nl": "netherlands", "pk": "pakistan", "pl": "poland",
    "pt": "portugal", "rs": "serbia", "ru": "russia", "se": "sweden",
    "si": "slovenia", "sw": "switzerland", "th": "thailand",
    "uk": "united-kingdom", "us": "united-states", "za": "south-africa",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
IMPERSONATE = "chrome146"  # curl_cffi TLS 指纹: 模拟 chrome146, 防 CF 拦


# ============================================================
# Config / DB
# ============================================================

def load_config():
    try:
        with open(_paths.config_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def get_conn():
    conn = sqlite3.connect(_paths.database_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS quackr_numbers (
            number          TEXT PRIMARY KEY,
            locale          TEXT,
            provider        TEXT,
            added_at        INTEGER,
            first_seen      INTEGER,
            last_seen       INTEGER,
            last_status     TEXT,
            dead            INTEGER NOT NULL DEFAULT 0,
            dead_reason     TEXT,
            use_count       INTEGER NOT NULL DEFAULT 0,
            claimed_until   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_quackr_pick
            ON quackr_numbers(dead, last_status, claimed_until, use_count, last_seen);

        CREATE TABLE IF NOT EXISTS quackr_usages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            number    TEXT NOT NULL,
            platform  TEXT NOT NULL,
            success   INTEGER NOT NULL,
            used_at   INTEGER NOT NULL,
            note      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_quackr_usages_number
            ON quackr_usages(number);
    """)
    conn.commit()


# ============================================================
# Pool: scrape / refresh / pick / mark
# ============================================================

def scrape_numbers():
    r = http.get(QUACKR_NUMBERS_URL,
                 headers={"Accept": "application/json", "User-Agent": UA},
                 timeout=30, impersonate=IMPERSONATE)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "b" in data:
        data = data["b"]
    return data


def refresh_pool(conn, locale=None):
    rows = scrape_numbers()
    now = int(time.time())
    online_seen = inserted = updated = offline = 0
    for r in rows:
        num = r.get("number")
        if not num:
            continue
        if locale and r.get("locale") != locale:
            continue
        try:
            added_int = int(r.get("added"))
        except (TypeError, ValueError):
            added_int = None
        status = r.get("status")
        if status != "Online":
            offline += 1
            conn.execute(
                "UPDATE quackr_numbers SET last_status=?, last_seen=? WHERE number=?",
                (status, now, num)
            )
            continue
        online_seen += 1
        existing = conn.execute(
            "SELECT 1 FROM quackr_numbers WHERE number=?", (num,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE quackr_numbers SET locale=?, provider=?, added_at=?, "
                "last_seen=?, last_status='Online' WHERE number=?",
                (r.get("locale"), r.get("provider"), added_int, now, num)
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO quackr_numbers (number, locale, provider, added_at, "
                "first_seen, last_seen, last_status) VALUES (?,?,?,?,?,?, 'Online')",
                (num, r.get("locale"), r.get("provider"), added_int, now, now)
            )
            inserted += 1
    conn.commit()
    return {
        "total_seen": len(rows), "online_seen": online_seen,
        "inserted": inserted, "updated": updated, "offline_skipped": offline,
    }


def pick_number(conn, locale=None, max_use=1, claim_ttl=600):
    now = int(time.time())
    until = now + claim_ttl
    conn.execute("BEGIN IMMEDIATE")
    try:
        where = ("dead=0 AND last_status='Online' AND use_count<? "
                 "AND claimed_until<?")
        args = [max_use, now]
        if locale:
            where += " AND locale=?"
            args.append(locale)
        row = conn.execute(
            f"SELECT number, locale FROM quackr_numbers WHERE {where} "
            "ORDER BY use_count ASC, last_seen DESC LIMIT 1",
            args
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            "UPDATE quackr_numbers SET claimed_until=? WHERE number=?",
            (until, row["number"])
        )
        conn.commit()
        return {"number": row["number"], "locale": row["locale"],
                "claimed_until": until}
    except Exception:
        conn.rollback()
        raise


def mark_used(conn, number, platform, success=True, note=None):
    now = int(time.time())
    conn.execute("BEGIN IMMEDIATE")
    try:
        if success:
            conn.execute(
                "UPDATE quackr_numbers SET use_count=use_count+1, "
                "claimed_until=0 WHERE number=?", (number,)
            )
        else:
            conn.execute(
                "UPDATE quackr_numbers SET claimed_until=0 WHERE number=?",
                (number,)
            )
        conn.execute(
            "INSERT INTO quackr_usages (number, platform, success, used_at, note) "
            "VALUES (?,?,?,?,?)",
            (number, platform, 1 if success else 0, now, note)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def mark_dead(conn, number, reason=None):
    conn.execute(
        "UPDATE quackr_numbers SET dead=1, dead_reason=?, claimed_until=0 "
        "WHERE number=?", (reason, number)
    )
    conn.commit()


def release(conn, number):
    conn.execute(
        "UPDATE quackr_numbers SET claimed_until=0 WHERE number=?", (number,)
    )
    conn.commit()


# ============================================================
# Firebase id_token
# ============================================================

def refresh_id_token(refresh_token):
    r = http.post(
        QUACKR_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://quackr.io",
            "Referer": "https://quackr.io/",
            "User-Agent": UA,
        },
        timeout=15, impersonate=IMPERSONATE,
    )
    r.raise_for_status()
    j = r.json()
    return j["access_token"], time.time() + int(j["expires_in"]) - 60


# ============================================================
# Turnstile (本地 D3vin solver)
# ============================================================

def solve_turnstile(solver_url, page_url, sitekey, timeout=120, proxy=None):
    """同步调用 sentinel_solver 的 POST /turnstile/token, 返回 token 字符串。"""
    base = solver_url.rstrip("/")
    body = {"url": page_url, "sitekey": sitekey}
    if proxy:
        body["proxy"] = proxy
    r = http.post(f"{base}/turnstile/token", json=body, timeout=timeout + 10,
                  impersonate=IMPERSONATE)
    if r.status_code != 200:
        try:
            err = r.json().get("error")
        except Exception:
            err = r.text[:200]
        raise RuntimeError(f"turnstile solver {r.status_code}: {err}")
    return r.json()["token"]


def page_url_for(number, locale):
    country = LOCALE_TO_COUNTRY.get(locale or "")
    if country:
        return QUACKR_PAGE_TPL.format(country=country, number=number)
    return QUACKR_ROOT_PAGE


# ============================================================
# Messages
# ============================================================

def fetch_messages(number, id_token, turnstile_token, page_url,
                   limit=20, time_filter_ms=86_400_000):
    url = QUACKR_MESSAGES_TPL.format(number=number)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {id_token}",
        "Origin": "https://quackr.io",
        "Referer": page_url,
        "User-Agent": UA,
        "x-turnstile-token": turnstile_token,
        "x-origin-url": page_url,
    }
    r = http.get(url, headers=headers,
                 params={"limit": limit, "timeFilter": time_filter_ms},
                 timeout=20, impersonate=IMPERSONATE)
    return r.status_code, r.text


def _extract_messages(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("messages", "data", "items", "result"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


def _msg_text(m):
    parts = []
    for k in ("body", "text", "message", "content", "sms", "msg"):
        v = m.get(k)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def _msg_id(m):
    for k in ("id", "_id", "messageId", "uuid"):
        v = m.get(k)
        if v is not None:
            return str(v)
    return json.dumps(m, sort_keys=True, default=str)


def wait_otp(number, locale, refresh_token, solver_url,
             regex=r"\b(\d{4,8})\b", timeout=180, poll_interval=5, log=print):
    """quackr 的 turnstile token 是一次性的, 每轮都得重新解。"""
    pat = re.compile(regex)
    page_url = page_url_for(number, locale)
    id_token, id_exp = refresh_id_token(refresh_token)
    seen = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if time.time() >= id_exp:
            id_token, id_exp = refresh_id_token(refresh_token)
        log(f"[wait-otp] solving turnstile via {solver_url} url={page_url}")
        try:
            ts_token = solve_turnstile(solver_url, page_url,
                                       QUACKR_TURNSTILE_SITEKEY)
        except Exception as e:
            log(f"[wait-otp] turnstile solve failed: {e}; retry in {poll_interval}s")
            time.sleep(poll_interval)
            continue
        status, body = fetch_messages(number, id_token, ts_token, page_url)
        log(f"[wait-otp] GET /api/messages/{number} -> {status} ({len(body)} bytes)")
        log(f"[wait-otp] body: {body}")
        if status == 401:
            id_token, id_exp = refresh_id_token(refresh_token)
            continue
        if status == 403:
            log(f"[wait-otp] 403, re-solve next loop")
            time.sleep(2)
            continue
        if status != 200:
            time.sleep(poll_interval)
            continue
        try:
            data = json.loads(body)
        except Exception:
            log(f"[wait-otp] non-JSON body, skip")
            time.sleep(poll_interval)
            continue
        msgs = _extract_messages(data)
        log(f"[wait-otp] parsed {len(msgs)} message(s)")
        for m in msgs:
            mid = _msg_id(m)
            if mid in seen:
                continue
            seen.add(mid)
            text = _msg_text(m)
            if not text:
                continue
            log(f"[wait-otp] msg: {text[:160]}")
            mt = pat.search(text)
            if mt:
                return mt.group(1) if mt.groups() else mt.group(0)
        time.sleep(poll_interval)
    return None


# ============================================================
# CLI
# ============================================================

def cmd_refresh(args, conn, cfg):
    stats = refresh_pool(conn, locale=args.locale)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_pick(args, conn, cfg):
    row = pick_number(conn, locale=args.locale, max_use=args.max_use,
                      claim_ttl=args.claim_ttl)
    if not row and args.auto_refresh:
        print("[*] pool empty, refreshing", file=sys.stderr)
        refresh_pool(conn, locale=args.locale)
        row = pick_number(conn, locale=args.locale, max_use=args.max_use,
                          claim_ttl=args.claim_ttl)
    if not row:
        sys.exit(2)
    if args.json:
        print(json.dumps(row))
    else:
        print(f"{row['number']}\t{row['locale']}")


def cmd_mark_used(args, conn, cfg):
    mark_used(conn, args.number, args.platform,
              success=not args.fail, note=args.note)
    print("ok")


def cmd_mark_dead(args, conn, cfg):
    mark_dead(conn, args.number, reason=args.reason)
    print("ok")


def cmd_release(args, conn, cfg):
    release(conn, args.number)
    print("ok")


def cmd_wait_otp(args, conn, cfg):
    refresh_token = args.refresh_token or cfg.get("quackr_refresh_token")
    solver_url = (args.solver_url or cfg.get("sentinel_solver_url")
                  or "http://127.0.0.1:5732")
    if not refresh_token:
        print("[!] missing quackr_refresh_token (set in config.json or "
              "--refresh-token)", file=sys.stderr)
        sys.exit(2)
    locale = args.locale
    if not locale:
        row = conn.execute(
            "SELECT locale FROM quackr_numbers WHERE number=?", (args.number,)
        ).fetchone()
        locale = row["locale"] if row else None
    code = wait_otp(args.number, locale, refresh_token, solver_url,
                    regex=args.regex, timeout=args.timeout,
                    poll_interval=args.interval,
                    log=lambda s: print(s, file=sys.stderr))
    if code:
        print(code)
    else:
        sys.exit(3)


def cmd_list(args, conn, cfg):
    sql = ("SELECT number, locale, last_status, dead, use_count, "
           "claimed_until, last_seen FROM quackr_numbers WHERE 1=1")
    params = []
    if args.locale:
        sql += " AND locale=?"
        params.append(args.locale)
    if args.available:
        now = int(time.time())
        sql += (" AND dead=0 AND last_status='Online' AND use_count<? "
                "AND claimed_until<?")
        params.extend([args.max_use, now])
    sql += " ORDER BY use_count ASC, last_seen DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    now = int(time.time())
    print(f"{'number':<16} {'loc':<3} {'status':<8} {'dead':<4} "
          f"{'used':<4} {'claim':<6} last_seen")
    for r in rows:
        cl = "yes" if r["claimed_until"] > now else "-"
        print(f"{r['number']:<16} {r['locale'] or '-':<3} "
              f"{r['last_status'] or '-':<8} {r['dead']:<4} "
              f"{r['use_count']:<4} {cl:<6} {r['last_seen']}")


# ============================================================
# SmsProvider 适配 (sms_provider.py)
# ============================================================

class QuackrProvider:
    """把上面的池操作适配到 sms_provider.SmsProvider 接口。

    注意: quackr 是免费公共号码池, "acquire" 仅 claim-lease 占位, 不冻结资金;
    release_no_sms == release(只放 claim, 号继续在池里); release_bad == mark_dead。
    """
    name = "quackr"

    def __init__(self, cfg):
        from .sms_provider import SmsProviderError
        self.cfg = cfg
        self.refresh_token = cfg.get("quackr_refresh_token") or ""
        self.solver_url = (cfg.get("sentinel_solver_url")
                           or "http://127.0.0.1:5732")
        if not self.refresh_token:
            raise SmsProviderError("quackr_refresh_token 未配置 (config.json)")
        self._conn = None

    def _db(self):
        if self._conn is None:
            self._conn = get_conn()
            init_db(self._conn)
        return self._conn

    def acquire(self, *, locale=None, max_use=1, claim_ttl=600,
                auto_refresh=True, **_):
        from .sms_provider import NoNumberAvailable, SmsSession
        conn = self._db()
        row = pick_number(conn, locale=locale, max_use=max_use,
                          claim_ttl=claim_ttl)
        if not row and auto_refresh:
            refresh_pool(conn, locale=locale)
            row = pick_number(conn, locale=locale, max_use=max_use,
                              claim_ttl=claim_ttl)
        if not row:
            raise NoNumberAvailable(
                f"quackr 池里没有可用号 (locale={locale}, max_use={max_use})"
            )
        return SmsSession(
            provider=self.name, number=row["number"], handle=row["number"],
            locale=row["locale"], cost=None,
            extra={"claimed_until": row["claimed_until"]},
        )

    def wait_otp(self, session, *, regex=r"\b(\d{4,8})\b", timeout=180,
                 poll_interval=5, log=print):
        return wait_otp(session.number, session.locale, self.refresh_token,
                        self.solver_url, regex=regex, timeout=timeout,
                        poll_interval=poll_interval, log=log)

    def release_ok(self, session):
        mark_used(self._db(), session.number,
                  platform=self.cfg.get("quackr_platform", "unknown"),
                  success=True)

    def release_no_sms(self, session):
        release(self._db(), session.number)

    def release_bad(self, session, reason=""):
        mark_dead(self._db(), session.number, reason=reason or None)

    # 与 HeroSmsProvider 对齐: 复用基类 acquire_with_retry 不强求, quackr 通常 1 次就够
    def acquire_with_retry(self, max_retries=3, *, wait_timeout=180,
                           poll_interval=5, regex=r"\b(\d{4,8})\b",
                           log=print, **acquire_kwargs):
        from .sms_provider import SmsProvider as _Base
        return _Base.acquire_with_retry(
            self, max_retries=max_retries, wait_timeout=wait_timeout,
            poll_interval=poll_interval, regex=regex, log=log,
            **acquire_kwargs,
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh", help="抓 numbers.json 入库")
    p.add_argument("--locale")

    p = sub.add_parser("pick", help="选号 (claim-lease)")
    p.add_argument("--locale")
    p.add_argument("--max-use", type=int, default=1)
    p.add_argument("--claim-ttl", type=int, default=600,
                   help="claim 自动过期时间(秒), 默认 600")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-auto-refresh", dest="auto_refresh",
                   action="store_false", default=True,
                   help="池空时不自动 refresh")

    p = sub.add_parser("mark-used", help="标记本次使用 (success → use_count+=1)")
    p.add_argument("number")
    p.add_argument("--platform", required=True)
    p.add_argument("--fail", action="store_true",
                   help="标记本次失败 (不增 use_count, 仅释放 claim)")
    p.add_argument("--note")

    p = sub.add_parser("mark-dead", help="标记号码不可用")
    p.add_argument("number")
    p.add_argument("--reason")

    p = sub.add_parser("release", help="手动释放 claim")
    p.add_argument("number")

    p = sub.add_parser("wait-otp", help="阻塞轮询验证码, 命中即 print 退出")
    p.add_argument("number")
    p.add_argument("--locale", help="缺省从 DB 查")
    p.add_argument("--regex", default=r"\b(\d{4,8})\b",
                   help="OTP 提取正则, 默认 4-8 位连续数字")
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--interval", type=int, default=5)
    p.add_argument("--refresh-token")
    p.add_argument("--solver-url")

    p = sub.add_parser("list", help="列出号码池状态")
    p.add_argument("--locale")
    p.add_argument("--available", action="store_true",
                   help="仅显示当前可 pick 的")
    p.add_argument("--max-use", type=int, default=1)
    p.add_argument("--limit", type=int, default=30)

    args = ap.parse_args()
    cfg = load_config()
    conn = get_conn()
    init_db(conn)
    handlers = {
        "refresh": cmd_refresh, "pick": cmd_pick,
        "mark-used": cmd_mark_used, "mark-dead": cmd_mark_dead,
        "release": cmd_release, "wait-otp": cmd_wait_otp, "list": cmd_list,
    }
    handlers[args.cmd](args, conn, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
