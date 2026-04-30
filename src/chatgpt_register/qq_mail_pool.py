#!/usr/bin/env python3

"""
自有域名 catch-all 邮箱池 (IMAP 邮箱收信)

适用场景:
    自有域名通过 Cloudflare Email Routing (catch-all) 把 *@yourdomain.com
    全部转发到 QQ 邮箱。本模块提供:
    1. 后台 IMAP 轮询线程, 持续抓 QQ 收件箱新邮件
    2. 按"原始收件人"(Cloudflare 转发后保留在 To: header) 分桶
    3. 像真人的随机邮箱名生成 (first.last / firstlast42 / j.smith / john_smith ...)
    4. 注册流程按 address 拉取最新 OTP, 不串号

Cloudflare Email Routing 行为说明:
    - 原始 To: header 保留为 user@yourdomain.com (我们靠这个分桶)
    - Cloudflare 会附加 X-Forwarded-To 表示真实投递地址 (QQ)
    - From / Subject / 正文保持原样, 编码也不动

IMAP:
    - 默认 imap.qq.com:993 SSL
    - 也支持其它标准 IMAP SSL 邮箱服务器
    - 用户名通常为完整邮箱地址
    - 密码可以是邮箱密码, 也可以是服务商要求的授权码
"""

import base64
import imaplib
import email
import select
import socket
import threading
import time
import re
import random
import ssl
from typing import Callable, Optional
from email.header import decode_header
from email.utils import parsedate_to_datetime

from .log_config import DEFAULT_LOG_LEVEL, normalize_log_level, should_log
from . import paths as _paths


class _IdleUnsupportedError(RuntimeError):
    """IMAP server does not support IDLE and should fall back to polling."""


def _imap_utf7_encode(name: str) -> bytes:
    """RFC 3501 modified UTF-7 编码 mailbox 名 (兼容中文/特殊字符)"""
    res = []
    buf = []

    def flush():
        if buf:
            text = "".join(buf)
            b = text.encode("utf-16-be")
            enc = base64.b64encode(b).rstrip(b"=").decode("ascii").replace("/", ",")
            res.append("&" + enc + "-")
            buf.clear()

    for ch in name:
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            flush()
            if ch == "&":
                res.append("&-")
            else:
                res.append(ch)
        else:
            buf.append(ch)
    flush()
    # SELECT 时用引号包起来, 避免空格/特殊字符歧义
    return ('"' + "".join(res) + '"').encode("ascii")


# ---- 名字池 (常见英文 first/last) ----
# 取常见姓名, 让生成的邮箱看起来像真人

_FIRST_NAMES = [
    "james", "john", "robert", "michael", "william", "david", "richard", "joseph",
    "thomas", "charles", "christopher", "daniel", "matthew", "anthony", "donald",
    "mark", "paul", "steven", "andrew", "kenneth", "joshua", "kevin", "brian",
    "george", "edward", "ronald", "timothy", "jason", "jeffrey", "ryan", "jacob",
    "gary", "nicholas", "eric", "jonathan", "stephen", "larry", "justin", "scott",
    "brandon", "frank", "benjamin", "gregory", "samuel", "raymond", "patrick",
    "alexander", "jack", "dennis", "jerry", "tyler", "aaron", "henry", "douglas",
    "peter", "adam", "nathan", "zachary", "walter", "kyle", "harold", "carl",
    "jeremy", "keith", "roger", "gerald", "ethan", "arthur", "terry", "christian",
    "sean", "lawrence", "austin", "joe", "noah", "jesse", "albert", "bryan",
    "billy", "bruce", "willie", "jordan", "dylan", "alan", "ralph", "gabriel",
    "roy", "juan", "wayne", "eugene", "logan", "randy", "louis", "russell",
    "vincent", "philip", "bobby", "johnny", "bradley", "mary", "patricia", "jennifer",
    "linda", "elizabeth", "barbara", "susan", "jessica", "sarah", "karen", "lisa",
    "nancy", "betty", "sandra", "margaret", "ashley", "kimberly", "emily",
    "donna", "michelle", "carol", "amanda", "melissa", "deborah", "stephanie",
    "rebecca", "laura", "sharon", "cynthia", "kathleen", "amy", "shirley",
    "angela", "helen", "anna", "brenda", "pamela", "nicole", "samantha", "katherine",
    "christine", "debra", "rachel", "carolyn", "janet", "maria", "olivia", "heather",
    "diane", "julie", "joyce", "victoria", "kelly", "christina", "joan", "evelyn",
    "judith", "andrea", "hannah", "megan", "cheryl", "jacqueline", "sophia", "martha",
    "gloria", "teresa", "ann", "sara", "madison", "frances", "kathryn", "janice",
    "jean", "abigail", "alice", "julia", "judy", "grace", "denise", "amber",
    "doris", "marilyn", "danielle", "beverly", "isabella", "theresa", "diana",
    "natalie", "brittany", "charlotte", "marie", "kayla", "alexis", "lori", "tiffany",
    "tom", "tony", "max", "leo", "sam", "ben", "alex", "chris", "dan", "matt",
    "nick", "rob", "mike", "joe", "jake", "luke", "evan", "ian", "ivan", "owen",
]

_LAST_NAMES = [
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
    "rodriguez", "martinez", "hernandez", "lopez", "gonzalez", "wilson", "anderson",
    "thomas", "taylor", "moore", "jackson", "martin", "lee", "perez", "thompson",
    "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson", "walker",
    "young", "allen", "king", "wright", "scott", "torres", "nguyen", "hill", "flores",
    "green", "adams", "nelson", "baker", "hall", "rivera", "campbell", "mitchell",
    "carter", "roberts", "gomez", "phillips", "evans", "turner", "diaz", "parker",
    "cruz", "edwards", "collins", "reyes", "stewart", "morris", "morales", "murphy",
    "cook", "rogers", "gutierrez", "ortiz", "morgan", "cooper", "peterson", "bailey",
    "reed", "kelly", "howard", "ramos", "kim", "cox", "ward", "richardson", "watson",
    "brooks", "chavez", "wood", "james", "bennett", "gray", "mendoza", "ruiz",
    "hughes", "price", "alvarez", "castillo", "sanders", "patel", "myers", "long",
    "ross", "foster", "jimenez", "powell", "jenkins", "perry", "russell", "sullivan",
    "bell", "coleman", "butler", "henderson", "barnes", "gonzales", "fisher",
    "vasquez", "simmons", "romero", "jordan", "patterson", "alexander", "hamilton",
    "graham", "reynolds", "griffin", "wallace", "moreno", "west", "cole", "hayes",
    "bryant", "herrera", "gibson", "ellis", "tran", "medina", "aguilar", "stevens",
    "murray", "ford", "castro", "marshall", "owens", "harrison", "fernandez",
    "mcdonald", "woods", "washington", "kennedy", "wells", "vargas", "henry",
    "chen", "freeman", "webb", "tucker", "guzman", "burns", "crawford", "olson",
    "simpson", "porter", "hunter", "gordon", "mendez", "silva", "shaw", "snyder",
    "mason", "dixon", "munoz", "hunt", "hicks", "holmes", "palmer", "wagner",
    "black", "robertson", "boyd", "rose", "stone", "salazar", "fox", "warren",
    "mills", "meyer", "rice", "schmidt", "garza", "daniels", "ferguson", "nichols",
    "stephens", "soto", "weaver", "ryan", "gardner", "payne", "grant", "dunn",
]


_OTP_PATTERNS = [
    r"Verification code:?\s*(\d{6})",
    r"code is\s*(\d{6})",
    r"代码为[:：]?\s*(\d{6})",
    r"验证码[:：]?\s*(\d{6})",
    r">\s*(\d{6})\s*<",
    r"(?<![#&])\b(\d{6})\b",
]


def extract_otp(content: str):
    """从邮件正文提取 6 位 OTP, 兼容 OpenAI 各种话术"""
    if not content:
        return None
    for pattern in _OTP_PATTERNS:
        for code in re.findall(pattern, content, re.IGNORECASE):
            if code == "177010":  # 已知误判 (DuckMail 注释里就提过)
                continue
            return code
    return None


class QQMailPool:
    def __init__(self, host, port, user, authcode, domain,
                 poll_interval=4, debug=False, folder="INBOX",
                 security="auto",
                 log: Optional[Callable[[str], None]] = None,
                 log_level: Optional[str] = None):
        self.host = host
        self.port = int(port)
        self.user = user
        self.authcode = authcode
        self.domain = domain.lower().lstrip("@")
        self.poll_interval = max(1, int(poll_interval))
        self.debug = bool(debug)
        self.log_level = normalize_log_level(log_level, default="debug" if self.debug else DEFAULT_LOG_LEVEL)
        self.folder = folder or "INBOX"
        self.security = str(security or "auto").strip().lower() or "auto"
        self.log = log

        # 收件箱: address(lower) -> [{uid, ts, subject, from, body}, ...] (新→旧)
        self._inbox = {}
        self._inbox_lock = threading.Lock()

        self._used_locals = set()
        self._used_lock = threading.Lock()

        # 仅对注册地址打 INFO 收件日志, 避免无关 worker 的邮件污染日志通道
        self._watched_addresses = set()
        self._watched_lock = threading.Lock()

        self._last_uid = 0
        self._stop = threading.Event()
        self._thread = None
        self._started = False
        self._ready = threading.Event()
        self._idle_supported = None

    # ---- 生命周期 ----

    def start(self, wait_baseline=True, baseline_timeout=10):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, name="QQMailPool", daemon=True
        )
        self._thread.start()
        if wait_baseline:
            self._ready.wait(timeout=baseline_timeout)

    def stop(self):
        self._stop.set()

    # ---- IMAP 主循环 ----

    def _cleanup_imap(self, imap):
        if not imap:
            return
        try:
            imap.shutdown()
            return
        except Exception:
            pass
        try:
            if getattr(imap, "sock", None):
                imap.sock.close()
        except Exception:
            pass

    def _open_imap(self):
        ctx = ssl.create_default_context()
        if self.security == "ssl" or (self.security == "auto" and int(self.port) == 993):
            return imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx)
        imap = imaplib.IMAP4(self.host, self.port)
        if self.security in {"starttls", "auto"}:
            try:
                imap.starttls(ssl_context=ctx)
            except Exception:
                self._cleanup_imap(imap)
                raise
        return imap

    def _connect(self):
        imap = None
        try:
            imap = self._open_imap()
            imap.login(self.user, self.authcode)
            # QQ IMAP 登录后建议发送 ID 命令以避免被限流
            try:
                imap.xatom("ID", '("name" "QQMailPool" "version" "1.0")')
            except Exception:
                pass
            if self._idle_supported is not False:
                self._idle_supported = self._detect_idle_support(imap)
            return imap
        except Exception:
            self._cleanup_imap(imap)
            raise

    def _detect_idle_support(self, imap):
        try:
            typ, data = imap.capability()
        except Exception:
            return None
        if typ != "OK" or not data:
            return None
        caps = []
        for item in data:
            if isinstance(item, bytes):
                caps.append(item.upper())
            else:
                caps.append(str(item).encode("utf-8", errors="ignore").upper())
        merged = b" ".join(caps)
        supported = b"IDLE" in merged.split()
        if self.debug:
            self._log(
                f"CAPABILITY {'支持' if supported else '不支持'} IDLE: "
                f"{merged.decode('utf-8', errors='replace')[:200]}"
            )
        return supported

    def _select_folder(self, imap):
        """SELECT 配置的文件夹, 自动用 modified UTF-7 编码非 ASCII 名"""
        if all(0x20 <= ord(c) <= 0x7E for c in self.folder):
            mailbox = self.folder
        else:
            mailbox = _imap_utf7_encode(self.folder)
        typ, data = imap.select(mailbox)
        if typ != "OK":
            raise imaplib.IMAP4.error(
                f"SELECT {self.folder!r} 失败: {typ} {data!r}"
            )
        if self.debug:
            n = data[0].decode() if data and data[0] else "?"
            self._log(f"SELECT {self.folder!r} → 邮件数={n}")

    # IDLE 单次最长持续时间. 设为 30s 而不是 25min: 即使 QQ 偶尔不推 EXISTS 通知,
    # 我们也每 30s 主动 DONE → poll 一次, 不会久等. 30s 比 25min 安全得多.
    IDLE_MAX_SECONDS = 30
    # IDLE 期间 select 的 tick, 用于响应 self._stop
    IDLE_TICK_SECONDS = 5

    def _loop(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                imap = self._connect()
                self._select_folder(imap)
                # 设定 baseline: 只关心从此刻起的新邮件
                if self._last_uid == 0:
                    typ, data = imap.uid("SEARCH", None, "ALL")
                    if typ == "OK" and data and data[0]:
                        uids = data[0].split()
                        if uids:
                            # 数值最大的 UID, 不依赖服务器排序
                            self._last_uid = max(int(u) for u in uids)
                    self._log(f"baseline UID = {self._last_uid} (ready)")
                self._ready.set()
                backoff = 5

                while not self._stop.is_set():
                    # 1. 先把当前还没拉的新邮件抓完
                    self._poll_once(imap)
                    # 2. IDLE 长连接等推送; 若服务端不支持则回退到普通轮询
                    if self._idle_supported is False:
                        if self._stop.wait(self.poll_interval):
                            break
                        continue
                    try:
                        self._idle_wait(imap, self.IDLE_MAX_SECONDS)
                        if self._idle_supported is None:
                            self._idle_supported = True
                    except _IdleUnsupportedError:
                        self._idle_supported = False
                        self._log(f"IDLE 不支持, 回退到每 {self.poll_interval}s 轮询")
                        if self._stop.wait(self.poll_interval):
                            break
                    # 醒来后回到 1. 再 poll 一遍即可拿到新 UID
                try:
                    imap.logout()
                except Exception:
                    pass
            except Exception as e:
                self._log(f"IMAP 异常: {e}; {backoff}s 后重连")
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)

    def _idle_wait(self, imap, max_seconds):
        """进入 IMAP IDLE 等服务器推送 EXISTS, 拿到推送/超时/stop 后返回.
        通过直接读写 imaplib 内部 socket 实现 (stdlib 不原生支持 IDLE).
        """
        tag = imap._new_tag()
        try:
            imap.send(b"%s IDLE\r\n" % tag)
        except Exception as e:
            raise imaplib.IMAP4.abort(f"IDLE 发送失败: {e}")

        # 等服务端 "+ idling" 回应
        try:
            resp = imap.readline()
        except Exception as e:
            raise imaplib.IMAP4.abort(f"IDLE 等待 + 失败: {e}")
        if not resp.startswith(b"+"):
            low = resp.lower()
            if b"idle" in low and (b"not recognized" in low or b"unsupported" in low):
                raise _IdleUnsupportedError(resp.decode("utf-8", errors="replace"))
            self._log(f"IDLE 被拒: {resp!r}")
            return

        if self.debug:
            self._log(f"IDLE 进入 (最多 {max_seconds}s)")

        # SSL socket 不设 timeout (设了会进入"读一半 timeout"死锁状态),
        # 改用 select() 等可读信号. SSL 的 pending() 也要查一下.
        sock = imap.socket()
        deadline = time.time() + max_seconds
        new_mail_seen = False

        try:
            while True:
                if self._stop.is_set():
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    if self.debug:
                        self._log("IDLE 到期, 主动 DONE 重置")
                    break
                # SSL 缓冲里可能已有解密好的数据, 优先读
                pending = 0
                if isinstance(sock, ssl.SSLSocket):
                    try:
                        pending = sock.pending()
                    except Exception:
                        pending = 0
                if pending == 0:
                    tick = min(remaining, self.IDLE_TICK_SECONDS)
                    try:
                        ready, _, _ = select.select([sock], [], [], tick)
                    except (OSError, ValueError) as e:
                        raise imaplib.IMAP4.abort(f"IDLE select 失败: {e}")
                    if not ready:
                        continue  # 没数据, 回到顶部检查 stop / deadline
                # 这里 socket 一定有数据可读, readline 不会卡
                try:
                    line = imap.readline()
                except Exception as e:
                    raise imaplib.IMAP4.abort(f"IDLE 读取失败: {e}")
                if not line:
                    raise imaplib.IMAP4.abort("IDLE 连接被关闭")
                low = line.lower()
                if b"exists" in low or b"recent" in low:
                    if self.debug:
                        self._log(f"IDLE 推送: {line.strip()!r}")
                    new_mail_seen = True
                    break  # 退出 IDLE, 上层去 poll
                # 其他未带 tag 的响应 (FETCH/EXPUNGE 等) 忽略
        finally:
            # 无论如何都要发 DONE 退出 IDLE 状态
            try:
                imap.send(b"DONE\r\n")
            except Exception:
                pass
            # 排空到 tag OK 为止, 让 imaplib 状态机回到 SELECTED
            try:
                while True:
                    line = imap.readline()
                    if not line:
                        break
                    if line.startswith(tag):
                        break
            except Exception:
                pass
        return new_mail_seen

    def _poll_once(self, imap):
        next_uid = self._last_uid + 1
        try:
            typ, data = imap.uid("SEARCH", None, f"UID {next_uid}:*")
        except imaplib.IMAP4.abort:
            raise
        except Exception as e:
            self._log(f"SEARCH 失败: {e}")
            return
        if typ != "OK" or not data or not data[0]:
            self._log(f"SEARCH UID {next_uid}:* 无返回 (typ={typ})")
            return
        all_returned = data[0].split()
        uids = [u for u in all_returned if int(u) > self._last_uid]
        if self.debug:
            self._log(
                f"SEARCH UID {next_uid}:* → 返回 {len(all_returned)} 个 "
                f"(过滤后新邮件 {len(uids)} 个); last_uid={self._last_uid}"
            )
        if not uids:
            return
        for uid_b in uids:
            uid = -1
            try:
                uid = int(uid_b)
                typ, msg_data = imap.uid("FETCH", uid_b, "(RFC822)")
                if typ != "OK" or not msg_data:
                    self._log(f"FETCH UID={uid} 无数据 (typ={typ})")
                    continue
                raw = None
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) >= 2:
                        raw = part[1]
                        break
                if not raw:
                    self._log(f"FETCH UID={uid} 解析不到 RFC822 raw")
                    continue
                msg = email.message_from_bytes(raw)
                self._ingest(uid, msg)
            except Exception as e:
                self._log(f"FETCH UID {uid_b} 失败: {e}")
            finally:
                if uid > self._last_uid:
                    self._last_uid = uid

    def _ingest(self, uid, msg):
        recipient = self._extract_recipient(msg)
        recipient_lc = recipient.lower() if recipient else None
        with self._watched_lock:
            is_watched = recipient_lc in self._watched_addresses if recipient_lc else False
        if self.debug and (is_watched or not recipient_lc):
            hdrs = {}
            for h in ("To", "From", "Subject", "Delivered-To",
                      "X-Original-To", "X-Forwarded-To"):
                v = msg.get(h)
                if v:
                    hdrs[h] = self._decode_header(v)[:120]
            self._log(f"UID={uid} headers={hdrs}")
        if not recipient:
            self._log(f"UID={uid} 无法解析原始收件人, 丢弃")
            return
        recipient = recipient_lc
        subject = self._decode_header(msg.get("Subject", ""))
        sender = self._decode_header(msg.get("From", ""))
        body = self._extract_body(msg)
        ts = time.time()
        item = {
            "uid": uid, "ts": ts, "subject": subject,
            "from": sender, "body": body,
        }
        with self._inbox_lock:
            lst = self._inbox.setdefault(recipient, [])
            lst.insert(0, item)
            if len(lst) > 20:
                del lst[20:]
        if is_watched or self.debug:
            self._log(
                f"收件 UID={uid} → {recipient} | from={sender[:40]} | "
                f"subj={subject[:40]}"
            )

    def _extract_recipient(self, msg):
        """从邮件 header 找出原始收件人 (Cloudflare 保留 To:)"""
        # 优先级: Delivered-To 在转发链路里通常是最终目的, 而 To: 才是原始
        candidates = []
        for h in ("To", "X-Original-To", "Delivered-To", "X-Forwarded-To"):
            for v in msg.get_all(h) or []:
                candidates.append(v)
        for received in msg.get_all("Received") or []:
            m = re.search(r"for\s+<?([\w.+\-]+@[\w.\-]+)>?", received)
            if m:
                candidates.append(m.group(1))

        suffix = "@" + self.domain
        for v in candidates:
            addr = self._parse_email_addr(v)
            if addr and addr.lower().endswith(suffix):
                return addr
        # 找不到匹配域名的就返回第一个能解析的地址 (兜底)
        for v in candidates:
            addr = self._parse_email_addr(v)
            if addr:
                return addr
        return None

    @staticmethod
    def _parse_email_addr(header_value):
        if not header_value:
            return None
        m = re.search(r"([\w.+\-]+@[\w.\-]+)", header_value)
        return m.group(1) if m else None

    @staticmethod
    def _decode_header(s):
        if not s:
            return ""
        try:
            parts = decode_header(s)
        except Exception:
            return s
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                try:
                    out.append(text.decode(charset or "utf-8", errors="replace"))
                except Exception:
                    out.append(text.decode("utf-8", errors="replace"))
            else:
                out.append(text)
        return "".join(out)

    @staticmethod
    def _extract_body(msg):
        chunks = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype not in ("text/plain", "text/html"):
                    continue
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(charset, errors="replace"))
                except Exception:
                    pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(charset, errors="replace"))
            except Exception:
                pass
        return "\n".join(chunks)

    # ---- 对外 API ----

    def register_address(self, address):
        """声明 worker 关注的收件地址 → 该地址的接收事件会以 INFO 打印"""
        if not address:
            return
        with self._watched_lock:
            self._watched_addresses.add(address.lower())

    def unregister_address(self, address):
        if not address:
            return
        with self._watched_lock:
            self._watched_addresses.discard(address.lower())

    def acquire_email(self, domain=None, base_address=None):
        domain = (domain or self.domain).lower().lstrip("@")
        prefix_local = ""
        if base_address:
            parsed = self._parse_email_addr(base_address) or str(base_address).strip()
            if "@" in parsed:
                prefix_local, parsed_domain = parsed.split("@", 1)
                prefix_local = prefix_local.strip().lower()
                parsed_domain = parsed_domain.strip().lower().lstrip("@")
                if parsed_domain:
                    domain = parsed_domain
        addr = None
        for _ in range(80):
            local = self._build_candidate_local(prefix_local)
            with self._used_lock:
                if local in self._used_locals:
                    continue
                self._used_locals.add(local)
            addr = f"{local}@{domain}"
            break
        if not addr:
            # 极端情况: 加随机后缀
            local = self._build_candidate_local(prefix_local) + str(random.randint(100, 9999))
            with self._used_lock:
                self._used_locals.add(local)
            addr = f"{local}@{domain}"
        # 记录该地址 acquire 时刻, 防止抓到 INBOX 里偶然存在的同名旧邮件
        with self._inbox_lock:
            self._inbox.setdefault(addr.lower(), [])
        self.register_address(addr)
        return addr

    def release(self, address):
        if not address:
            return
        with self._inbox_lock:
            self._inbox.pop(address.lower(), None)
        self.unregister_address(address)

    def get_messages_since(self, address, since_ts=0.0):
        with self._inbox_lock:
            items = list(self._inbox.get(address.lower(), []))
        if since_ts:
            items = [x for x in items if x["ts"] >= since_ts]
        return items

    def wait_for_otp(self, address, timeout=120, since_ts=None,
                     exclude_codes=None, poll_interval=2):
        """阻塞等待 address 的最新 OTP, 失败返回 None"""
        self.register_address(address)
        deadline = time.time() + timeout
        exclude_codes = set(exclude_codes or [])
        addr_lc = address.lower()
        while time.time() < deadline:
            with self._inbox_lock:
                items = list(self._inbox.get(addr_lc, []))
            for item in items:
                if since_ts and item["ts"] < since_ts:
                    continue
                code = extract_otp(item["body"])
                if code and code not in exclude_codes:
                    return code
            time.sleep(poll_interval)
        return None

    # ---- 邮箱名生成 ----

    def _build_candidate_local(self, prefix_local=""):
        if not prefix_local:
            return self._random_human_local()
        return f"{prefix_local}{self._random_suffix_local()}"

    def _random_suffix_local(self):
        # 2925 这类“一邮多用”场景通常只接受字母/数字/下划线, 这里复用
        # 现有真人邮箱名生成器, 再把点号等字符收敛成下划线。
        local = re.sub(r"[^a-z0-9_]+", "_", self._random_human_local().lower()).strip("_")
        if local:
            return local
        return f"user{random.randint(100, 9999)}"

    def _random_human_local(self):
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        style = random.randint(0, 9)
        if style == 0:
            return f"{first}.{last}"
        if style == 1:
            return f"{first}{last}{random.randint(1, 99)}"
        if style == 2:
            return f"{first[0]}.{last}"
        if style == 3:
            return f"{first[0]}{last}"
        if style == 4:
            return f"{first}_{last}"
        if style == 5:
            return f"{first}{last}{random.randint(1985, 2005)}"
        if style == 6:
            return f"{first}.{last}{random.randint(1, 99)}"
        if style == 7:
            return f"{first}{last[0]}{random.randint(10, 999)}"
        if style == 8:
            return f"{first}_{last}{random.randint(1, 99)}"
        return f"{first}.{last[0]}{random.randint(10, 99)}"

    # ---- 日志 ----

    def _log(self, msg):
        if not should_log("debug", self.log_level):
            return
        line = f"[QQMailPool] {msg}"
        if self.log:
            try:
                self.log(line, level="debug")
                return
            except TypeError:
                self.log(line)
                return
        print(line)


# ---- 池缓存 ----

_pool_instances = {}
_pool_init_lock = threading.Lock()


def _resolve_cli_imap_config(config, selected_key=""):
    """兼容新旧配置结构，给 qq_mail_pool.py 独立 CLI 使用。"""
    legacy = {
        "qq_imap_host": config.get("qq_imap_host") or config.get("mail_imap_host") or "",
        "qq_imap_port": int(config.get("qq_imap_port") or config.get("mail_imap_port") or 993),
        "qq_imap_user": config.get("qq_imap_user") or config.get("mail_imap_user") or "",
        "qq_imap_authcode": (
            config.get("qq_imap_authcode")
            or config.get("mail_imap_authcode")
            or config.get("mail_imap_password")
            or ""
        ),
        "qq_imap_folder": config.get("qq_imap_folder") or config.get("mail_imap_folder") or "INBOX",
        "qq_imap_security": config.get("qq_imap_security") or config.get("mail_imap_security") or "auto",
        "mail_domain": config.get("mail_domain") or "",
    }
    if legacy["qq_imap_host"] and legacy["qq_imap_user"] and legacy["qq_imap_authcode"]:
        return legacy

    profiles = config.get("imap_profiles") or []
    profiles_by_key = {
        str(item.get("key") or item.get("id") or "").strip(): item
        for item in profiles if isinstance(item, dict)
    }
    email_sources = config.get("email_sources") or []
    default_source_key = str(config.get("default_email_source") or "").strip()
    source = None
    requested_key = str(selected_key or "").strip()
    if requested_key:
        for item in email_sources:
            if isinstance(item, dict) and str(item.get("key") or item.get("id") or "").strip() == requested_key:
                source = item
                break
        if not source and requested_key in profiles_by_key:
            profile = profiles_by_key.get(requested_key)
            return {
                "qq_imap_host": str(profile.get("host") or "").strip(),
                "qq_imap_port": int(profile.get("port") or 993),
                "qq_imap_user": str(profile.get("user") or "").strip(),
                "qq_imap_authcode": str(
                    profile.get("password")
                    or profile.get("authcode")
                    or profile.get("imap_password")
                    or ""
                ).strip(),
                "qq_imap_folder": str(profile.get("folder") or "INBOX").strip() or "INBOX",
                "qq_imap_security": str(
                    profile.get("security")
                    or profile.get("imap_security")
                    or profile.get("ssl_mode")
                    or "auto"
                ).strip().lower() or "auto",
                "mail_domain": "",
            }
    if not source and default_source_key:
        for item in email_sources:
            if isinstance(item, dict) and str(item.get("key") or item.get("id") or "").strip() == default_source_key:
                source = item
                break
    if not source and email_sources:
        source = next((item for item in email_sources if isinstance(item, dict)), None)

    profile = None
    mail_domain = ""
    if isinstance(source, dict):
        receiver = str(
            source.get("receiver")
            or source.get("receiver_profile")
            or source.get("imap_profile")
            or source.get("profile")
            or ""
        ).strip()
        mail_domain = str(source.get("domain") or "").strip().lower().lstrip("@")
        if receiver:
            profile = profiles_by_key.get(receiver)

    if not profile and profiles:
        profile = next((item for item in profiles if isinstance(item, dict)), None)

    if not isinstance(profile, dict):
        return legacy

    return {
        "qq_imap_host": str(profile.get("host") or "").strip(),
        "qq_imap_port": int(profile.get("port") or 993),
        "qq_imap_user": str(profile.get("user") or "").strip(),
        "qq_imap_authcode": str(
            profile.get("password")
            or profile.get("authcode")
            or profile.get("imap_password")
            or ""
        ).strip(),
        "qq_imap_folder": str(profile.get("folder") or "INBOX").strip() or "INBOX",
        "qq_imap_security": str(
            profile.get("security")
            or profile.get("imap_security")
            or profile.get("ssl_mode")
            or "auto"
        ).strip().lower() or "auto",
        "mail_domain": mail_domain,
    }


def get_pool(config=None, log: Optional[Callable[[str], None]] = None):
    """根据 config 拿到池实例, 相同连接参数复用同一个池"""
    with _pool_init_lock:
        if not config:
            return None
        user = config.get("mail_imap_user") or config.get("qq_imap_user")
        authcode = (
            config.get("mail_imap_password")
            or config.get("mail_imap_authcode")
            or config.get("qq_imap_authcode")
        )
        domain = config.get("mail_domain")
        if not (user and authcode and domain):
            return None
        host = config.get("mail_imap_host") or config.get("qq_imap_host", "imap.qq.com")
        port = int(config.get("mail_imap_port") or config.get("qq_imap_port", 993))
        folder = config.get("mail_imap_folder") or config.get("qq_imap_folder", "INBOX")
        security = str(config.get("mail_imap_security") or config.get("qq_imap_security") or "auto").strip().lower() or "auto"
        cache_key = (host, port, user, domain, folder, security)
        if cache_key in _pool_instances:
            return _pool_instances[cache_key]
        pool = QQMailPool(
            host=host,
            port=port,
            user=user,
            authcode=authcode,
            domain=domain,
            poll_interval=config.get("mail_poll_interval", 4),
            debug=bool(config.get("mail_debug", False)),
            folder=folder,
            security=security,
            log=log,
            log_level=("debug" if bool(config.get("mail_debug", False)) else config.get("log_level")),
        )
        pool.start()
        _pool_instances[cache_key] = pool
        return pool


# ---- CLI 自检 ----
#   python qq_mail_pool.py             → 生成一个地址等 60s OTP
#   python qq_mail_pool.py inspect     → 打印 INBOX 最新 5 封邮件的 header 详情
#   python qq_mail_pool.py inspect 10  → 同上, 取最新 10 封

def _cli_inspect(cfg, count=5, selected_key=""):
    """直接用 IMAP 取 INBOX 最新 N 封邮件, 打印 header 全貌"""
    cfg = _resolve_cli_imap_config(cfg, selected_key=selected_key)
    print(f"连接 {cfg['qq_imap_host']}:{cfg.get('qq_imap_port', 993)} ...")
    helper = QQMailPool(
        host=cfg["qq_imap_host"],
        port=int(cfg.get("qq_imap_port", 993)),
        user=cfg["qq_imap_user"],
        authcode=cfg["qq_imap_authcode"],
        domain=cfg.get("mail_domain", "") or "example.com",
        folder=cfg.get("qq_imap_folder", "INBOX"),
        security=cfg.get("qq_imap_security", "auto"),
    )
    imap = helper._open_imap()
    imap.login(cfg["qq_imap_user"], cfg["qq_imap_authcode"])
    try:
        imap.xatom("ID", '("name" "QQMailPool" "version" "1.0")')
    except Exception:
        pass
    folder = cfg.get("qq_imap_folder", "INBOX")
    if all(0x20 <= ord(c) <= 0x7E for c in folder):
        mailbox = folder
    else:
        mailbox = _imap_utf7_encode(folder)
    typ, data = imap.select(mailbox)
    print(f"SELECT {folder!r} → {typ} 邮件总数={data[0].decode() if data else '?'}")

    typ, data = imap.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        print("SEARCH ALL 无结果")
        return
    uids = data[0].split()
    print(f"{folder} 现有 UID 总数 = {len(uids)}, 最大 UID = {uids[-1].decode()}")

    target_domain = "@" + cfg.get("mail_domain", "").lower()
    print(f"\n查找域名: {target_domain}")

    print(f"\n--- 取最新 {count} 封 ---")
    tail = uids[-count:]
    matched = []
    for uid_b in reversed(tail):
        uid = uid_b.decode()
        typ, msg_data = imap.uid(
            "FETCH", uid_b,
            "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT DELIVERED-TO X-ORIGINAL-TO X-FORWARDED-TO RECEIVED)])",
        )
        if typ != "OK" or not msg_data:
            print(f"UID {uid}: FETCH 失败")
            continue
        raw = None
        for part in msg_data:
            if isinstance(part, tuple) and len(part) >= 2:
                raw = part[1]
                break
        if not raw:
            print(f"UID {uid}: 拿不到 raw")
            continue
        msg = email.message_from_bytes(raw)
        date_h = msg.get("Date", "?")
        from_h = QQMailPool._decode_header(msg.get("From", ""))
        to_h = QQMailPool._decode_header(msg.get("To", ""))
        subj_h = QQMailPool._decode_header(msg.get("Subject", ""))
        deliv = msg.get("Delivered-To", "")
        x_orig = msg.get("X-Original-To", "")
        x_fwd = msg.get("X-Forwarded-To", "")
        print(f"\nUID {uid}")
        print(f"  Date     : {date_h}")
        print(f"  From     : {from_h[:80]}")
        print(f"  To       : {to_h[:80]}")
        print(f"  Subject  : {subj_h[:80]}")
        if deliv:    print(f"  Delivered-To  : {deliv[:80]}")
        if x_orig:   print(f"  X-Original-To : {x_orig[:80]}")
        if x_fwd:    print(f"  X-Forwarded-To: {x_fwd[:80]}")
        # Received: for <addr> 行
        for rcv in (msg.get_all("Received") or [])[:3]:
            m = re.search(r"for\s+<?([\w.+\-]+@[\w.\-]+)>?", rcv)
            if m:
                print(f"  Received-for  : {m.group(1)}")
        if target_domain and target_domain in (to_h + " " + deliv + " " + x_orig + " " + x_fwd).lower():
            matched.append(uid)

    print(f"\n--- 结果 ---")
    print(f"匹配域名 {target_domain} 的邮件: {matched if matched else '无'}")
    if not matched:
        print("\n如果你刚收到过 OpenAI 邮件却没列在上面, 可能原因:")
        print("  1. count 太小, 用 `python qq_mail_pool.py inspect 30` 多取几条")
        print("  2. 邮件不在 INBOX (查看 QQ 邮箱的`其他文件夹`、`广告邮件`、`订阅邮件`等)")
        print("  3. Cloudflare 还没投递成功 (CF dashboard → Email → Routing → Activity log)")
    imap.logout()


if __name__ == "__main__":
    import json
    import sys as _sys

    cfg_path = _paths.config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if len(_sys.argv) > 1 and _sys.argv[1] == "inspect":
        selected_key = ""
        n = 5
        if len(_sys.argv) > 2:
            if _sys.argv[2].isdigit():
                n = int(_sys.argv[2])
            else:
                selected_key = _sys.argv[2]
        if len(_sys.argv) > 3 and _sys.argv[3].isdigit():
            n = int(_sys.argv[3])
        _cli_inspect(cfg, n, selected_key=selected_key)
        raise SystemExit(0)

    cfg = _resolve_cli_imap_config(cfg)
    cfg["mail_debug"] = True
    pool = get_pool(cfg)
    if not pool:
        print("config.json 缺少 qq_imap_user / qq_imap_authcode / mail_domain")
        raise SystemExit(1)

    addr = pool.acquire_email()
    print(f"\n测试地址: {addr}")
    print(f"→ 现在【你手动】用任何邮箱(Gmail/QQ/外部) 给这个地址发一封")
    print(f"  正文带 6 位数字的邮件 (例: 'code 123456'), 脚本等 120s 抓取")
    print(f"  CF Email Routing 应当转发到 QQ, 池子应当抓到\n")
    code = pool.wait_for_otp(addr, timeout=120)
    print(f"\nOTP: {code}")
