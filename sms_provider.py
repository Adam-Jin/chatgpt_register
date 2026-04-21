#!/usr/bin/env python3
"""
sms_provider.py - 临时号码接码服务的统一抽象。

provider lifecycle:
    sess = provider.acquire(...)        # 拿号
    code = provider.wait_otp(sess, ...) # 拉短信
    if code:
        provider.release_ok(sess)       # 成功 → 终态结算
    else:
        provider.release_no_sms(sess)   # 收不到 → 释放/退款

acquire_with_retry: 收不到短信时自动 release_no_sms + 换号, 直到 max_retries。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ============================================================
# 异常
# ============================================================

class SmsProviderError(Exception):
    """provider 通用错误基类。"""


class NoNumberAvailable(SmsProviderError):
    """池中/接口没有满足条件的号 (价格/国家/库存)。"""


class AcquireFailed(SmsProviderError):
    """拿号过程出错 (网络/鉴权/余额不足等)。"""


# ============================================================
# 数据结构
# ============================================================

@dataclass
class SmsSession:
    """一次接码会话。number 是 E.164 不带 + 的纯数字。"""
    provider: str          # "quackr" / "herosms"
    number: str            # 纯数字, 例: 66812345678
    handle: str            # provider 内部句柄: quackr=number, herosms=activationId
    locale: Optional[str] = None       # 国家短码 (us/uk/th/...)
    cost: Optional[float] = None       # 本次冻结金额 (USD); quackr 免费 → None
    extra: dict = field(default_factory=dict)


# ============================================================
# 基类
# ============================================================

class SmsProvider:
    name: str = "base"

    # ---- 必须实现 ----

    def acquire(self, **kwargs) -> SmsSession:
        raise NotImplementedError

    def wait_otp(self, session: SmsSession, *,
                 regex: str = r"\b(\d{4,8})\b",
                 timeout: int = 180,
                 poll_interval: int = 5,
                 log: Callable[[str], None] = print) -> Optional[str]:
        raise NotImplementedError

    def release_ok(self, session: SmsSession) -> None:
        """成功完成: hero-sms→finishActivation, quackr→mark_used(success=True)。"""
        raise NotImplementedError

    def release_no_sms(self, session: SmsSession) -> None:
        """收不到短信: hero-sms→cancelActivation(退款), quackr→release(只放 claim)。"""
        raise NotImplementedError

    def release_bad(self, session: SmsSession, reason: str = "") -> None:
        """号本身不合用 (被目标平台拒绝): hero-sms→cancel, quackr→mark_dead。"""
        raise NotImplementedError

    # ---- 通用: 自动换号重试 ----

    def acquire_with_retry(
        self,
        max_retries: int = 3,
        *,
        wait_timeout: int = 120,
        poll_interval: int = 5,
        regex: str = r"\b(\d{4,8})\b",
        log: Callable[[str], None] = print,
        **acquire_kwargs,
    ) -> tuple[SmsSession, str]:
        """循环 acquire→wait_otp, 收不到就换号; 命中返回 (session, code)。

        acquire 抛 NoNumberAvailable 直接传出去, 不重试 (没货就是没货)。
        最后一次仍收不到, 抛 SmsProviderError 并把最后一个 session release 掉。
        """
        last_err = None
        for attempt in range(1, max_retries + 1):
            log(f"[{self.name}] attempt {attempt}/{max_retries} acquire...")
            sess = self.acquire(**acquire_kwargs)
            log(f"[{self.name}] got number={sess.number} "
                f"handle={sess.handle} cost={sess.cost}")
            try:
                code = self.wait_otp(sess, regex=regex,
                                     timeout=wait_timeout,
                                     poll_interval=poll_interval, log=log)
            except Exception as e:
                last_err = e
                log(f"[{self.name}] wait_otp error: {e}; release_no_sms")
                try:
                    self.release_no_sms(sess)
                except Exception as e2:
                    log(f"[{self.name}] release_no_sms also failed: {e2}")
                continue
            if code:
                return sess, code
            log(f"[{self.name}] no SMS in {wait_timeout}s, release_no_sms + retry")
            try:
                self.release_no_sms(sess)
            except Exception as e:
                log(f"[{self.name}] release_no_sms failed: {e}")
        raise SmsProviderError(
            f"{self.name}: 重试 {max_retries} 次仍未收到短信"
            + (f" (last_err={last_err})" if last_err else "")
        )


# ============================================================
# 工厂
# ============================================================

def get_provider(name: str, cfg: dict) -> SmsProvider:
    """按名称构造 provider。lazy import 避免循环。"""
    name = (name or "").lower()
    if name == "quackr":
        from quackr_pool import QuackrProvider
        return QuackrProvider(cfg)
    if name in ("herosms", "hero-sms", "hero_sms"):
        from herosms_pool import HeroSmsProvider
        return HeroSmsProvider(cfg)
    raise ValueError(f"unknown sms provider: {name!r}")
