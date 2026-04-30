#!/usr/bin/env python3

"""
addy.io 别名邮箱池

适用场景:
    在 addy.io 把某个自有域名 (如 103311.com) 添加为 custom domain
    并打开 catch-all, 所有 *@103311.com 转发到 addy.io 配置的 recipient
    (recipient 又指向你的真实 IMAP 邮箱, 例如 QQ / 2925).

    本模块在每次注册前显式调 addy.io API 创建别名 (而非纯靠 catchall),
    这样能拿到 alias_id, 后续可 deactivate / delete; 真正的 OTP 收信
    仍然走 QQMailPool (复用现有 IMAP 长连 / IDLE 推送).

addy.io 文档: https://app.addy.io/docs/
"""

import json
import threading
import urllib.error
import urllib.request
from typing import Callable, Optional

from .qq_mail_pool import QQMailPool, get_pool as _get_qq_mail_pool
from . import paths as _paths


_DEFAULT_BASE_URL = "https://app.addy.io"


class AddyClient:
    """addy.io HTTP API 极简客户端 (仅别名增/删)"""

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL,
                 timeout: float = 20.0):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        url = f"{self.base_url}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
                if not payload:
                    return None
                try:
                    return json.loads(payload.decode("utf-8"))
                except Exception:
                    return None
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise RuntimeError(
                f"addy.io {method} {path} -> HTTP {e.code}: {err_body[:300]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"addy.io {method} {path} 网络错误: {e}") from e

    def list_recipients(self) -> list:
        resp = self._request("GET", "/api/v1/recipients") or {}
        return list(resp.get("data") or [])

    def create_alias(self, domain: str, local_part: Optional[str] = None,
                     recipient_ids: Optional[list] = None,
                     description: str = "",
                     format: str = "custom") -> dict:
        body = {"domain": domain, "format": format}
        if format == "custom":
            if not local_part:
                raise ValueError("format=custom 需要 local_part")
            body["local_part"] = local_part
        if recipient_ids:
            body["recipient_ids"] = list(recipient_ids)
        if description:
            body["description"] = description
        resp = self._request("POST", "/api/v1/aliases", body=body) or {}
        return resp.get("data") or {}

    def deactivate_alias(self, alias_id: str):
        self._request("DELETE", f"/api/v1/active-aliases/{alias_id}")

    def delete_alias(self, alias_id: str):
        self._request("DELETE", f"/api/v1/aliases/{alias_id}")


class AddyMailPool:
    """addy.io 别名 + 现有 IMAP 收件池 (QQMailPool) 的组合.

    暴露的方法和 QQMailPool 一致, 因此可直接被 chatgpt_register.py 当作
    `qq_pool` 使用 (acquire_email / release / wait_for_otp / ...).
    """

    # addy.io 支持的 format 取值
    _VALID_FORMATS = {
        "custom", "uuid", "random_characters", "random_words",
        "random_male_name", "random_female_name", "random_noun",
    }

    def __init__(self, client: AddyClient, domain: str,
                 imap_pool: QQMailPool,
                 recipient_ids: Optional[list] = None,
                 description: str = "",
                 delete_on_release: bool = False,
                 deactivate_on_release: bool = False,
                 format: str = "custom",
                 log: Optional[Callable[[str], None]] = None):
        self.client = client
        self.domain = domain.lower().lstrip("@")
        self._imap = imap_pool
        self.recipient_ids = list(recipient_ids or [])
        self.description = description or ""
        self.delete_on_release = bool(delete_on_release)
        self.deactivate_on_release = bool(deactivate_on_release)
        fmt = (format or "custom").strip().lower()
        if fmt not in self._VALID_FORMATS:
            fmt = "custom"
        self.format = fmt
        self.log = log
        # address(lower) -> alias_id (addy 返回的 uuid)
        self._alias_ids: dict[str, str] = {}
        self._alias_lock = threading.Lock()

    # ---- 生命周期 ----
    def start(self, **kwargs):
        # 实际收信由 imap_pool 负责; get_pool() 已经 start 过, 这里是幂等保护
        self._imap.start(**kwargs)

    def stop(self):
        # imap_pool 是共享的, 不在这里 stop
        pass

    # ---- 主流程 ----
    def acquire_email(self, domain: Optional[str] = None,
                      base_address: Optional[str] = None) -> str:
        """调 addy API 创建别名 → 返回完整地址.

        format=custom: 复用 IMAP 池的人名生成器作为 local_part (要求该 domain 是
                       addy 上你自己加的 custom domain 或 username 子域)
        format=其它   : 不传 local_part, 让 addy 自己生成 (共享域名 anonaddy.me 必须走这条路)
        """
        target_domain = (domain or self.domain).lower().lstrip("@")
        if self.format == "custom":
            return self._acquire_custom(target_domain, base_address)
        return self._acquire_random(target_domain)

    def _acquire_custom(self, target_domain: str,
                        base_address: Optional[str]) -> str:
        # 复用 imap pool 的随机名生成 + 去重 + watched 注册
        addr = self._imap.acquire_email(domain=target_domain, base_address=base_address)
        local_part, addr_domain = addr.split("@", 1)
        try:
            alias = self.client.create_alias(
                domain=addr_domain,
                local_part=local_part,
                recipient_ids=self.recipient_ids,
                description=self.description,
                format="custom",
            )
            alias_id = alias.get("id") or ""
            email_returned = alias.get("email") or addr
            if alias_id:
                with self._alias_lock:
                    self._alias_ids[email_returned.lower()] = alias_id
                self._log(f"创建别名 {email_returned} (id={alias_id[:8]}…)")
            return email_returned
        except Exception as e:
            self._log(f"创建别名 {addr} 失败, 回退到 catchall: {e}")
            return addr

    def _acquire_random(self, target_domain: str) -> str:
        """共享域名走随机格式: 让 addy 自己生成 local_part, 拿到 email 后再注册到 IMAP 池"""
        try:
            alias = self.client.create_alias(
                domain=target_domain,
                recipient_ids=self.recipient_ids,
                description=self.description,
                format=self.format,
            )
        except Exception as e:
            # 随机格式失败没有 catchall 可兜底, 直接抛
            raise RuntimeError(f"addy.io 创建别名失败 (format={self.format}): {e}") from e
        alias_id = alias.get("id") or ""
        email = (alias.get("email") or "").strip().lower()
        if not email:
            raise RuntimeError(f"addy.io 返回缺少 email 字段: {alias}")
        # 把这个 addy 生成的地址登记到底层 IMAP 池, 让收件分桶 + 旧邮件过滤都跟上
        self._imap.register_address(email)
        with self._imap._inbox_lock:
            self._imap._inbox.setdefault(email, [])
        if alias_id:
            with self._alias_lock:
                self._alias_ids[email] = alias_id
            self._log(f"创建别名 {email} (id={alias_id[:8]}…, format={self.format})")
        return email

    def release(self, address: Optional[str]):
        if not address:
            return
        addr_lc = address.lower()
        with self._alias_lock:
            alias_id = self._alias_ids.pop(addr_lc, None)
        # 让 IMAP 池清理 inbox / watched
        try:
            self._imap.release(address)
        except Exception:
            pass
        if not alias_id:
            return
        try:
            if self.delete_on_release:
                self.client.delete_alias(alias_id)
                self._log(f"删除别名 {address} (id={alias_id[:8]}…)")
            elif self.deactivate_on_release:
                self.client.deactivate_alias(alias_id)
                self._log(f"反激活别名 {address} (id={alias_id[:8]}…)")
        except Exception as e:
            # 失败不抛: 注册主流程不应被清理逻辑影响
            self._log(f"清理别名 {address} 失败 (忽略): {e}")

    # ---- 透传给底层 IMAP 池 ----
    def register_address(self, address):
        self._imap.register_address(address)

    def unregister_address(self, address):
        self._imap.unregister_address(address)

    def get_messages_since(self, address, since_ts=0.0):
        return self._imap.get_messages_since(address, since_ts=since_ts)

    def wait_for_otp(self, address, timeout=120, since_ts=None,
                     exclude_codes=None, poll_interval=2):
        return self._imap.wait_for_otp(
            address,
            timeout=timeout,
            since_ts=since_ts,
            exclude_codes=exclude_codes,
            poll_interval=poll_interval,
        )

    # ---- 日志 ----
    def _log(self, msg: str):
        line = f"[AddyPool] {msg}"
        if self.log:
            try:
                self.log(line)
                return
            except Exception:
                pass
        print(line)


# ---- 池缓存 ----
_pool_instances: dict[tuple, AddyMailPool] = {}
_pool_init_lock = threading.Lock()


def get_pool(addy_config: dict, imap_pool_config: dict,
             log: Optional[Callable[[str], None]] = None) -> Optional[AddyMailPool]:
    """构造/复用 AddyMailPool.

    addy_config:
        api_key       (必填) addy.io Bearer token
        domain        (必填) 已在 addy.io 添加的 custom domain (如 103311.com)
        base_url      可选, 默认 https://app.addy.io
        recipient_ids 可选, 别名转发到的 recipient uuid 列表; 留空 = addy 默认
        description   可选, 创建别名时的备注
        delete_on_release / deactivate_on_release 可选

    imap_pool_config: 透传给 qq_mail_pool.get_pool 的 IMAP 连接 / domain 配置
        (domain 应等于 addy_config["domain"], 这样收件分桶才匹配)
    """
    if not addy_config or not imap_pool_config:
        return None
    api_key = (addy_config.get("api_key") or "").strip()
    domain = (addy_config.get("domain") or "").strip().lower().lstrip("@")
    if not (api_key and domain):
        return None
    base_url = (addy_config.get("base_url") or _DEFAULT_BASE_URL).rstrip("/")
    recipient_ids = tuple(addy_config.get("recipient_ids") or [])
    fmt = (addy_config.get("format") or "custom").strip().lower()
    cache_key = (
        api_key,
        base_url,
        domain,
        recipient_ids,
        fmt,
        imap_pool_config.get("mail_imap_user", ""),
        imap_pool_config.get("mail_imap_folder", "INBOX"),
    )
    with _pool_init_lock:
        existing = _pool_instances.get(cache_key)
        if existing is not None:
            return existing
        imap_pool = _get_qq_mail_pool(imap_pool_config, log=log)
        if imap_pool is None:
            return None
        client = AddyClient(api_key=api_key, base_url=base_url)
        pool = AddyMailPool(
            client=client,
            domain=domain,
            imap_pool=imap_pool,
            recipient_ids=list(recipient_ids),
            description=str(addy_config.get("description") or ""),
            delete_on_release=bool(addy_config.get("delete_on_release", False)),
            deactivate_on_release=bool(addy_config.get("deactivate_on_release", False)),
            format=fmt,
            log=log,
        )
        pool.start()
        _pool_instances[cache_key] = pool
        return pool


# ---- CLI 自检 ----
if __name__ == "__main__":
    import sys

    cfg_path = _paths.config_path()
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    sources = [s for s in (cfg.get("email_sources") or [])
               if isinstance(s, dict) and (s.get("type") or "").lower() == "addy"]
    if not sources:
        print("config.json 里没找到 type=addy 的 email_source")
        sys.exit(1)
    src = sources[0]

    # 子命令: 列出当前账户的 recipients (含 uuid), 方便填到 config.recipient_ids
    if len(sys.argv) > 1 and sys.argv[1] == "recipients":
        api_key = src.get("api_key", "")
        if not api_key:
            print("config.json email_sources[].api_key 为空, 无法调用 addy.io API")
            sys.exit(1)
        client = AddyClient(api_key=api_key,
                            base_url=src.get("base_url") or _DEFAULT_BASE_URL)
        try:
            items = client.list_recipients()
        except Exception as e:
            print(f"调用失败: {e}")
            sys.exit(1)
        if not items:
            print("当前账户没有任何 recipient (请先在 addy.io 后台添加)")
            sys.exit(0)
        print(f"共 {len(items)} 个 recipient:\n")
        for item in items:
            verified = item.get("email_verified_at") or "(未验证)"
            print(f"  id      : {item.get('id')}")
            print(f"  email   : {item.get('email')}")
            print(f"  verified: {verified}")
            print(f"  default : {item.get('default_recipient', False)}")
            print()
        sys.exit(0)

    profiles = {p.get("key"): p for p in (cfg.get("imap_profiles") or [])
                if isinstance(p, dict)}
    profile = profiles.get(src.get("receiver"))
    if not profile:
        print(f"找不到 receiver={src.get('receiver')} 的 imap_profile")
        sys.exit(1)
    imap_cfg = {
        "mail_imap_host": profile["host"],
        "mail_imap_port": int(profile.get("port") or 993),
        "mail_imap_user": profile["user"],
        "mail_imap_password": profile.get("password") or profile.get("authcode") or "",
        "mail_imap_folder": profile.get("folder", "INBOX"),
        "mail_imap_security": profile.get("security", "auto"),
        "mail_domain": src["domain"],
        "mail_poll_interval": cfg.get("mail_poll_interval", 4),
        "mail_debug": True,
    }
    addy_cfg = {
        "api_key": src.get("api_key", ""),
        "domain": src["domain"],
        "base_url": src.get("base_url") or _DEFAULT_BASE_URL,
        "recipient_ids": src.get("recipient_ids") or [],
        "description": src.get("description") or "test",
        "format": src.get("format") or "custom",
    }
    pool = get_pool(addy_cfg, imap_cfg)
    if not pool:
        print("get_pool 返回 None, 检查 addy_config / imap_pool_config")
        sys.exit(1)
    addr = pool.acquire_email()
    print(f"\n创建别名: {addr}")
    print(f"→ 现在【你手动】给这个地址发一封带 6 位数字的邮件, 等 120s 抓取")
    code = pool.wait_for_otp(addr, timeout=120)
    print(f"\nOTP: {code}")
    pool.release(addr)
