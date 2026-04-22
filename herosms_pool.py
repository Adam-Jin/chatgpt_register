#!/usr/bin/env python3
"""
herosms_pool.py - hero-sms.com (sms-activate 兼容协议) 接码 provider。

API: https://hero-sms.com/stubs/handler_api.php
计费模型: getNumber 立即冻结 activationCost; cancel 全退; finish/超时 真扣。
默认: country=52 (Thailand), service=oai (OpenAI/ChatGPT), maxPrice=$0.05。

CLI:
  verify              校对 country/service ID (打印 Thailand & OpenAI 候选)
  balance             查余额
  prices              当前 service+country 的报价 + 库存
  acquire             拿一个号 (默认 fixedPrice=true)
  wait-otp <id>       轮询 OTP
  cancel <id>         取消 (退款)
  finish <id>         完成 (扣费)
  active              当前进行中的 activation 列表
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Callable, Optional

from curl_cffi import requests as http

from sms_provider import (
    AcquireFailed, NoNumberAvailable, SmsProvider, SmsProviderError, SmsSession,
)


class ActivationInactive(SmsProviderError):
    """activation 已 finish/cancel/expired (HTTP 409). 号 dead, 不要再 cancel/finish。"""

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config.json")
HEROSMS_API = "https://hero-sms.com/stubs/handler_api.php"

DEFAULT_COUNTRY = 52       # Thailand (sms-activate 标准, verify 命令可校对)
DEFAULT_SERVICE = "dr"     # OpenAI/ChatGPT (hero-sms 的 code; 非 sms-activate 标准 "oai")
DEFAULT_MAX_PRICE = 0.05   # USD

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
IMPERSONATE = "chrome146"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


# ============================================================
# 低层 HTTP
# ============================================================

def _call(api_key: str, params: dict, timeout: int = 20) -> Any:
    """统一调用. 返回 (status_code, parsed_or_text)。

    hero-sms 的响应有时是 JSON, 有时是纯文本 (ACCESS_NUMBER:xx, NO_NUMBERS, ...),
    所以这里返回二者之一供上层判断。
    """
    p = {"api_key": api_key, **params}
    r = http.get(HEROSMS_API, params=p,
                 headers={"User-Agent": UA, "Accept": "application/json"},
                 timeout=timeout, impersonate=IMPERSONATE)
    body = r.text
    parsed: Any = body
    if body and body[:1] in ("{", "["):
        try:
            parsed = r.json()
        except Exception:
            parsed = body
    return r.status_code, parsed


# ============================================================
# 单接口包装
# ============================================================

def get_balance(api_key: str) -> float:
    status, body = _call(api_key, {"action": "getBalance"})
    if status != 200:
        raise SmsProviderError(f"getBalance HTTP {status}: {body!r}")
    if isinstance(body, dict) and "balance" in body:
        return float(body["balance"])
    if isinstance(body, str) and body.startswith("ACCESS_BALANCE:"):
        return float(body.split(":", 1)[1])
    raise SmsProviderError(f"getBalance unexpected body: {body!r}")


def get_prices(api_key: str, service: Optional[str] = None,
               country: Optional[int] = None) -> dict:
    """返回嵌套 dict: {country_id: {service_code: {cost,count,physicalCount}}}.

    service/country 都可省略, 省略时返回该维度的全量。
    """
    params: dict = {"action": "getPrices"}
    if service:
        params["service"] = service
    if country is not None:
        params["country"] = country
    status, body = _call(api_key, params)
    if status != 200:
        raise SmsProviderError(f"getPrices HTTP {status}: {body!r}")
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0]
    if isinstance(body, dict):
        return body
    raise SmsProviderError(f"getPrices unexpected: {body!r}")


def cheapest_price(prices: dict) -> Optional[tuple[str, float, int]]:
    """从嵌套 getPrices 返回挑最便宜且有库存的。返回 ("country/service", cost, count)。"""
    best: Optional[tuple[str, float, int]] = None
    for c_id, svcs in (prices or {}).items():
        if not isinstance(svcs, dict):
            continue
        for svc_code, info in svcs.items():
            if not isinstance(info, dict):
                continue
            cost = info.get("cost")
            count = info.get("count") or info.get("physicalCount") or 0
            if cost is None or count <= 0:
                continue
            c = float(cost)
            if best is None or c < best[1]:
                best = (f"{c_id}/{svc_code}", c, int(count))
    return best


def get_countries(api_key: str) -> list[dict]:
    status, body = _call(api_key, {"action": "getCountries"})
    if status != 200:
        raise SmsProviderError(f"getCountries HTTP {status}: {body!r}")
    if isinstance(body, list):
        return body
    raise SmsProviderError(f"getCountries unexpected: {body!r}")


def get_services_list(api_key: str, country: Optional[int] = None,
                      lang: str = "en") -> list[dict]:
    params = {"action": "getServicesList", "lang": lang}
    if country is not None:
        params["country"] = country
    status, body = _call(api_key, params)
    if status != 200:
        raise SmsProviderError(f"getServicesList HTTP {status}: {body!r}")
    if isinstance(body, dict) and "services" in body:
        return body["services"]
    raise SmsProviderError(f"getServicesList unexpected: {body!r}")


def get_number_v2(api_key: str, *, service: str, country: int,
                  max_price: Optional[float] = None,
                  fixed_price: bool = True,
                  operator: Optional[str] = None,
                  phone_exception: Optional[str] = None,
                  ref: Optional[str] = None) -> dict:
    """拿号. 没货时抛 NoNumberAvailable, 余额不足/参数错抛 AcquireFailed。"""
    params: dict = {"action": "getNumberV2",
                    "service": service, "country": country}
    if max_price is not None:
        params["maxPrice"] = max_price
    if fixed_price:
        params["fixedPrice"] = "true"
    if operator:
        params["operator"] = operator
    if phone_exception:
        params["phoneException"] = phone_exception
    if ref:
        params["ref"] = ref

    status, body = _call(api_key, params)
    if status == 404:
        raise NoNumberAvailable(f"getNumberV2 404 (no stock): {body!r}")
    if status == 402:
        raise AcquireFailed(f"getNumberV2 402 余额不足: {body!r}")
    if status == 401:
        raise AcquireFailed(f"getNumberV2 401 api_key 无效: {body!r}")
    if status >= 400:
        raise AcquireFailed(f"getNumberV2 HTTP {status}: {body!r}")

    if isinstance(body, str):
        if body.startswith("NO_NUMBERS"):
            raise NoNumberAvailable(body)
        if body.startswith("NO_BALANCE"):
            raise AcquireFailed(body)
        raise AcquireFailed(f"getNumberV2 unexpected text: {body!r}")
    if not isinstance(body, dict) or "phoneNumber" not in body:
        raise AcquireFailed(f"getNumberV2 unexpected body: {body!r}")
    return body


def get_status_v2(api_key: str, activation_id: str) -> Optional[dict]:
    """200 → dict; 文本 STATUS_WAIT_CODE → None; 其它抛错。"""
    status, body = _call(api_key,
                         {"action": "getStatusV2", "id": activation_id})
    if status != 200:
        raise SmsProviderError(f"getStatusV2 HTTP {status}: {body!r}")
    if isinstance(body, str):
        if body.startswith("STATUS_WAIT_CODE") or body.startswith("STATUS_WAIT_RETRY"):
            return None
        if body.startswith("STATUS_CANCEL"):
            raise SmsProviderError(f"activation cancelled: {body}")
        if body.startswith("STATUS_OK"):
            # "STATUS_OK:1234"
            code = body.split(":", 1)[1] if ":" in body else ""
            return {"verificationType": 2, "sms": {"code": code, "text": ""}}
        raise SmsProviderError(f"getStatusV2 unexpected text: {body!r}")
    if isinstance(body, dict):
        return body
    raise SmsProviderError(f"getStatusV2 unexpected: {body!r}")


def get_all_sms(api_key: str, activation_id: str, *,
                size: int = 20, page: int = 1) -> list[dict]:
    """拉某 activation 上的全部 SMS (按 SMS id 去重靠这个).

    409 → ActivationInactive (号已 finish/cancel/expired).
    返回 data[] 数组, 每条含 {id, code, text, date, phoneFrom, service, type}。
    """
    status, body = _call(api_key, {
        "action": "getAllSms", "id": activation_id, "size": size, "page": page,
    })
    if status == 409:
        raise ActivationInactive(f"getAllSms 409: {body!r}")
    if status == 404:
        raise ActivationInactive(f"getAllSms 404 (not found): {body!r}")
    if status != 200:
        raise SmsProviderError(f"getAllSms HTTP {status}: {body!r}")
    if isinstance(body, dict) and "data" in body:
        return body["data"] or []
    if isinstance(body, list):
        return body
    raise SmsProviderError(f"getAllSms unexpected: {body!r}")


def set_status(api_key: str, activation_id: str, status_code: int) -> str:
    """1=已发, 3=请求重发, 6=完成, 8=取消。"""
    status, body = _call(api_key, {"action": "setStatus",
                                   "id": activation_id, "status": status_code})
    if status >= 400:
        raise SmsProviderError(f"setStatus HTTP {status}: {body!r}")
    return body if isinstance(body, str) else json.dumps(body)


def cancel_activation(api_key: str, activation_id: str) -> None:
    status, body = _call(api_key, {"action": "cancelActivation",
                                   "id": activation_id})
    if status not in (200, 204):
        # 兼容 setStatus=8 fallback
        if status == 409:
            raise SmsProviderError(f"cancel rejected (already received SMS?): {body!r}")
        raise SmsProviderError(f"cancelActivation HTTP {status}: {body!r}")


def finish_activation(api_key: str, activation_id: str) -> None:
    status, body = _call(api_key, {"action": "finishActivation",
                                   "id": activation_id})
    if status not in (200, 204):
        raise SmsProviderError(f"finishActivation HTTP {status}: {body!r}")


def get_active_activations(api_key: str, start: int = 0,
                           limit: int = 100) -> list[dict]:
    """拉账号下还未结算的 activation 列表.

    实际响应是嵌套结构 (与文档不一致):
      {"activeActivations": {"affected_rows": N, "row": {...}, "rows": [...]}}
    每条 rows 含: activationId, phoneNumber, serviceCode, activationStatus,
    activationTime, activationCost, smsCode, smsText, estDate (到期时间),
    receiveSmsDate, finishDate, ...
    """
    status, body = _call(api_key, {"action": "getActiveActivations",
                                   "start": start, "limit": limit})
    if status != 200:
        raise SmsProviderError(f"getActiveActivations HTTP {status}: {body!r}")
    if not isinstance(body, dict):
        return []
    inner = body.get("activeActivations")
    # 形状 1: 嵌套 dict 含 rows[]
    if isinstance(inner, dict):
        rows = inner.get("rows")
        if isinstance(rows, list):
            return rows
        return []
    # 形状 2: 文档里写的直接是 list
    if isinstance(inner, list):
        return inner
    return []


# ============================================================
# Provider
# ============================================================

class HeroSmsProvider(SmsProvider):
    name = "herosms"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api_key = (cfg.get("herosms_api_key")
                        or os.environ.get("HEROSMS_API_KEY") or "")
        if not self.api_key:
            raise SmsProviderError(
                "herosms_api_key 未配置 (config.json 或 HEROSMS_API_KEY)"
            )
        self.default_country = int(cfg.get("herosms_country", DEFAULT_COUNTRY))
        self.default_service = cfg.get("herosms_service", DEFAULT_SERVICE)
        self.default_max_price = float(
            cfg.get("herosms_max_price", DEFAULT_MAX_PRICE))

    # ---- SmsProvider 接口 ----

    def acquire(self, *, country: Optional[int] = None,
                service: Optional[str] = None,
                max_price: Optional[float] = None,
                fixed_price: bool = True,
                operator: Optional[str] = None,
                phone_exception: Optional[str] = None,
                **_) -> SmsSession:
        c = int(country) if country is not None else self.default_country
        s = service or self.default_service
        mp = self.default_max_price if max_price is None else float(max_price)
        try:
            info = get_number_v2(self.api_key, service=s, country=c,
                                 max_price=mp, fixed_price=fixed_price,
                                 operator=operator,
                                 phone_exception=phone_exception)
        except NoNumberAvailable:
            # 顺手补一个 cheapest 提示
            try:
                prices = get_prices(self.api_key, s, c)
                best = cheapest_price(prices)
            except Exception:
                best = None
            hint = ""
            if best:
                op, cost, cnt = best
                hint = (f"; 当前最低 ${cost:.4f} (operator={op}, 库存={cnt});"
                        f" 建议提高 max_price 或换 country/service")
            else:
                hint = "; getPrices 也没拿到任何报价, 建议换 country/service"
            raise NoNumberAvailable(
                f"hero-sms 没有满足 service={s} country={c} "
                f"maxPrice=${mp} 的号{hint}"
            )
        return SmsSession(
            provider=self.name,
            number=str(info["phoneNumber"]),
            handle=str(info["activationId"]),
            locale=str(info.get("countryCode", c)),
            cost=float(info.get("activationCost", 0) or 0),
            extra={
                "currency": info.get("currency"),
                "countryPhoneCode": info.get("countryPhoneCode"),
                "operator": info.get("activationOperator"),
                "activationEndTime": info.get("activationEndTime"),
                "service": s,
            },
        )

    def wait_otp(self, session: SmsSession, *,
                 regex: str = r"\b(\d{4,8})\b", timeout: int = 120,
                 poll_interval: int = 5,
                 log: Callable[[str], None] = print,
                 since_sms_ids: Optional[set] = None,
                 on_lease_lost: Optional[Callable[[], bool]] = None,
                 ) -> Optional[str]:
        """轮询 OTP. 复用号场景必须传 since_sms_ids 排除历史 SMS。

        Returns:
            code 字符串, 收不到/过期/租约丢失返回 None。
            如需配套拿到 sms_id, 用 wait_otp_with_id。

        on_lease_lost: 每次 poll 调一次, 返回 True 表示租约丢了立刻退出。
        """
        result = self.wait_otp_with_id(
            session, regex=regex, timeout=timeout, poll_interval=poll_interval,
            log=log, since_sms_ids=since_sms_ids, on_lease_lost=on_lease_lost,
        )
        return result[0] if result else None

    def wait_otp_with_id(
        self, session: SmsSession, *,
        regex: str = r"\b(\d{4,8})\b", timeout: int = 120,
        poll_interval: int = 5,
        log: Callable[[str], None] = print,
        since_sms_ids: Optional[set] = None,
        on_lease_lost: Optional[Callable[[], bool]] = None,
    ) -> Optional[tuple]:
        """同 wait_otp 但返回 (code, sms_id) 用于 phone_pool 去重。

        ActivationInactive → 立刻 None (号 dead).
        """
        pat = re.compile(regex)
        seen = set(since_sms_ids or ())
        deadline = time.time() + timeout
        # 通知服务"号已就绪等收码"
        try:
            set_status(self.api_key, session.handle, 1)
        except Exception as e:
            log(f"[herosms] setStatus=1 failed (non-fatal): {e}")

        while time.time() < deadline:
            if on_lease_lost and on_lease_lost():
                log(f"[herosms] {session.handle} lease lost, abort wait_otp")
                return None
            try:
                msgs = get_all_sms(self.api_key, session.handle,
                                    size=20, page=1)
            except ActivationInactive as e:
                log(f"[herosms] {session.handle} inactive: {e}")
                return None
            except SmsProviderError as e:
                log(f"[herosms] getAllSms error: {e}")
                time.sleep(poll_interval)
                continue

            new_msgs = [m for m in msgs if str(m.get("id")) not in seen]
            if not new_msgs:
                log(f"[herosms] {session.handle} WAIT_CODE "
                    f"(seen {len(msgs)} msgs, all old)")
                time.sleep(poll_interval)
                continue

            # 取最新一条 (按 date 排序, 字符串 ISO 8601 直接比)
            new_msgs.sort(key=lambda m: m.get("date") or "", reverse=True)
            for m in new_msgs:
                sms_id = str(m.get("id") or "")
                code_field = m.get("code") or ""
                text = m.get("text") or ""
                log(f"[herosms] new sms id={sms_id} code={code_field!r} "
                    f"text={text!r}")
                for src in (code_field, text):
                    if not src:
                        continue
                    pm = pat.search(str(src))
                    if pm:
                        code = pm.group(1) if pm.groups() else pm.group(0)
                        return code, sms_id
                seen.add(sms_id)  # 新 SMS 但抠不出码, 标记已看过避免下轮重复
            time.sleep(poll_interval)
        return None

    def release_ok(self, session: SmsSession) -> None:
        try:
            finish_activation(self.api_key, session.handle)
        except SmsProviderError as e:
            # 已经 inactive (例如号池已自己 finish 过) → 吞掉
            if "409" in str(e) or "404" in str(e):
                return
            raise

    def release_no_sms(self, session: SmsSession) -> None:
        try:
            cancel_activation(self.api_key, session.handle)
            return
        except SmsProviderError as e:
            msg = str(e)
            # hero-sms 强制号最少占用 120s 才允许取消, 早于此则直接失败
            # 这种情况让号自然过期 (默认 ~20min 后退款), 不阻断换号
            if "EARLY_CANCEL_DENIED" in msg or "Minimum activation period" in msg:
                return
        # 兜底再试 setStatus=8 (同样可能 EARLY_CANCEL_DENIED, 也吞掉)
        try:
            set_status(self.api_key, session.handle, 8)
        except SmsProviderError as e:
            msg = str(e)
            if "EARLY_CANCEL_DENIED" in msg or "Minimum activation period" in msg:
                return
            raise

    def release_bad(self, session: SmsSession, reason: str = "") -> None:
        # 号没毛病但目标平台拒绝: 走 cancel 退款
        self.release_no_sms(session)


# ============================================================
# CLI
# ============================================================

def cmd_verify(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    if not api_key:
        print("[!] 缺 herosms_api_key", file=sys.stderr); sys.exit(2)
    print("# Countries (filter: thai/泰):")
    for c in get_countries(api_key):
        eng = (c.get("eng") or "").lower()
        chn = c.get("chn") or ""
        rus = (c.get("rus") or "").lower()
        if "thai" in eng or "泰" in chn or "таи" in rus:
            print(f"  id={c.get('id')}  eng={c.get('eng')}  chn={chn}")
    print("\n# Services (filter: openai/chatgpt):")
    services = get_services_list(api_key, country=args.country, lang="en")
    for s in services:
        n = (s.get("name") or "").lower()
        if "openai" in n or "chatgpt" in n or s.get("code") in ("oai",):
            print(f"  code={s.get('code')}  name={s.get('name')}")


def cmd_balance(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    print(f"${get_balance(api_key):.4f}")


def cmd_prices(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    service = args.service or cfg.get("herosms_service", DEFAULT_SERVICE)
    if args.all:
        country: Optional[int] = None
    elif args.country is not None:
        country = args.country
    else:
        country = int(cfg.get("herosms_country", DEFAULT_COUNTRY))
    prices = get_prices(api_key, service, country)
    if args.json:
        print(json.dumps(prices, ensure_ascii=False, indent=2))
        return

    # 把嵌套 {country: {service: info}} 拍平
    rows: list[tuple[str, str, float, int, int]] = []
    for c_id, svcs in prices.items():
        if not isinstance(svcs, dict):
            continue
        for svc_code, info in svcs.items():
            if not isinstance(info, dict):
                continue
            rows.append((
                str(c_id), str(svc_code),
                float(info.get("cost") or 0),
                int(info.get("count") or 0),
                int(info.get("physicalCount") or 0),
            ))
    # 仅显示有库存的; 无库存的算入但排在底部 (count desc 优先)
    rows.sort(key=lambda r: (-(1 if (r[3] or r[4]) else 0), r[2], -r[3]))

    print(f"# service={service or 'ALL'} "
          f"country={country if country is not None else 'ALL'}")
    print(f"{'country':<8} {'service':<8} {'cost':>8} {'count':>8} {'physical':>10}")
    for c_id, svc, cost, cnt, phys in rows:
        print(f"{c_id:<8} {svc:<8} {cost:>8.4f} {cnt:>8} {phys:>10}")
    if not rows:
        print("(no prices)")

    best = cheapest_price(prices)
    if best:
        tag, cost, cnt = best
        print(f"\n# cheapest in-stock: ${cost:.4f}  ({tag})  count={cnt}")
    max_price = (args.max_price if args.max_price is not None
                 else float(cfg.get("herosms_max_price", DEFAULT_MAX_PRICE)))
    if best and best[1] > max_price:
        print(f"# WARN: cheapest ${best[1]:.4f} > max_price ${max_price:.4f}",
              file=sys.stderr)


def cmd_acquire(args, cfg):
    provider = HeroSmsProvider(cfg)
    try:
        sess = provider.acquire(
            country=args.country, service=args.service,
            max_price=args.max_price,
            fixed_price=not args.no_fixed_price,
            operator=args.operator,
        )
    except NoNumberAvailable as e:
        print(f"[!] {e}", file=sys.stderr); sys.exit(3)
    except AcquireFailed as e:
        print(f"[!] {e}", file=sys.stderr); sys.exit(4)
    out = {
        "activationId": sess.handle, "number": sess.number,
        "cost": sess.cost, "locale": sess.locale, **sess.extra,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"id={sess.handle}  number={sess.number}  cost=${sess.cost}")


def cmd_wait_otp(args, cfg):
    provider = HeroSmsProvider(cfg)
    sess = SmsSession(provider="herosms", number=args.number or "?",
                      handle=args.id, locale=None, cost=None)
    code = provider.wait_otp(sess, regex=args.regex, timeout=args.timeout,
                             poll_interval=args.interval,
                             log=lambda s: print(s, file=sys.stderr))
    if code:
        print(code)
    else:
        sys.exit(3)


def cmd_cancel(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    cancel_activation(api_key, args.id)
    print("ok")


def cmd_finish(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    finish_activation(api_key, args.id)
    print("ok")


def cmd_all_sms(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    msgs = get_all_sms(api_key, args.id, size=args.size, page=args.page)
    if args.json:
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
        return
    if not msgs:
        print("(no sms)")
        return
    print(f"{'sms_id':<14} {'date':<25} {'from':<14} {'code':<10} text")
    for m in msgs:
        print(f"{str(m.get('id','')):<14} "
              f"{m.get('date',''):<25} "
              f"{(m.get('phoneFrom','') or '')[:13]:<14} "
              f"{(m.get('code','') or '')[:10]:<10} "
              f"{(m.get('text','') or '')[:80]}")


def cmd_active(args, cfg):
    api_key = cfg.get("herosms_api_key") or os.environ.get("HEROSMS_API_KEY")
    rows = get_active_activations(api_key)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("(none)")
        return
    print(f"{'id':<12} {'service':<8} {'number':<16} {'cost':>7} {'status':<6} time")
    for r in rows:
        print(f"{r.get('activationId',''):<12} "
              f"{r.get('serviceCode',''):<8} "
              f"{r.get('phoneNumber',''):<16} "
              f"{float(r.get('activationCost',0) or 0):>7.4f} "
              f"{r.get('activationStatus',''):<6} "
              f"{r.get('activationTime','')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify", help="校对 country/service ID")
    p.add_argument("--country", type=int, default=None,
                   help="过滤 services 的 country, 默认全量")

    sub.add_parser("balance", help="余额")

    p = sub.add_parser("prices", help="当前 service+country 报价表")
    p.add_argument("--service", help="不传用 config (默认 dr); 传空可拉所有 service")
    p.add_argument("--country", type=int, help="不传用 config (默认 52)")
    p.add_argument("--all", action="store_true",
                   help="拉所有 country (覆盖 --country / config)")
    p.add_argument("--max-price", type=float, default=None,
                   help="高于此值时打 WARN")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("acquire", help="拿一个号")
    p.add_argument("--service")
    p.add_argument("--country", type=int)
    p.add_argument("--max-price", type=float, default=None)
    p.add_argument("--operator")
    p.add_argument("--no-fixed-price", action="store_true",
                   help="允许接口在没货时用更高价的号 (默认 fixedPrice=true)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("wait-otp", help="轮询 OTP")
    p.add_argument("id")
    p.add_argument("--number", help="仅展示用")
    p.add_argument("--regex", default=r"\b(\d{4,8})\b")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--interval", type=int, default=5)

    p = sub.add_parser("cancel", help="取消并退款")
    p.add_argument("id")

    p = sub.add_parser("finish", help="完成 (扣费)")
    p.add_argument("id")

    p = sub.add_parser("active", help="进行中的 activation")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("all-sms", help="拉某 activation 的所有 SMS (按 id 去重靠这个)")
    p.add_argument("id")
    p.add_argument("--size", type=int, default=20)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--json", action="store_true")

    args = ap.parse_args()
    cfg = load_config()
    handlers = {
        "verify": cmd_verify, "balance": cmd_balance, "prices": cmd_prices,
        "acquire": cmd_acquire, "wait-otp": cmd_wait_otp,
        "cancel": cmd_cancel, "finish": cmd_finish, "active": cmd_active,
        "all-sms": cmd_all_sms,
    }
    handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
