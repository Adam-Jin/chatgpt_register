#!/usr/bin/env python3
"""
phone_pool.py - hero-sms 手机号复用池。

OpenAI 允许一个手机号绑 ~3 个账号; 一次 `getNumberV2` (~$0.05) 后, 在
activation 窗口 (默认 ~20min) 内继续把同 number 喂给 OpenAI 注册新账号,
就能把单号成本摊到 2~3 个账号上。

核心约束:
  - 一个号同一时刻只能被一个注册流程使用 (排他, 否则两边 wait_otp 拿到的
    OTP 互相串台). 用 SQLite 行级 lease (lease_owner + lease_until) 抢占,
    租约到期或心跳停 → 别的进程能抢走.
  - 收码必须用 herosms 的 getAllSms (按 sms_id 去重), 不能用 getStatusV2.
  - 不调用 finishActivation 直到 used_count 用满, 否则 activation inactive
    了, getAllSms 返回 409, 号就废了.

CLI:
  init              建表
  list              本地池快照 (含 lease 状态)
  reconcile         拉 hero-sms getActiveActivations 与本地表对账
  prune             清掉 expired/dead/finished 旧记录 (默认保留 7 天)
  stats             成本摊销统计
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sms_provider import (
    AcquireFailed, NoNumberAvailable, SmsProviderError, SmsSession,
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

DEFAULT_MAX_REUSE = 3
DEFAULT_LEASE_SECONDS = 60       # 一次抢占的初始租约
DEFAULT_HEARTBEAT_SECONDS = 30   # 持有方每 30s 续租一次
END_AT_SAFETY_MARGIN = 30        # activation_end_at 距 now 不足这值不再复用


# ============================================================
# DDL
# ============================================================

PHONE_POOL_DDL = """
CREATE TABLE IF NOT EXISTS phone_pool (
    activation_id   TEXT PRIMARY KEY,
    phone_number    TEXT NOT NULL,
    provider        TEXT NOT NULL DEFAULT 'herosms',
    country         INTEGER,
    service         TEXT,
    cost            REAL,
    acquired_at     INTEGER NOT NULL,
    end_at          INTEGER,
    used_count      INTEGER NOT NULL DEFAULT 0,
    last_otp_at     INTEGER,
    status          TEXT NOT NULL DEFAULT 'fresh',
    dead_reason     TEXT,
    lease_owner     TEXT,
    lease_until     INTEGER,
    can_get_another INTEGER NOT NULL DEFAULT 1
)
"""

PHONE_POOL_SMS_DDL = """
CREATE TABLE IF NOT EXISTS phone_pool_sms (
    sms_id          TEXT PRIMARY KEY,
    activation_id   TEXT NOT NULL,
    code            TEXT,
    text            TEXT,
    sms_date        TEXT,
    received_at     INTEGER NOT NULL,
    used_for_account TEXT
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pp_status ON phone_pool(status)",
    "CREATE INDEX IF NOT EXISTS idx_pp_lease ON phone_pool(lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_pps_act ON phone_pool_sms(activation_id)",
]


# ============================================================
# 异常
# ============================================================

class LeaseLost(SmsProviderError):
    """续租失败 (被别的进程抢了或行被清). 主流程应当立刻退出本次复用。"""


class PhonePoolCapacityExhausted(Exception):
    def __init__(self, current_active: int, max_active: int):
        super().__init__(f"phone pool active cap exhausted: {current_active}/{max_active}")
        self.current_active = current_active
        self.max_active = max_active


# ============================================================
# 数据结构
# ============================================================

@dataclass
class PhoneLease:
    """一次注册流程对某号的租约 + 配套上下文。

    lifecycle:
        lease = pool.acquire_or_reuse()
        lease.start_heartbeat()
        try:
            ... 跑 OpenAI add_phone send + wait_otp(since_sms_ids=lease.baseline_sms_ids) + validate ...
            lease.mark_used(sms_id=..., code=..., account_id=...)
        except: lease.mark_dead(reason=...)
        finally: lease.stop_heartbeat()
    """
    pool: "PhonePool"
    activation_id: str
    phone_number: str
    cost: float
    used_count: int          # 拿到租约时的 used_count (注册 OpenAI 时 +1 后是当前账号序号)
    is_reused: bool          # True=复用旧号, False=本次新拿的
    locale: Optional[str] = None
    end_at: Optional[int] = None
    extra: dict = field(default_factory=dict)

    # 内部状态
    _heartbeat_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _heartbeat_stop: Optional[threading.Event] = field(default=None, repr=False)
    _lease_lost: bool = field(default=False, repr=False)
    _released: bool = field(default=False, repr=False)

    # ---- 给主流程使用 ----

    def baseline_sms_ids(self) -> set[str]:
        """已经看过的 sms id (避免把上一个账号的 OTP 误当本次)."""
        return self.pool.get_seen_sms_ids(self.activation_id)

    def to_session(self) -> SmsSession:
        """转成 SmsSession 喂给 OpenAI 流程."""
        return SmsSession(
            provider="herosms",
            number=self.phone_number,
            handle=self.activation_id,
            locale=self.locale,
            cost=self.cost,
            extra={**self.extra, "phone_pool_lease": True,
                   "is_reused": self.is_reused,
                   "used_count": self.used_count},
        )

    def lease_lost_check(self) -> bool:
        """传给 wait_otp on_lease_lost 用. 心跳线程检测到丢租约会置 True."""
        return self._lease_lost

    def start_heartbeat(self):
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_stop = threading.Event()
        t = threading.Thread(
            target=self._heartbeat_loop, name=f"lease-hb-{self.activation_id}",
            daemon=True)
        self._heartbeat_thread = t
        t.start()

    def stop_heartbeat(self):
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        # 不 join: 心跳线程睡 30s 太长, daemon 自然退就行

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self.pool.heartbeat_seconds):
            if self._released:
                return
            ok = self.pool._renew_lease(self.activation_id, self.pool.owner_id)
            if not ok:
                self._lease_lost = True
                self.pool._unregister_lease_worker(self.activation_id)
                self.pool._notify_cap()
                return

    def mark_used(self, sms_id: str, code: str, account_id: Optional[str] = None):
        self.pool._mark_used(self, sms_id, code, account_id)
        self._released = True
        self.stop_heartbeat()

    def mark_dead(self, reason: str):
        self.pool._mark_dead(self, reason)
        self._released = True
        self.stop_heartbeat()

    def release_lease_only(self):
        """主动还租约不改 used_count (用于 OpenAI 拒号但不知道号是否真坏的兜底).

        实践中走 mark_dead('openai_rejected') 更简单, 这个方法暂留.
        """
        self.pool._release_lease(self.activation_id, self.pool.owner_id)
        self._released = True
        self.stop_heartbeat()


# ============================================================
# 主类
# ============================================================

class PhonePool:
    def __init__(self, provider, *,
                 db_path: str = DB_PATH,
                 max_reuse: int = DEFAULT_MAX_REUSE,
                 max_active: int = 0,
                 acquire_timeout: float = 60.0,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS,
                 heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
                 log: Callable[[str], None] = print):
        """
        provider: 必须是 HeroSmsProvider 实例 (其他 provider 没有"复用同号"的 API).
        """
        self.provider = provider
        self.db_path = db_path
        self.max_reuse = int(max_reuse)
        self.max_active = int(max_active)
        self.acquire_timeout = float(acquire_timeout)
        self.lease_seconds = int(lease_seconds)
        self.heartbeat_seconds = int(heartbeat_seconds)
        self.log = log
        self.owner_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._cap_cond = threading.Condition()
        self.cap_waiters = 0
        self._state_lock = threading.Lock()
        self._fresh_total = 0
        self._reuse_total = 0
        self._lease_workers: dict[str, Optional[str]] = {}
        self._init_db()

    # ---------- DB plumbing ----------

    @contextmanager
    def _conn(self, immediate: bool = False):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            if immediate:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init_db(self):
        with self._conn() as c:
            c.execute(PHONE_POOL_DDL)
            c.execute(PHONE_POOL_SMS_DDL)
            for ddl in INDEXES:
                c.execute(ddl)

    # ---------- 公开方法 ----------

    def reconcile(self, finish_expired: bool = True):
        """启动时调一次: 用 hero-sms getActiveActivations 修正本地表.

        判断 "号还能不能收码" 的真相: 比较 now vs estDate (云端真实到期时间).
          - estDate > now → status='reused', end_at=estDate
          - estDate <= now → status='expired', 顺手 finishActivation 清账

        分支:
          - 云端有 + 本地无 → 进程崩过, 补录 (used_count=1)
          - 云端有 + 本地有 → 刷新 end_at / phone_number
          - 云端无 + 本地仍 fresh/reused → 标 expired (云端已自己回收)
        """
        from herosms_pool import get_active_activations, finish_activation
        try:
            remote = get_active_activations(self.provider.api_key)
        except Exception as e:
            self.log(f"[phone_pool] reconcile getActiveActivations 失败: {e}")
            return

        now = int(time.time())
        remote_by_id = {str(r.get("activationId")): r
                        for r in (remote or []) if isinstance(r, dict)}

        to_finish: list[str] = []
        cap_changed = False

        with self._conn(immediate=True) as c:
            # A. 云端有的: 按 estDate 分活/死
            for aid, r in remote_by_id.items():
                phone = str(r.get("phoneNumber") or "")
                cost = float(r.get("activationCost") or 0)
                country_code = int(r.get("countryCode") or 0) or None
                service = r.get("serviceCode") or None
                est_date_str = r.get("estDate") or r.get("activationEndTime") or ""
                est_epoch = self._parse_local_dt(est_date_str)
                alive = est_epoch is not None and est_epoch > now

                row = c.execute(
                    "SELECT activation_id, used_count, status FROM phone_pool "
                    "WHERE activation_id=?", (aid,)).fetchone()

                if alive:
                    if not row:
                        c.execute(
                            "INSERT INTO phone_pool (activation_id, phone_number, "
                            "provider, country, service, cost, acquired_at, end_at, "
                            "used_count, status, can_get_another) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (aid, phone, "herosms", country_code, service, cost,
                             now, est_epoch, 1, "reused", 1))
                        self.log(f"[phone_pool] reconcile +new {aid} {phone} "
                                 f"(estDate={est_date_str})")
                    else:
                        c.execute(
                            "UPDATE phone_pool SET phone_number=?, "
                            "can_get_another=1, end_at=? WHERE activation_id=?",
                            (phone, est_epoch, aid))
                else:
                    # 接收窗口已过, 不能复用
                    used_count = int(row["used_count"]) if row else 1
                    if row:
                        c.execute(
                            "UPDATE phone_pool SET status='expired', "
                            "can_get_another=0, "
                            "lease_owner=NULL, lease_until=NULL "
                            "WHERE activation_id=?", (aid,))
                        self._unregister_lease_worker(aid)
                        cap_changed = True
                    else:
                        c.execute(
                            "INSERT INTO phone_pool (activation_id, phone_number, "
                            "provider, country, service, cost, acquired_at, end_at, "
                            "used_count, status, can_get_another) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (aid, phone, "herosms", country_code, service, cost,
                             now, est_epoch or now, used_count, "expired", 0))
                    if finish_expired:
                        to_finish.append(aid)
                    self.log(f"[phone_pool] reconcile -expired {aid} "
                             f"(estDate={est_date_str}, used_count={used_count})")

            # B. 云端没有但本地仍 fresh/reused → expired
            cur = c.execute(
                "SELECT activation_id FROM phone_pool "
                "WHERE status IN ('fresh','reused')")
            local_ids = [r["activation_id"] for r in cur.fetchall()]
            for aid in local_ids:
                if aid not in remote_by_id:
                    c.execute(
                        "UPDATE phone_pool SET status='expired', "
                        "lease_owner=NULL, lease_until=NULL "
                        "WHERE activation_id=?", (aid,))
                    self._unregister_lease_worker(aid)
                    cap_changed = True
                    self.log(f"[phone_pool] reconcile -gone {aid} "
                             f"(云端已无)")

        # 在事务外调网络: 给过期号 finishActivation 清账
        for aid in to_finish:
            try:
                finish_activation(self.provider.api_key, aid)
                self.log(f"[phone_pool] finishActivation {aid} ok")
            except Exception as e:
                self.log(f"[phone_pool] finishActivation {aid} 失败 (忽略): {e}")
        if cap_changed:
            self._notify_cap()

    def acquire_or_reuse(self, **acquire_kwargs) -> PhoneLease:
        """优先复用; 没可复用号或抢不到时 fallback 到 provider.acquire().

        acquire_kwargs 透传给 provider.acquire (country/service/max_price/...).
        """
        # 先尝试原子抢占可复用号
        reused = self._try_claim_reused()
        if reused is not None:
            self.log(
                f"[phone_pool] REUSE activation={reused.activation_id} "
                f"number={reused.phone_number} used_count={reused.used_count}/"
                f"{self.max_reuse}")
            return reused

        if self.max_active > 0:
            deadline = time.time() + self.acquire_timeout
            with self._cap_cond:
                while True:
                    reused = self._try_claim_reused()
                    if reused is not None:
                        self.log(
                            f"[phone_pool] REUSE activation={reused.activation_id} "
                            f"number={reused.phone_number} used_count={reused.used_count}/"
                            f"{self.max_reuse}")
                        return reused
                    with self._conn() as c:
                        active = self._count_active_locked(c)
                    if active < self.max_active:
                        return self._acquire_fresh(**acquire_kwargs)
                    remain = deadline - time.time()
                    if remain <= 0:
                        raise PhonePoolCapacityExhausted(active, self.max_active)
                    self.cap_waiters += 1
                    try:
                        self._cap_cond.wait(timeout=min(remain, 5.0))
                    finally:
                        self.cap_waiters = max(0, self.cap_waiters - 1)

        # fallback: 拿全新号入池
        return self._acquire_fresh(**acquire_kwargs)

    def _acquire_fresh(self, **acquire_kwargs) -> PhoneLease:
        sess = self.provider.acquire(**acquire_kwargs)
        now = int(time.time())
        end_at = self._parse_end_at(sess.extra.get("activationEndTime"), now)
        with self._conn(immediate=True) as c:
            c.execute(
                "INSERT OR REPLACE INTO phone_pool (activation_id, phone_number, "
                "provider, country, service, cost, acquired_at, end_at, "
                "used_count, status, lease_owner, lease_until, can_get_another) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sess.handle, str(sess.number), "herosms",
                 acquire_kwargs.get("country"),
                 sess.extra.get("service") or acquire_kwargs.get("service"),
                 sess.cost, now, end_at, 0, "fresh",
                 self.owner_id, now + self.lease_seconds, 1))
        with self._state_lock:
            self._fresh_total += 1
        lease = PhoneLease(
            pool=self, activation_id=sess.handle,
            phone_number=str(sess.number), cost=sess.cost or 0,
            used_count=0, is_reused=False, locale=sess.locale,
            end_at=end_at, extra=dict(sess.extra),
        )
        self._register_lease_worker(lease.activation_id)
        self.log(f"[phone_pool] FRESH activation={lease.activation_id} "
                 f"number={lease.phone_number} cost=${lease.cost}")
        return lease

    def get_seen_sms_ids(self, activation_id: str) -> set[str]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT sms_id FROM phone_pool_sms WHERE activation_id=?",
                (activation_id,))
            return {r["sms_id"] for r in cur.fetchall()}

    def stats(self) -> dict[str, Any]:
        now = int(time.time())
        with self._conn() as c:
            active = self._count_active_locked(c)
            spent_row = c.execute(
                "SELECT COALESCE(SUM(cost), 0) AS spent FROM phone_pool"
            ).fetchone()
            lease_rows = c.execute(
                "SELECT activation_id, phone_number, used_count, status "
                "FROM phone_pool WHERE lease_owner=? AND (lease_until IS NULL OR lease_until > ?)",
                (self.owner_id, now),
            ).fetchall()
        with self._state_lock:
            fresh_total = self._fresh_total
            reuse_total = self._reuse_total
            lease_workers = dict(self._lease_workers)
        total = fresh_total + reuse_total
        reuse_rate = (reuse_total / total) if total else 0.0
        leases = []
        for row in lease_rows:
            leases.append({
                "worker_id": lease_workers.get(row["activation_id"]),
                "activation_id": row["activation_id"],
                "phone_number": row["phone_number"],
                "used_count": int(row["used_count"] or 0),
                "max_reuse": self.max_reuse,
                "is_reused": row["status"] == "reused",
            })
        return {
            "active": int(active),
            "max_active": self.max_active,
            "fresh_total": fresh_total,
            "reuse_total": reuse_total,
            "reuse_rate": reuse_rate,
            "spent": float(spent_row["spent"] or 0.0),
            "leases": leases,
            "cap_waiters": self.cap_waiters,
        }

    # ---------- 内部: lease + state ----------

    def _try_claim_reused(self) -> Optional[PhoneLease]:
        """原子 SQL 抢占一条可复用号。命中返回 PhoneLease, 否则 None。"""
        now = int(time.time())
        cutoff = now + END_AT_SAFETY_MARGIN
        new_lease_until = now + self.lease_seconds
        with self._conn(immediate=True) as c:
            row = c.execute(
                "SELECT activation_id, phone_number, cost, used_count, country, "
                "       service, end_at "
                "FROM phone_pool "
                "WHERE status='reused' AND used_count<? "
                "  AND can_get_another=1 "
                "  AND (end_at IS NULL OR end_at > ?) "
                "  AND (lease_owner IS NULL OR lease_until < ?) "
                "ORDER BY used_count DESC, end_at ASC "
                "LIMIT 1",
                (self.max_reuse, cutoff, now)).fetchone()
            if not row:
                # 调试: 没选中时, 把所有 reused 状态的号 + 各过滤位拉出来打印
                diag = c.execute(
                    "SELECT activation_id, status, used_count, can_get_another, "
                    "       end_at, lease_owner, lease_until, "
                    "       (CASE WHEN end_at IS NULL OR end_at > ? "
                    "        THEN 1 ELSE 0 END) AS e_ok, "
                    "       (CASE WHEN lease_owner IS NULL OR lease_until < ? "
                    "        THEN 1 ELSE 0 END) AS l_ok "
                    "FROM phone_pool "
                    "WHERE status IN ('fresh','reused') "
                    "ORDER BY acquired_at DESC LIMIT 10",
                    (cutoff, now)).fetchall()
                if diag:
                    self.log(f"[phone_pool] _try_claim_reused MISS "
                             f"(now={now} cutoff={cutoff} max_reuse={self.max_reuse}):")
                    for d in diag:
                        self.log(f"  aid={d['activation_id']} "
                                 f"status={d['status']} used={d['used_count']} "
                                 f"cga={d['can_get_another']} "
                                 f"end_in={d['end_at']-now if d['end_at'] else 'N/A'}s "
                                 f"e_ok={d['e_ok']} l_ok={d['l_ok']} "
                                 f"lease_owner={d['lease_owner']!r}")
                else:
                    self.log(f"[phone_pool] _try_claim_reused MISS (无任何活号)")
                return None
            # 原子抢占: 用同样条件 UPDATE, 防 TOCTOU
            cur = c.execute(
                "UPDATE phone_pool SET lease_owner=?, lease_until=? "
                "WHERE activation_id=? "
                "  AND (lease_owner IS NULL OR lease_until < ?)",
                (self.owner_id, new_lease_until, row["activation_id"], now))
            if cur.rowcount == 0:
                return None
        with self._state_lock:
            self._reuse_total += 1
        lease = PhoneLease(
            pool=self, activation_id=row["activation_id"],
            phone_number=row["phone_number"], cost=float(row["cost"] or 0),
            used_count=int(row["used_count"]), is_reused=True,
            locale=str(row["country"]) if row["country"] is not None else None,
            end_at=row["end_at"],
            extra={"service": row["service"]},
        )
        self._register_lease_worker(lease.activation_id)
        return lease

    def _renew_lease(self, activation_id: str, owner: str) -> bool:
        """心跳续租. 返回 False 表示我已经被抢走."""
        now = int(time.time())
        new_until = now + self.lease_seconds
        with self._conn(immediate=True) as c:
            cur = c.execute(
                "UPDATE phone_pool SET lease_until=? "
                "WHERE activation_id=? AND lease_owner=?",
                (new_until, activation_id, owner))
            return cur.rowcount > 0

    def _release_lease(self, activation_id: str, owner: str):
        with self._conn(immediate=True) as c:
            cur = c.execute(
                "UPDATE phone_pool SET lease_owner=NULL, lease_until=NULL "
                "WHERE activation_id=? AND lease_owner=?",
                (activation_id, owner))
        self._unregister_lease_worker(activation_id)
        if cur.rowcount:
            self._notify_cap()

    def _mark_used(self, lease: PhoneLease, sms_id: str, code: str,
                   account_id: Optional[str]):
        now = int(time.time())
        new_count = lease.used_count + 1
        finished = new_count >= self.max_reuse
        new_status = "finished" if finished else "reused"
        with self._conn(immediate=True) as c:
            cur = c.execute(
                "UPDATE phone_pool SET used_count=?, status=?, last_otp_at=?, "
                "lease_owner=NULL, lease_until=NULL "
                "WHERE activation_id=? AND lease_owner=?",
                (new_count, new_status, now, lease.activation_id, self.owner_id))
            if cur.rowcount == 0:
                # 我们已经被抢了, 但成功注册了; 还是把 sms 落表, 只是不动 used_count
                self.log(f"[phone_pool] WARN mark_used: lease lost on "
                         f"{lease.activation_id} but OpenAI succeeded")
            c.execute(
                "INSERT OR IGNORE INTO phone_pool_sms (sms_id, activation_id, "
                "code, sms_date, received_at, used_for_account) "
                "VALUES (?,?,?,?,?,?)",
                (str(sms_id), lease.activation_id, code, None, now, account_id))
        self.log(f"[phone_pool] mark_used activation={lease.activation_id} "
                 f"used_count={new_count}/{self.max_reuse}"
                 + (" FINISHED" if finished else ""))
        self._unregister_lease_worker(lease.activation_id)
        self._notify_cap()
        if finished:
            try:
                self.provider.release_ok(lease.to_session())
                self.log(f"[phone_pool] finishActivation {lease.activation_id} ok")
            except Exception as e:
                self.log(f"[phone_pool] finishActivation 失败 (忽略): {e}")

    def _mark_dead(self, lease: PhoneLease, reason: str):
        now = int(time.time())
        with self._conn(immediate=True) as c:
            c.execute(
                "UPDATE phone_pool SET status='dead', dead_reason=?, "
                "lease_owner=NULL, lease_until=NULL "
                "WHERE activation_id=? AND lease_owner=?",
                (reason, lease.activation_id, self.owner_id))
        self._unregister_lease_worker(lease.activation_id)
        self._notify_cap()
        self.log(f"[phone_pool] mark_dead activation={lease.activation_id} "
                 f"reason={reason}")
        # 试着退款 (cancel). hero-sms <120s 强制不让 cancel, 失败就让它过期
        try:
            self.provider.release_no_sms(lease.to_session())
        except Exception as e:
            self.log(f"[phone_pool] cancelActivation 失败 (忽略): {e}")

    @staticmethod
    def _parse_end_at(s: Any, now: int) -> int:
        """ISO 8601 (e.g. 2026-02-18T18:11:23+00:00) 转 epoch.

        getNumberV2 用这种格式. 失败 → now + 20*60。
        """
        if not s:
            return now + 20 * 60
        try:
            from datetime import datetime
            txt = str(s).replace("Z", "+00:00")
            return int(datetime.fromisoformat(txt).timestamp())
        except Exception:
            return now + 20 * 60

    def _count_active_locked(self, c) -> int:
        now = int(time.time())
        row = c.execute(
            "SELECT COUNT(*) AS n FROM phone_pool "
            "WHERE status IN ('fresh','reused') AND (end_at IS NULL OR end_at > ?)",
            (now,),
        ).fetchone()
        return int(row["n"] or 0)

    def _register_lease_worker(self, activation_id: str) -> None:
        with self._state_lock:
            self._lease_workers[activation_id] = self._current_worker_id()

    def _unregister_lease_worker(self, activation_id: str) -> None:
        with self._state_lock:
            self._lease_workers.pop(activation_id, None)

    def _notify_cap(self) -> None:
        with self._cap_cond:
            self._cap_cond.notify_all()

    @staticmethod
    def _current_worker_id() -> Optional[str]:
        try:
            from monitor import current_worker_id

            return current_worker_id()
        except Exception:
            return None

    # hero-sms 背后是 sms-activate 协议, 时间戳用莫斯科时间 (UTC+3, 无 DST)
    HEROSMS_TZ_OFFSET_HOURS = 3

    @staticmethod
    def _parse_local_dt(s: Any) -> Optional[int]:
        """hero-sms 'YYYY-MM-DD HH:MM:SS' (莫斯科 UTC+3) 转 epoch.

        getActiveActivations 的 estDate / activationTime / receiveSmsDate 都
        用这种格式. 解析失败返回 None。
        """
        if not s:
            return None
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
            tz = timezone(timedelta(hours=PhonePool.HEROSMS_TZ_OFFSET_HOURS))
            return int(dt.replace(tzinfo=tz).timestamp())
        except Exception:
            return None


# ============================================================
# CLI
# ============================================================

def _load_cfg():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _build_pool(cfg):
    from herosms_pool import HeroSmsProvider
    provider = HeroSmsProvider(cfg)
    return PhonePool(
        provider,
        max_reuse=int(cfg.get("phone_max_reuse", DEFAULT_MAX_REUSE)),
        max_active=int(cfg.get("phone_max_active", 0) or 0),
        acquire_timeout=float(cfg.get("phone_acquire_timeout", 60.0)),
        lease_seconds=int(cfg.get("phone_pool_lease_seconds",
                                  DEFAULT_LEASE_SECONDS)),
        heartbeat_seconds=int(cfg.get("phone_pool_heartbeat_seconds",
                                      DEFAULT_HEARTBEAT_SECONDS)),
    )


def cmd_init(args, cfg):
    pool = _build_pool(cfg)
    print(f"db: {pool.db_path}; tables ready")


def cmd_list(args, cfg):
    pool = _build_pool(cfg)
    now = int(time.time())
    with pool._conn() as c:
        rows = list(c.execute(
            "SELECT * FROM phone_pool ORDER BY acquired_at DESC LIMIT ?",
            (args.limit,)))
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(empty)")
        return
    print(f"{'aid':<12} {'number':<14} {'status':<10} {'used':<5} "
          f"{'lease':<8} {'end_in':<7} cost")
    for r in rows:
        leased = "-"
        if r["lease_owner"] and r["lease_until"] and r["lease_until"] > now:
            leased = f"{r['lease_until']-now}s"
        end_in = "-"
        if r["end_at"]:
            end_in = f"{(r['end_at']-now)//60}m"
        print(f"{r['activation_id']:<12} {r['phone_number']:<14} "
              f"{r['status']:<10} {r['used_count']:<5} {leased:<8} "
              f"{end_in:<7} ${float(r['cost'] or 0):.4f}")


def cmd_reconcile(args, cfg):
    pool = _build_pool(cfg)
    pool.reconcile()
    cmd_list(argparse.Namespace(limit=50, json=False), cfg)


def cmd_prune(args, cfg):
    pool = _build_pool(cfg)
    cutoff = int(time.time()) - args.days * 86400
    with pool._conn(immediate=True) as c:
        cur = c.execute(
            "DELETE FROM phone_pool "
            "WHERE acquired_at < ? AND status IN ('finished','dead','expired')",
            (cutoff,))
        n_pp = cur.rowcount
        cur = c.execute(
            "DELETE FROM phone_pool_sms WHERE received_at < ?", (cutoff,))
        n_sms = cur.rowcount
    print(f"pruned: phone_pool={n_pp} phone_pool_sms={n_sms}")


def cmd_stats(args, cfg):
    pool = _build_pool(cfg)
    snapshot = pool.stats()
    with pool._conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(used_count),0) AS accounts "
            "FROM phone_pool").fetchone()
    n, spent, accounts = row["n"], float(snapshot["spent"]), int(row["accounts"])
    avg = spent / accounts if accounts else 0
    print(f"numbers   : {n}")
    print(f"accounts  : {accounts}  (used_count 累计)")
    print(f"spent     : ${spent:.4f}")
    print(f"active    : {snapshot['active']} / {snapshot['max_active']}")
    print(f"reuse     : {snapshot['reuse_total']} ({snapshot['reuse_rate']*100:.1f}%)")
    print(f"avg/acc   : ${avg:.4f}  (vs 单号单账号 ${spent/n if n else 0:.4f})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="建表")
    p = sub.add_parser("list", help="本地池快照")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    sub.add_parser("reconcile", help="拉云端列表对账")
    p = sub.add_parser("prune", help="清掉旧的 finished/dead/expired")
    p.add_argument("--days", type=int, default=7)
    sub.add_parser("stats", help="成本摊销统计")

    args = ap.parse_args()
    cfg = _load_cfg()
    handlers = {
        "init": cmd_init, "list": cmd_list, "reconcile": cmd_reconcile,
        "prune": cmd_prune, "stats": cmd_stats,
    }
    handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
