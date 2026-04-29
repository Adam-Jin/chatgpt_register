#!/usr/bin/env python3

"""
ChatGPT 批量自动注册工具 (并发版)
依赖: pip install curl_cffi
功能: 使用 DuckMail 临时邮箱，并发自动注册 ChatGPT 账号，自动获取 OTP 验证码
"""

import argparse
import os
import re
import uuid
import json
import queue
import random
import string
import time
import sys
import atexit
import asyncio
import threading
import traceback
import secrets
import hashlib
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.error
import urllib.request
from typing import Any

from curl_cffi import requests as curl_requests
from log_config import DEFAULT_LOG_LEVEL, normalize_log_level, should_log

try:
    from sentinel_solver import SentinelSolver as EmbeddedSentinelSolver
except Exception as _sentinel_import_exc:
    EmbeddedSentinelSolver = None
    _EMBEDDED_SENTINEL_IMPORT_ERROR = _sentinel_import_exc
else:
    _EMBEDDED_SENTINEL_IMPORT_ERROR = None

try:
    from curl_cffi.requests import BrowserType
except Exception:
    BrowserType = None

try:
    from sms_provider import (
        AcquireFailed, NoNumberAvailable, SmsProviderError, get_provider,
    )
except Exception:  # 接码模块缺失时降级 (add_phone 阶段才会真的报错)
    get_provider = None
    SmsProviderError = Exception
    NoNumberAvailable = Exception
    AcquireFailed = Exception

try:
    from phone_pool import PhonePool, PhonePoolCapacityExhausted
except Exception:
    PhonePool = None
    PhonePoolCapacityExhausted = Exception

try:
    import monitor
except Exception:
    monitor = None

try:
    import questionary
except Exception as _questionary_import_exc:
    questionary = None
    _QUESTIONARY_IMPORT_ERROR = _questionary_import_exc
else:
    _QUESTIONARY_IMPORT_ERROR = None

try:
    from qq_mail_pool import get_pool as _get_qq_mail_pool, extract_otp as _qq_extract_otp
except Exception:
    _get_qq_mail_pool = None
    _qq_extract_otp = None

try:
    from addy_pool import get_pool as _get_addy_pool
except Exception:
    _get_addy_pool = None

# 全局线程锁
_print_lock = threading.Lock()
_file_lock = threading.Lock()


APP_BANNER_LINES = [
    "        .-''''-.",
    "       /  .--.  \\",
    "      /  /_  _\\  \\",
    "      | |(@)(@)| |",
    "      | |  __  | |",
    "      \\  \\_==_/  /",
    "       '._/  \\_.'",
    "       .--\\__/--.",
    "      /          \\",
]


class OAuthPendingRequired(RuntimeError):
    """OAuth 命中可补救分支，应写入 pending_oauth.txt 后续补跑。"""


_ANSI_RESET = "\033[0m"
_ANSI_LEVEL = {
    "debug": "\033[2;37m",
    "info": "\033[37m",
    "success": "\033[1;32m",
    "warn": "\033[1;33m",
    "error": "\033[1;31m",
}
_CURRENT_LOG_LEVEL = DEFAULT_LOG_LEVEL


def _stdout_supports_color() -> bool:
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _console_log(message: str, *, level: str = "info") -> None:
    if not should_log(level, _CURRENT_LOG_LEVEL):
        return
    text = str(message)
    if _stdout_supports_color():
        text = f"{_ANSI_LEVEL.get(level, _ANSI_LEVEL['info'])}{text}{_ANSI_RESET}"
    with _print_lock:
        print(text)


def _console_block(lines, *, level: str = "info") -> None:
    for line in lines:
        _console_log(line, level=level)


def _banner_text() -> str:
    return "\n".join(APP_BANNER_LINES)


def _print_banner(*, level: str = "info") -> None:
    _console_block(APP_BANNER_LINES, level=level)

# ================= 加载配置 =================
def _load_config():
    """从 config.json 加载配置，环境变量优先级更高"""
    config = {
        "total_accounts": 1,
        "duckmail_api_base": "https://api.duckmail.sbs",
        "duckmail_bearer": "",
        "default_email_source": "",
        "email_sources": [],
        "proxy": "",
        "output_file": "registered_accounts.txt",
        "enable_oauth": True,
        "oauth_required": True,
        # OAuth 流程命中 add_phone 时，是否允许通过 SMS 平台自动接码。
        # 默认关闭，避免静默消耗号码；显式开启后才会走 herosms/quackr。
        "oauth_add_phone_sms": False,
        "oauth_issuer": "https://auth.openai.com",
        "oauth_client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "oauth_redirect_uri": "http://localhost:1455/auth/callback",
        "ak_file": "ak.txt",
        "rk_file": "rk.txt",
        "max_workers": 1,
        "tui_enabled": False,
        "log_level": DEFAULT_LOG_LEVEL,
        "token_json_dir": "codex_tokens",
        "upload_api_url": "",
        "upload_api_token": "",
        "sentinel_solver_url": "http://127.0.0.1:5732",
        "sentinel_inprocess": True,
        "sentinel_solver_thread": 0,
        "sentinel_solver_headless": True,
        "sentinel_solver_channel": "chromium",
        "sentinel_solver_debug": False,
        # 接码 provider: "herosms" (推荐) / "quackr" / "" 关闭
        # 仅在 oauth_add_phone_sms=true 或 --oauth-add-phone-sms 时使用。
        "sms_provider": "herosms",
        "sms_max_retries": 3,
        "sms_wait_otp_timeout": 120,
        "sms_poll_interval": 5,
        "phone_max_active": 0,
        "phone_acquire_timeout": 60.0,
        # herosms 默认 (HeroSmsProvider 内部还会再读一次, 这里只为方便覆盖)
        "herosms_api_key": "",
        "herosms_country": 52,
        "herosms_service": "dr",
        "herosms_max_price": 0.05,
        "herosms_free_price": True,
        # landbridge: 链 xray(订阅节点) -> arxlabs gateway. landings 按并发数自动生成
        "landbridge": {
            "enabled": False,
            "subscription": {"path": "", "url": "", "node_name": ""},
            "gateway": {
                "host": "", "port": 0, "account": "", "password": "",
                "protocol": "http",
                "tls": False, "sni": "", "skip_cert_verify": False,
            },
            "proxy_user_template": {
                "country": "Rand",
                "state": "",
                "city": "",
                "ip_mode": "Sticky",
                "sticky_minutes": 5,
            },
            "cliproxy_token": "",
            "xray": {
                "local_socks_host": "127.0.0.1",
                "local_socks_port": 10808,
                "startup_timeout_seconds": 10,
                "loglevel": "error",
            },
            "server": {"bind": "127.0.0.1", "port": 0, "dial_timeout": 10},
        },
    }

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception as e:
            _console_log(f"⚠️ 加载 config.json 失败: {e}", level="warn")

    # 环境变量优先级更高
    config["duckmail_api_base"] = os.environ.get("DUCKMAIL_API_BASE", config["duckmail_api_base"])
    config["duckmail_bearer"] = os.environ.get("DUCKMAIL_BEARER", config["duckmail_bearer"])
    config["log_level"] = normalize_log_level(
        os.environ.get("CHATGPT_REGISTER_LOG_LEVEL", os.environ.get("LOG_LEVEL", config["log_level"])),
        default=config["log_level"],
    )
    config["proxy"] = os.environ.get("PROXY", config["proxy"])
    config["total_accounts"] = int(os.environ.get("TOTAL_ACCOUNTS", config["total_accounts"]))
    config["enable_oauth"] = os.environ.get("ENABLE_OAUTH", config["enable_oauth"])
    config["oauth_required"] = os.environ.get("OAUTH_REQUIRED", config["oauth_required"])
    config["oauth_add_phone_sms"] = os.environ.get(
        "OAUTH_ADD_PHONE_SMS",
        os.environ.get("OAUTH_ADD_PHONE_VIA_SMS", config["oauth_add_phone_sms"]),
    )
    config["oauth_issuer"] = os.environ.get("OAUTH_ISSUER", config["oauth_issuer"])
    config["oauth_client_id"] = os.environ.get("OAUTH_CLIENT_ID", config["oauth_client_id"])
    config["oauth_redirect_uri"] = os.environ.get("OAUTH_REDIRECT_URI", config["oauth_redirect_uri"])
    config["ak_file"] = os.environ.get("AK_FILE", config["ak_file"])
    config["rk_file"] = os.environ.get("RK_FILE", config["rk_file"])
    config["max_workers"] = int(os.environ.get("MAX_WORKERS", config["max_workers"]))
    config["token_json_dir"] = os.environ.get("TOKEN_JSON_DIR", config["token_json_dir"])
    config["upload_api_url"] = os.environ.get("UPLOAD_API_URL", config["upload_api_url"])
    config["upload_api_token"] = os.environ.get("UPLOAD_API_TOKEN", config["upload_api_token"])
    config["sentinel_solver_url"] = os.environ.get("SENTINEL_SOLVER_URL", config["sentinel_solver_url"])
    config["sentinel_inprocess"] = os.environ.get("SENTINEL_INPROCESS", config["sentinel_inprocess"])
    config["sentinel_solver_thread"] = int(os.environ.get("SENTINEL_SOLVER_THREAD", config["sentinel_solver_thread"]))
    config["sentinel_solver_headless"] = os.environ.get("SENTINEL_SOLVER_HEADLESS", config["sentinel_solver_headless"])
    config["sentinel_solver_channel"] = os.environ.get("SENTINEL_SOLVER_CHANNEL", config["sentinel_solver_channel"])
    config["sentinel_solver_debug"] = os.environ.get("SENTINEL_SOLVER_DEBUG", config["sentinel_solver_debug"])
    config["sms_provider"] = os.environ.get("SMS_PROVIDER", config["sms_provider"])
    config["herosms_api_key"] = os.environ.get("HEROSMS_API_KEY", config["herosms_api_key"])
    config["phone_max_active"] = int(os.environ.get("PHONE_MAX_ACTIVE", config["phone_max_active"] or 0))
    config["phone_acquire_timeout"] = float(os.environ.get("PHONE_ACQUIRE_TIMEOUT", config["phone_acquire_timeout"]))

    return config


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_interactive():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _normalize_questionary_choices(choices):
    normalized = []
    for item in choices:
        if isinstance(item, dict):
            normalized.append({
                "title": str(item.get("title") or item.get("name") or item.get("value") or ""),
                "value": item.get("value"),
            })
        else:
            normalized.append({"title": str(item), "value": item})
    return [item for item in normalized if item["title"]]


def _prompt_select(message: str, choices, default: Any = None):
    normalized = _normalize_questionary_choices(choices)
    if questionary is not None:
        q_choices = [questionary.Choice(title=item["title"], value=item["value"]) for item in normalized]
        try:
            result = questionary.select(message, choices=q_choices, default=default).ask()
            if result is None:
                raise KeyboardInterrupt
            return result
        except Exception as e:
            _console_log(f"[Warn] questionary.select 失败，回退到 input: {e}", level="warn")
    for idx, item in enumerate(normalized, start=1):
        _console_log(f"[{idx}] {item['title']}")
    raw = input(f"{message}: ").strip()
    if raw.isdigit():
        pick = int(raw)
        if 1 <= pick <= len(normalized):
            return normalized[pick - 1]["value"]
    if not raw and default is None and normalized:
        return normalized[0]["value"]
    return default


def _prompt_text(
    message: str,
    default: str = "",
    *,
    instruction: str = "",
    placeholder_default: bool = False,
) -> str:
    if questionary is not None:
        try:
            prompt_default = default or ""
            extra_kwargs = {}
            prompt_instruction = instruction or None
            if placeholder_default and default:
                prompt_default = ""
                extra_kwargs["placeholder"] = [("fg:#7a7a7a", str(default))]
                if not prompt_instruction:
                    prompt_instruction = "留空使用默认值"
            result = questionary.text(
                message,
                default=prompt_default,
                instruction=prompt_instruction,
                **extra_kwargs,
            ).ask()
            if result is None:
                raise KeyboardInterrupt
            return (result or "").strip()
        except Exception as e:
            _console_log(f"[Warn] questionary.text 失败，回退到 input: {e}", level="warn")
    prompt = f"{message}"
    if instruction:
        prompt += f" ({instruction})"
    elif default:
        prompt += f" (默认 {default})"
    prompt += ": "
    return input(prompt).strip()


def _prompt_positive_int(message: str, default: int) -> int:
    raw = _prompt_text(
        message,
        str(default),
        instruction=f"直接输入数字，留空默认 {default}",
        placeholder_default=True,
    ).strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return default


def _prompt_oauth_add_phone_sms(default: bool = False, *, label: str = "OAuth") -> bool:
    return _prompt_confirm(
        f"{label} 命中 add_phone 时自动通过短信接码?",
        default=default,
    )


def _prompt_confirm(message: str, default: bool = True) -> bool:
    if questionary is not None:
        try:
            result = questionary.confirm(message, default=default).ask()
            if result is None:
                raise KeyboardInterrupt
            return bool(result)
        except Exception as e:
            _console_log(f"[Warn] questionary.confirm 失败，回退到 input: {e}", level="warn")
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{message} ({suffix}): ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "1", "true"}


# ---------------- landbridge 启动向导 ----------------

def _lb_http_post_json(url: str, form: dict, timeout: float = 10.0):
    import urllib.parse
    import urllib.request
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en,zh;q=0.9,zh-CN;q=0.8",
            "Origin": "https://dash.cliproxy.com",
            "Referer": "https://dash.cliproxy.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _lb_fetch_countries(token: str):
    data = _lb_http_post_json(
        "https://api.cliproxy.com/v2/country",
        {"cate": "traffic", "lang": "zh", "token": token},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"cliproxy /v2/country code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or []


def _lb_fetch_cities(token: str, country: str, state: str):
    data = _lb_http_post_json(
        "https://api.cliproxy.com/v1/traffic/city",
        {"country": country, "state": state, "lang": "zh", "token": token},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"cliproxy /v1/traffic/city code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or []


def _lb_pick(prompt: str, items, label_fn, value_fn, current_value, *, allow_keep=True):
    """fuzzy single-select。优先用 InquirerPy (输入即过滤, ↑↓ 选, Enter 确认),
    没装则回退到编号列表。Enter 保留高亮项 (默认即 current_value)。"""
    try:
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice
    except ImportError:
        return _lb_pick_numbered(prompt, items, label_fn, value_fn, current_value, allow_keep=allow_keep)

    # 把当前值置顶, 这样默认高亮就是 current_value, 直接 Enter 即可保留
    # (注意: fuzzy 的 default= 是预填搜索文本, 不是预选项, 所以不能用)
    current_label = None
    ordered = []
    rest = []
    for it in items:
        if value_fn(it) == current_value and current_label is None:
            current_label = label_fn(it)
            ordered.append(it)
        else:
            rest.append(it)
    ordered.extend(rest)
    choices = [Choice(value=value_fn(it), name=label_fn(it)) for it in ordered]

    keep_hint = f"当前: {current_label or current_value or '空'}"
    try:
        picked = inquirer.fuzzy(
            message=prompt,
            choices=choices,
            instruction=f"(输入过滤, ↑↓ 选, Enter 确认, Ctrl-C 保留 | {keep_hint})",
            max_height="60%",
            border=True,
            mandatory=False,
        ).execute()
    except KeyboardInterrupt:
        return current_value
    if picked is None:
        return current_value
    return picked


def _lb_pick_numbered(prompt: str, items, label_fn, value_fn, current_value, *, allow_keep=True):
    """编号列表回退实现 (InquirerPy 缺失时使用)。"""
    current_label = None
    for it in items:
        if value_fn(it) == current_value:
            current_label = label_fn(it)
            break
    if allow_keep:
        suffix = f" (回车=保留 {current_label or current_value or '空'})"
    else:
        suffix = ""
    for idx, it in enumerate(items, start=1):
        marker = "  *" if value_fn(it) == current_value else "   "
        print(f"{marker}[{idx}] {label_fn(it)}")
    raw = input(f"{prompt}{suffix}: ").strip()
    if not raw:
        return current_value
    if raw.isdigit():
        pick = int(raw)
        if 1 <= pick <= len(items):
            return value_fn(items[pick - 1])
    print(f"  无效输入, 保留 {current_label or current_value or '空'}")
    return current_value


def _landbridge_interactive(no_prompt: bool):
    """启动时的 landbridge 交互向导。修改 _landbridge 内的 cfg, 必要时持久化。"""
    if no_prompt:
        if _landbridge.is_enabled():
            _console_log("[landbridge] --no-landbridge-prompt: 沿用 config 配置 (enabled=true)")
        else:
            _console_log("[landbridge] --no-landbridge-prompt: config.enabled=false, 跳过")
        return

    cfg = _landbridge.get_cfg()
    cur_enabled = bool(cfg.get("enabled"))
    enabled = _prompt_confirm(
        f"[landbridge] 是否启用? (当前 config: {cur_enabled})",
        default=cur_enabled,
    )
    if enabled != cur_enabled:
        _landbridge.set_enabled(enabled, persist=True)
    else:
        _landbridge.set_enabled(enabled, persist=False)
    if not enabled:
        _console_log("[landbridge] 已禁用, 走 config.proxy")
        return

    if not _prompt_confirm("[landbridge] 进入配置向导?", default=False):
        _console_log("[landbridge] 沿用 config.proxy_user_template")
        return

    token = (cfg.get("cliproxy_token") or "").strip()
    if not token:
        _console_log("[landbridge] config.landbridge.cliproxy_token 为空, 无法拉取国家/州/城市列表", level="warn")
        return

    template = dict(cfg.get("proxy_user_template") or {})
    cur_country = (template.get("country") or "Rand").strip() or "Rand"
    cur_state = (template.get("state") or "").strip()
    cur_city = (template.get("city") or "").strip()
    cur_ip_mode = template.get("ip_mode") or "Sticky"
    cur_minutes = int(template.get("sticky_minutes") or 5)

    # Q3: 国家
    try:
        countries = _lb_fetch_countries(token)
    except Exception as e:
        _console_log(f"[landbridge] 拉取国家列表失败: {e}", level="error")
        return

    print()
    new_country = _lb_pick(
        "Q3 国家",
        countries,
        lambda c: f"{c.get('zh_name', '')} ({c.get('code', '')})",
        lambda c: c.get("code", ""),
        cur_country,
    )

    # Q4: 州 (Rand 时跳过)
    new_state = ""
    new_city = ""
    if new_country and new_country != "Rand":
        states = []
        for c in countries:
            if c.get("code") == new_country:
                states = c.get("States") or []
                break
        if states:
            print()
            new_state = _lb_pick(
                "Q4 州 (回车=Random)",
                [{"state": ""}] + states,
                lambda s: s.get("state") or "Random",
                lambda s: s.get("state") or "",
                cur_state,
            )
        # Q5: 城市 (state 为空时跳过)
        if new_state:
            try:
                cities = _lb_fetch_cities(token, new_country, new_state)
            except Exception as e:
                _console_log(f"[landbridge] 拉取城市列表失败: {e}", level="warn")
                cities = []
            if cities:
                print()
                new_city = _lb_pick(
                    "Q5 城市 (回车=Random)",
                    [{"city": ""}] + cities,
                    lambda c: c.get("city") or "Random",
                    lambda c: c.get("city") or "",
                    cur_city,
                )

    # Q6: 会话类型
    print()
    new_ip_mode = _lb_pick(
        "Q6 会话类型",
        [{"v": "Sticky"}, {"v": "Rotating"}],
        lambda x: x["v"],
        lambda x: x["v"],
        cur_ip_mode,
    )

    # Q7: 仅 Sticky 问时长
    new_minutes = cur_minutes
    if new_ip_mode == "Sticky":
        raw = input(f"Q7 Sticky 时长分钟 (1-120, 回车=保留 {cur_minutes}): ").strip()
        if raw:
            try:
                v = int(raw)
                if 1 <= v <= 120:
                    new_minutes = v
                else:
                    print(f"  超出范围, 保留 {cur_minutes}")
            except ValueError:
                print(f"  无效, 保留 {cur_minutes}")

    overrides = {
        "country": new_country,
        "state": new_state,
        "city": new_city,
        "ip_mode": new_ip_mode,
        "sticky_minutes": new_minutes,
    }
    _landbridge.apply_overrides(overrides)

    # Q8: 预览 + 确认
    gw = cfg.get("gateway") or {}
    account = gw.get("account", "")
    print()
    print(f"[landbridge] 预览 username 模板:")
    parts = [account]
    if new_country: parts.append(f"-region-{new_country}")
    if new_state:   parts.append(f"-st-{new_state}")
    if new_city:    parts.append(f"-city-{new_city}")
    t = new_minutes if new_ip_mode == "Sticky" else 1
    print(f"  {''.join(parts)}-sid-<8位随机>-t-{t}")
    print(f"  gateway: {gw.get('host')}:{gw.get('port')} ({gw.get('protocol', 'http')})")
    print(f"  ip_mode: {new_ip_mode}, t={t} 分钟")

    if _prompt_confirm("Q8 确认并保存到 config.json?", default=True):
        _landbridge.persist_to_config()
        _console_log("[landbridge] 已保存到 config.json")
    else:
        _console_log("[landbridge] 未保存; 本次运行仍按以上配置生效")


def _normalize_imap_profile(raw, fallback_key="default"):
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("id") or raw.get("key") or fallback_key).strip()
    key = str(raw.get("key") or raw.get("id") or name).strip() or fallback_key
    host = str(raw.get("host") or raw.get("imap_host") or raw.get("mail_imap_host") or "").strip()
    port = int(raw.get("port") or raw.get("imap_port") or raw.get("mail_imap_port") or 993)
    user = str(raw.get("user") or raw.get("imap_user") or raw.get("mail_imap_user") or "").strip()
    password = str(
        raw.get("password")
        or raw.get("authcode")
        or raw.get("imap_password")
        or raw.get("mail_imap_password")
        or raw.get("mail_imap_authcode")
        or ""
    ).strip()
    folder = str(raw.get("folder") or raw.get("imap_folder") or raw.get("mail_imap_folder") or "INBOX").strip() or "INBOX"
    security = str(
        raw.get("security")
        or raw.get("imap_security")
        or raw.get("ssl_mode")
        or raw.get("mail_imap_security")
        or ""
    ).strip().lower()
    security = {
        "": "auto",
        "auto": "auto",
        "ssl": "ssl",
        "implicit_ssl": "ssl",
        "tls": "starttls",
        "starttls": "starttls",
        "plain": "plain",
        "none": "plain",
    }.get(security, security or "auto")
    domain = str(raw.get("domain") or raw.get("mail_domain") or "").strip().lower().lstrip("@")
    enabled = _as_bool(raw.get("enabled", True))
    return {
        "key": key,
        "name": name,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "folder": folder,
        "security": security,
        "domain": domain,
        "enabled": enabled,
    }


def _load_imap_profiles(config):
    profiles = []
    raw_profiles = config.get("imap_profiles")
    if isinstance(raw_profiles, list):
        for idx, item in enumerate(raw_profiles, start=1):
            profile = _normalize_imap_profile(item, fallback_key=f"imap{idx}")
            if profile and profile["enabled"] and profile["host"] and profile["user"] and profile["password"]:
                profiles.append(profile)

    legacy_profile = _normalize_imap_profile(
        {
            "key": config.get("mail_imap_profile_key") or "default",
            "name": config.get("mail_imap_profile_name") or "default",
            "host": config.get("mail_imap_host") or config.get("qq_imap_host", ""),
            "port": config.get("mail_imap_port") or config.get("qq_imap_port", 993),
            "user": config.get("mail_imap_user") or config.get("qq_imap_user", ""),
            "password": (
                config.get("mail_imap_password")
                or config.get("mail_imap_authcode")
                or config.get("qq_imap_authcode", "")
            ),
            "folder": config.get("mail_imap_folder") or config.get("qq_imap_folder", "INBOX"),
            "domain": config.get("mail_domain", ""),
            "enabled": True,
        }
    )
    if legacy_profile and legacy_profile["host"] and legacy_profile["user"] and legacy_profile["password"]:
        duplicate = any(
            p["key"] == legacy_profile["key"]
            or (
                p["host"] == legacy_profile["host"]
                and int(p["port"]) == int(legacy_profile["port"])
                and p["user"] == legacy_profile["user"]
                and p["folder"] == legacy_profile["folder"]
            )
            for p in profiles
        )
        if not duplicate:
            profiles.insert(0, legacy_profile)
    return profiles


def _normalize_email_source(raw, fallback_key="source"):
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or raw.get("id") or fallback_key).strip() or fallback_key
    name = str(raw.get("name") or key).strip() or key
    source_type = str(raw.get("type") or raw.get("kind") or "").strip().lower()
    aliases = {
        "domain_catchall": "forward_domain",
        "forward": "forward_domain",
        "imap": "imap_mailbox",
        "imap_profile": "imap_mailbox",
        "mailbox": "imap_mailbox",
        "addy.io": "addy",
        "anonaddy": "addy",
    }
    source_type = aliases.get(source_type, source_type)
    domain = str(raw.get("domain") or "").strip().lower().lstrip("@")
    receiver = str(
        raw.get("receiver")
        or raw.get("receiver_profile")
        or raw.get("imap_profile")
        or raw.get("profile")
        or ""
    ).strip()
    address = str(raw.get("address") or raw.get("email") or "").strip()
    address_mode = str(
        raw.get("address_mode")
        or raw.get("mailbox_mode")
        or raw.get("alias_mode")
        or ""
    ).strip().lower()
    address_mode = {
        "": "fixed",
        "fixed": "fixed",
        "single": "fixed",
        "mailbox": "fixed",
        "suffix": "suffix_alias",
        "append": "suffix_alias",
        "alias": "suffix_alias",
        "local_suffix": "suffix_alias",
        "suffix_alias": "suffix_alias",
    }.get(address_mode, address_mode or "fixed")
    enabled = _as_bool(raw.get("enabled", True))
    api_key = str(raw.get("api_key") or raw.get("token") or "").strip()
    base_url = str(raw.get("base_url") or "").strip()
    recipient_ids = raw.get("recipient_ids") or []
    if isinstance(recipient_ids, str):
        recipient_ids = [s.strip() for s in recipient_ids.split(",") if s.strip()]
    elif not isinstance(recipient_ids, list):
        recipient_ids = []
    delete_on_release = _as_bool(raw.get("delete_on_release", False))
    deactivate_on_release = _as_bool(raw.get("deactivate_on_release", False))
    description = str(raw.get("description") or "").strip()
    addy_format = str(raw.get("format") or "custom").strip().lower() or "custom"
    return {
        "key": key,
        "name": name,
        "type": source_type,
        "domain": domain,
        "receiver": receiver,
        "address": address,
        "address_mode": address_mode,
        "enabled": enabled,
        "api_key": api_key,
        "base_url": base_url,
        "recipient_ids": list(recipient_ids),
        "delete_on_release": delete_on_release,
        "deactivate_on_release": deactivate_on_release,
        "description": description,
        "format": addy_format,
    }


def _mailbox_signature(profile):
    if not profile:
        return None
    return (
        profile.get("host", ""),
        int(profile.get("port") or 993),
        profile.get("user", ""),
        profile.get("folder", "INBOX"),
    )


def _build_forward_domain_source(profile, key_hint="default"):
    domain = str(profile.get("domain") or "").strip().lower().lstrip("@")
    if not domain:
        return None
    return {
        "key": str(key_hint or profile["key"]).strip() or profile["key"],
        "name": f"{domain} -> {profile['name']}",
        "type": "forward_domain",
        "domain": domain,
        "receiver": profile["key"],
        "address": "",
        "enabled": True,
    }


def _build_mailbox_source(profile):
    if not profile or not profile.get("user"):
        return None
    return {
        "key": profile["key"],
        "name": profile["name"],
        "type": "imap_mailbox",
        "domain": "",
        "receiver": profile["key"],
        "address": profile["user"],
        "address_mode": "fixed",
        "enabled": True,
    }


def _validate_email_source(source):
    if not source or not source["enabled"]:
        return False
    if source["type"] in {"duckmail", "custom"}:
        return True
    profile = _get_imap_profile(source["receiver"])
    if not profile:
        return False
    if source["type"] == "forward_domain":
        return bool(source["domain"])
    if source["type"] == "imap_mailbox":
        source["address"] = source["address"] or profile["user"]
        return bool(source["address"])
    if source["type"] == "addy":
        return bool(source["domain"] and source.get("api_key"))
    return False


def _load_email_sources(config):
    sources = []
    seen = set()
    raw_sources = config.get("email_sources")
    if isinstance(raw_sources, list):
        for idx, item in enumerate(raw_sources, start=1):
            source = _normalize_email_source(item, fallback_key=f"source{idx}")
            if not source or source["key"] in seen:
                continue
            if _validate_email_source(source):
                sources.append(source)
                seen.add(source["key"])

    if sources:
        return sources

    synth_seen = set()
    for profile in IMAP_PROFILES:
        signature = _mailbox_signature(profile)
        if profile.get("domain"):
            source = _build_forward_domain_source(profile, key_hint=profile["key"])
            dedupe_key = ("forward_domain", profile.get("domain"), signature)
        else:
            source = _build_mailbox_source(profile)
            dedupe_key = ("imap_mailbox", profile.get("user"), signature)
        if not source or dedupe_key in synth_seen or source["key"] in seen:
            continue
        if _validate_email_source(source):
            sources.append(source)
            seen.add(source["key"])
            synth_seen.add(dedupe_key)
    return sources


def _find_email_source_by_receiver(profile_key, *, preferred_type=""):
    candidates = [source for source in EMAIL_SOURCES if source["receiver"] == profile_key]
    if preferred_type:
        for source in candidates:
            if source["type"] == preferred_type:
                return source
    return candidates[0] if candidates else None


def _source_provider_key(source_key):
    return f"source:{source_key}"


def _normalize_mail_provider(mail_provider):
    value = (mail_provider or "").strip()
    if not value:
        return "duckmail"
    if value in {"duckmail", "custom"}:
        return value
    if value.startswith("source:"):
        key = value.split(":", 1)[1].strip()
        return value if key in EMAIL_SOURCES_BY_KEY else value
    if value in EMAIL_SOURCES_BY_KEY:
        return _source_provider_key(value)
    if value == "domain_catchall":
        return _source_provider_key(DEFAULT_EMAIL_SOURCE_KEY) if DEFAULT_EMAIL_SOURCE_KEY else value
    if value.startswith("imap:"):
        profile_key = value.split(":", 1)[1].strip()
        source = _find_email_source_by_receiver(profile_key, preferred_type="forward_domain")
        if not source:
            source = _find_email_source_by_receiver(profile_key, preferred_type="imap_mailbox")
        if source:
            return _source_provider_key(source["key"])
    return value


def _get_email_source(source_key=""):
    key = (source_key or DEFAULT_EMAIL_SOURCE_KEY or "").strip()
    if not key:
        return None
    return EMAIL_SOURCES_BY_KEY.get(key)


def _get_email_source_for_provider(mail_provider):
    normalized = _normalize_mail_provider(mail_provider)
    if normalized.startswith("source:"):
        return _get_email_source(normalized.split(":", 1)[1].strip())
    return None


def _mail_provider_is_imap(mail_provider):
    source = _get_email_source_for_provider(mail_provider)
    return bool(source and source["type"] in {"forward_domain", "imap_mailbox", "addy"})


def _mail_provider_profile_key(mail_provider):
    source = _get_email_source_for_provider(mail_provider)
    if source:
        return source["receiver"]
    return ""


def _get_imap_profile(profile_key=""):
    key = (profile_key or DEFAULT_IMAP_PROFILE_KEY or "").strip()
    if not key:
        return None
    return IMAP_PROFILES_BY_KEY.get(key)


def _get_receiver_profile_for_source(source):
    if not source:
        return None
    return _get_imap_profile(source.get("receiver", ""))


def _get_source_mail_domain(source):
    if not source:
        return ""
    if source["type"] in ("forward_domain", "addy"):
        return source["domain"]
    if source["type"] == "imap_mailbox":
        address = source["address"] or ""
        if "@" in address:
            return address.split("@", 1)[1].strip().lower()
    return ""


def _email_source_uses_suffix_alias(source):
    return bool(
        source
        and source.get("type") == "imap_mailbox"
        and source.get("address_mode", "fixed") == "suffix_alias"
    )


def _email_source_requires_single_address(source):
    return bool(source and source.get("type") == "imap_mailbox" and not _email_source_uses_suffix_alias(source))


def _describe_email_source(source):
    if not source:
        return "未配置"
    profile = _get_receiver_profile_for_source(source)
    if source["type"] == "forward_domain":
        target = profile["user"] if profile else "?"
        return f"域名邮箱: *@{source['domain']} -> {target}"
    if source["type"] == "addy":
        target = profile["user"] if profile else "?"
        return f"addy.io 别名: *@{source['domain']} -> {target}"
    if source["type"] == "imap_mailbox":
        address = source["address"] or (profile["user"] if profile else "?")
        if _email_source_uses_suffix_alias(source) and "@" in address:
            local, domain = address.split("@", 1)
            return f"邮箱别名: {local}<suffix>@{domain}"
        return f"邮箱收件箱: {address}"
    if source["type"] == "custom":
        return "指定邮箱"
    return "DuckMail 临时邮箱"


def _build_imap_pool_config(profile, domain):
    if not profile or not domain:
        return None
    log_level = "debug" if _as_bool(_CONFIG.get("mail_debug", False)) else _CURRENT_LOG_LEVEL
    return {
        "mail_imap_host": profile["host"],
        "mail_imap_port": profile["port"],
        "mail_imap_user": profile["user"],
        "mail_imap_password": profile["password"],
        "mail_imap_folder": profile["folder"],
        "mail_imap_security": profile.get("security", "auto"),
        "mail_domain": domain,
        "mail_poll_interval": _CONFIG.get("mail_poll_interval", 4),
        "mail_debug": bool(_CONFIG.get("mail_debug", False)),
        "log_level": log_level,
    }


def _get_mail_pool_for_provider(mail_provider):
    if not _mail_provider_is_imap(mail_provider) or not _get_qq_mail_pool:
        return None
    source = _get_email_source_for_provider(mail_provider)
    profile = _get_imap_profile(_mail_provider_profile_key(mail_provider))
    domain = _get_source_mail_domain(source)
    if not source or not profile or not domain:
        return None
    log = _make_channel_logger("email")
    imap_cfg = _build_imap_pool_config(profile, domain)
    if source["type"] == "addy":
        if not _get_addy_pool:
            return None
        addy_cfg = {
            "api_key": source.get("api_key", ""),
            "domain": domain,
            "base_url": source.get("base_url") or "",
            "recipient_ids": source.get("recipient_ids") or [],
            "description": source.get("description") or "",
            "delete_on_release": source.get("delete_on_release", False),
            "deactivate_on_release": source.get("deactivate_on_release", False),
            "format": source.get("format") or "custom",
        }
        return _get_addy_pool(addy_cfg, imap_cfg, log=log)
    return _get_qq_mail_pool(imap_cfg, log=log)


_CONFIG = _load_config()
_CURRENT_LOG_LEVEL = normalize_log_level(_CONFIG.get("log_level", DEFAULT_LOG_LEVEL))
DUCKMAIL_API_BASE = _CONFIG["duckmail_api_base"]
DUCKMAIL_BEARER = _CONFIG["duckmail_bearer"]
DEFAULT_TOTAL_ACCOUNTS = _CONFIG["total_accounts"]
DEFAULT_PROXY = _CONFIG["proxy"]
LANDBRIDGE_CFG = _CONFIG.get("landbridge") or {}
LANDBRIDGE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
import landbridge_runtime as _landbridge
_landbridge.configure(LANDBRIDGE_CFG, LANDBRIDGE_CONFIG_PATH)
DEFAULT_OUTPUT_FILE = _CONFIG["output_file"]
ENABLE_OAUTH = _as_bool(_CONFIG.get("enable_oauth", True))
OAUTH_REQUIRED = _as_bool(_CONFIG.get("oauth_required", True))
OAUTH_ADD_PHONE_SMS = _as_bool(_CONFIG.get("oauth_add_phone_sms", False))
OAUTH_ISSUER = _CONFIG["oauth_issuer"].rstrip("/")
OAUTH_CLIENT_ID = _CONFIG["oauth_client_id"]
OAUTH_REDIRECT_URI = _CONFIG["oauth_redirect_uri"]
AK_FILE = _CONFIG["ak_file"]
RK_FILE = _CONFIG["rk_file"]
DEFAULT_MAX_WORKERS = _CONFIG["max_workers"]
TOKEN_JSON_DIR = _CONFIG["token_json_dir"]
TUI_ENABLED = _as_bool(_CONFIG.get("tui_enabled", False))
LOG_LEVEL = _CURRENT_LOG_LEVEL
UPLOAD_API_URL = _CONFIG["upload_api_url"]
UPLOAD_API_TOKEN = _CONFIG["upload_api_token"]
SENTINEL_SOLVER_URL = _CONFIG["sentinel_solver_url"]
SENTINEL_INPROCESS = _as_bool(_CONFIG.get("sentinel_inprocess", True))
SENTINEL_SOLVER_THREAD = int(_CONFIG.get("sentinel_solver_thread", 0) or 0)
SENTINEL_SOLVER_HEADLESS = _as_bool(_CONFIG.get("sentinel_solver_headless", True))
SENTINEL_SOLVER_CHANNEL = _CONFIG.get("sentinel_solver_channel", "chromium")
SENTINEL_SOLVER_DEBUG = _as_bool(_CONFIG.get("sentinel_solver_debug", False))
SMS_PROVIDER_NAME = (_CONFIG.get("sms_provider") or "").strip()
SMS_MAX_RETRIES = int(_CONFIG.get("sms_max_retries", 3))
SMS_WAIT_OTP_TIMEOUT = int(_CONFIG.get("sms_wait_otp_timeout", 120))
SMS_POLL_INTERVAL = int(_CONFIG.get("sms_poll_interval", 5))
PHONE_MAX_REUSE = int(_CONFIG.get("phone_max_reuse", 3))
PHONE_MAX_ACTIVE = int(_CONFIG.get("phone_max_active") or 0)
PHONE_ACQUIRE_TIMEOUT = float(_CONFIG.get("phone_acquire_timeout", 60.0))
PHONE_POOL_LEASE_SECONDS = int(_CONFIG.get("phone_pool_lease_seconds", 60))
PHONE_POOL_HEARTBEAT_SECONDS = int(_CONFIG.get("phone_pool_heartbeat_seconds", 30))
RETRY_OAUTH_DEFAULT_WORKERS = 1
PHONE_POOL_ENABLED = bool(_CONFIG.get("phone_pool_enabled", True))
IMAP_PROFILES = _load_imap_profiles(_CONFIG)
IMAP_PROFILES_BY_KEY = {profile["key"]: profile for profile in IMAP_PROFILES}
DEFAULT_IMAP_PROFILE_KEY = IMAP_PROFILES[0]["key"] if IMAP_PROFILES else ""
EMAIL_SOURCES = _load_email_sources(_CONFIG)
EMAIL_SOURCES_BY_KEY = {source["key"]: source for source in EMAIL_SOURCES}
DEFAULT_EMAIL_SOURCE_KEY = (
    str(_CONFIG.get("default_email_source") or "").strip()
    if str(_CONFIG.get("default_email_source") or "").strip() in EMAIL_SOURCES_BY_KEY
    else (EMAIL_SOURCES[0]["key"] if EMAIL_SOURCES else "")
)

# 全局共享的号池 (跨注册线程共享 lease 状态; 进程内单例)
_phone_pool_singleton = None
_phone_pool_lock = threading.Lock()
_run_metrics_lock = threading.Lock()
_run_metrics = {
    "done": 0,
    "success": 0,
    "fail": 0,
    "warn": 0,
    "cap_skipped": 0,
    "spent": 0.0,
}
_intake_paused_event = threading.Event()
_shutdown_event = threading.Event()
_force_no_tui = False
_force_tui = False
_inflight_workers_lock = threading.Lock()
_inflight_workers: set[str] = set()


def _monitor_emit(channel_name, message, *, level="info", worker_id=None, **fields):
    if not should_log(level, _CURRENT_LOG_LEVEL):
        return
    if monitor is not None:
        monitor.emit(channel_name, message, level=level, worker_id=worker_id, **fields)
        return
    _console_log(f"[{channel_name}] {message}", level=level)


def _make_channel_logger(channel_name, *, default_level="info", **bound_fields):
    def _log(message, *, level=default_level):
        _monitor_emit(channel_name, message, level=level, **bound_fields)

    return _log


def _set_log_level(level):
    global _CURRENT_LOG_LEVEL, LOG_LEVEL
    _CURRENT_LOG_LEVEL = normalize_log_level(level, default=_CURRENT_LOG_LEVEL)
    LOG_LEVEL = _CURRENT_LOG_LEVEL


def _set_oauth_add_phone_sms_enabled(enabled):
    global OAUTH_ADD_PHONE_SMS
    if enabled is None:
        return
    OAUTH_ADD_PHONE_SMS = bool(enabled)
    _CONFIG["oauth_add_phone_sms"] = OAUTH_ADD_PHONE_SMS


def _worker_log(message, *, level="info", **fields):
    _monitor_emit("worker", message, level=level, **fields)


def _system_log(message, *, level="info", **fields):
    _monitor_emit("system", message, level=level, **fields)


def _summary_snapshot():
    with _run_metrics_lock:
        return dict(_run_metrics)


def _reset_run_metrics():
    with _run_metrics_lock:
        for key in ("done", "success", "fail", "warn", "cap_skipped"):
            _run_metrics[key] = 0
        _run_metrics["spent"] = 0.0
    with _inflight_workers_lock:
        _inflight_workers.clear()


def _bump_metric(key, delta=1):
    with _run_metrics_lock:
        _run_metrics[key] = _run_metrics.get(key, 0) + delta


def _mark_worker_active(worker_id: str) -> None:
    if not worker_id:
        return
    with _inflight_workers_lock:
        _inflight_workers.add(worker_id)


def _mark_worker_idle(worker_id: str) -> None:
    if not worker_id:
        return
    with _inflight_workers_lock:
        _inflight_workers.discard(worker_id)


def _inflight_workers_snapshot():
    with _inflight_workers_lock:
        return sorted(_inflight_workers)


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or "-"
    local, domain = email.split("@", 1)
    if len(local) <= 3:
        local_masked = local[0] + "***"
    else:
        local_masked = local[:3] + "***"
    return f"{local_masked}@{domain}"


def _is_tui_enabled() -> bool:
    if _force_tui:
        return True
    if _force_no_tui:
        return False
    if _as_bool(os.environ.get("CHATGPT_REGISTER_NO_TUI")):
        return False
    return TUI_ENABLED


def _get_phone_pool():
    """lazy 构造 PhonePool 单例. 仅 herosms 支持复用."""
    global _phone_pool_singleton
    if not PHONE_POOL_ENABLED or PhonePool is None or get_provider is None:
        return None
    if SMS_PROVIDER_NAME.lower() not in ("herosms", "hero-sms", "hero_sms"):
        return None
    with _phone_pool_lock:
        if _phone_pool_singleton is not None:
            return _phone_pool_singleton
        try:
            provider = get_provider(SMS_PROVIDER_NAME, _CONFIG)
            pool = PhonePool(
                provider,
                max_reuse=PHONE_MAX_REUSE,
                max_active=PHONE_MAX_ACTIVE,
                acquire_timeout=PHONE_ACQUIRE_TIMEOUT,
                lease_seconds=PHONE_POOL_LEASE_SECONDS,
                heartbeat_seconds=PHONE_POOL_HEARTBEAT_SECONDS,
                log=monitor.channel("phone_pool") if monitor is not None else print,
            )
            try:
                pool.reconcile()
            except Exception as e:
                _system_log(f"reconcile 失败 (忽略, 继续): {e}", level="warn")
            _phone_pool_singleton = pool
            return pool
        except Exception as e:
            _system_log(f"phone_pool 初始化失败, 走单次 acquire 模式: {e}", level="warn")
            return None

# 自有域名/IMAP 邮箱配置
DEFAULT_EMAIL_SOURCE = _get_email_source()
DEFAULT_IMAP_PROFILE = _get_imap_profile(_mail_provider_profile_key(_source_provider_key(DEFAULT_EMAIL_SOURCE_KEY)))
QQ_IMAP_USER = DEFAULT_IMAP_PROFILE["user"] if DEFAULT_IMAP_PROFILE else ""
QQ_IMAP_AUTHCODE = DEFAULT_IMAP_PROFILE["password"] if DEFAULT_IMAP_PROFILE else ""
MAIL_DOMAIN = DEFAULT_EMAIL_SOURCE["domain"] if DEFAULT_EMAIL_SOURCE and DEFAULT_EMAIL_SOURCE["type"] in ("forward_domain", "addy") else ""
HAS_DOMAIN_CATCHALL = bool(DEFAULT_EMAIL_SOURCE and DEFAULT_EMAIL_SOURCE["type"] in ("forward_domain", "addy"))

if not DUCKMAIL_BEARER:
    _console_block([
        "⚠️ 警告: 未设置 DUCKMAIL_BEARER，请在 config.json 中设置或设置环境变量",
        "   文件: config.json -> duckmail_bearer",
        "   环境变量: export DUCKMAIL_BEARER='your_api_key_here'",
    ], level="warn")


# Chrome 指纹配置: impersonate 与 sec-ch-ua 尽量匹配真实浏览器
_CHROME_PROFILES = [
    {
        "major": 120, "impersonate": "chrome120",
        "build": 6099, "patch_range": (110, 225),
        "sec_ch_ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    },
    {
        "major": 123, "impersonate": "chrome123",
        "build": 6312, "patch_range": (80, 140),
        "sec_ch_ua": '"Google Chrome";v="123", "Chromium";v="123", "Not:A-Brand";v="8"',
    },
    {
        "major": 124, "impersonate": "chrome124",
        "build": 6367, "patch_range": (60, 210),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    },
    {
        "major": 131, "impersonate": "chrome131",
        "build": 6778, "patch_range": (69, 205),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    },
    {
        "major": 133, "impersonate": "chrome133a",
        "build": 6943, "patch_range": (33, 153),
        "sec_ch_ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    },
    {
        "major": 136, "impersonate": "chrome136",
        "build": 7103, "patch_range": (48, 175),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    },
    {
        "major": 142, "impersonate": "chrome142",
        "build": 7540, "patch_range": (30, 150),
        "sec_ch_ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    },
]

if BrowserType is not None:
    _supported_impersonates = {browser.value for browser in BrowserType}
    _CHROME_PROFILES = [
        profile for profile in _CHROME_PROFILES
        if profile["impersonate"] in _supported_impersonates
    ]

if not _CHROME_PROFILES:
    raise RuntimeError("当前 curl_cffi 版本不支持任何已配置的 Chrome impersonate 指纹")


def _random_chrome_version():
    profile = random.choice(_CHROME_PROFILES)
    major = profile["major"]
    build = profile["build"]
    patch = random.randint(*profile["patch_range"])
    full_ver = f"{major}.0.{build}.{patch}"
    ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full_ver} Safari/537.36"
    return profile["impersonate"], major, full_ver, ua, profile["sec_ch_ua"]


def _random_delay(low=0.3, high=1.0):
    time.sleep(random.uniform(low, high))


def _make_trace_headers():
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    tp = f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01"
    return {
        "traceparent": tp, "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum", "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id), "x-datadog-parent-id": str(parent_id),
    }


def _generate_pkce():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


_sentinel_runtime_singleton = None
_sentinel_runtime_lock = threading.Lock()
_sentinel_runtime_config = {
    "thread": None,
    "default_proxy": None,
}


class _InProcessSentinelRuntime:
    def __init__(self, headless, thread, debug, channel, default_proxy):
        self.headless = headless
        self.thread = thread
        self.debug = debug
        self.channel = channel
        self.default_proxy = default_proxy
        self._thread = None
        self._loop = None
        self._solver = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._start_error = None

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._solver = EmbeddedSentinelSolver(
                headless=self.headless,
                thread=self.thread,
                debug=self.debug,
                channel=self.channel,
                default_proxy=self.default_proxy,
                enable_http=False,
                log=_make_channel_logger("sentinel"),
                log_level="debug" if self.debug else _CURRENT_LOG_LEVEL,
            )
            loop.run_until_complete(self._solver.ensure_started())
        except Exception as e:
            self._start_error = e
            self._ready.set()
            try:
                if self._solver is not None:
                    loop.run_until_complete(self._solver._shutdown())
            except Exception:
                pass
            loop.close()
            self._loop = None
            return

        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                if self._solver is not None:
                    loop.run_until_complete(self._solver._shutdown())
            except Exception:
                pass
            loop.close()
            self._loop = None

    def start(self, timeout=120):
        with self._start_lock:
            if not (self._thread and self._thread.is_alive()):
                self._ready.clear()
                self._start_error = None
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="sentinel-solver",
                    daemon=True,
                )
                self._thread.start()

        if not self._ready.wait(timeout):
            raise TimeoutError("in-process sentinel solver 启动超时")
        if self._start_error is not None:
            raise RuntimeError(f"in-process sentinel solver 启动失败: {self._start_error}")

    def _submit(self, coro_factory, timeout):
        self.start(timeout=max(timeout, 30))
        if self._loop is None or self._solver is None:
            raise RuntimeError("in-process sentinel solver event loop 不可用")
        coro = coro_factory(self._solver)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def health(self, timeout=30):
        return self._submit(lambda solver: solver.health_data(), timeout)

    def solve_token(self, flow, oai_did, user_agent, proxy=None, timeout=60):
        return self._submit(
            lambda solver: solver.solve_token(
                flow=flow,
                oai_did=oai_did,
                ua=user_agent,
                proxy=proxy,
            ),
            timeout,
        )

    def close(self, timeout=30):
        with self._start_lock:
            loop = self._loop
            worker = self._thread
            solver = self._solver
            if loop is None or worker is None:
                return
            self._thread = None
            self._solver = None
            self._ready.clear()

        try:
            if solver is not None:
                future = asyncio.run_coroutine_threadsafe(solver._shutdown(), loop)
                future.result(timeout=timeout)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        try:
            worker.join(timeout)
        except Exception:
            pass


def _get_inprocess_sentinel_runtime():
    global _sentinel_runtime_singleton
    if not SENTINEL_INPROCESS:
        return None
    if EmbeddedSentinelSolver is None:
        raise RuntimeError(f"sentinel_solver 模块导入失败: {_EMBEDDED_SENTINEL_IMPORT_ERROR}")
    with _sentinel_runtime_lock:
        if _sentinel_runtime_singleton is None:
            default_proxy = _sentinel_runtime_config["default_proxy"]
            if default_proxy is None:
                default_proxy = DEFAULT_PROXY or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                                or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
            thread = _sentinel_runtime_config["thread"]
            if thread is None:
                thread = SENTINEL_SOLVER_THREAD if SENTINEL_SOLVER_THREAD > 0 else (DEFAULT_MAX_WORKERS + 1)
            _sentinel_runtime_singleton = _InProcessSentinelRuntime(
                headless=SENTINEL_SOLVER_HEADLESS,
                thread=thread,
                debug=SENTINEL_SOLVER_DEBUG,
                channel=SENTINEL_SOLVER_CHANNEL,
                default_proxy=default_proxy,
            )
    return _sentinel_runtime_singleton


def _configure_inprocess_sentinel_runtime(*, thread=None, default_proxy=None):
    global _sentinel_runtime_singleton
    with _sentinel_runtime_lock:
        should_reset = _sentinel_runtime_singleton is not None
        if thread is not None:
            _sentinel_runtime_config["thread"] = int(thread)
        if default_proxy is not None:
            _sentinel_runtime_config["default_proxy"] = default_proxy
        if should_reset:
            try:
                _sentinel_runtime_singleton.close()
            except Exception:
                pass
            _sentinel_runtime_singleton = None


def _shutdown_inprocess_sentinel_runtime():
    runtime = _sentinel_runtime_singleton
    if runtime is None:
        return
    try:
        runtime.close()
    except Exception:
        pass


atexit.register(_shutdown_inprocess_sentinel_runtime)


# sentinel token 改由 sentinel_solver.py 服务在真浏览器里生成；
# 旧的 SentinelTokenGenerator/fetch_sentinel_challenge/build_sentinel_token 已废弃，
# 因为 OpenAI 把 PoW config schema 升到 25 字段并加了 Turnstile VM，本地无法复现。

def _request_sentinel_token(flow, device_id, user_agent, proxy=None, timeout=60):
    """优先走进程内 solver，回退到本地 HTTP solver 服务。"""
    if SENTINEL_INPROCESS:
        try:
            runtime = _get_inprocess_sentinel_runtime()
            token, err = runtime.solve_token(
                flow=flow,
                oai_did=device_id,
                user_agent=user_agent,
                proxy=proxy,
                timeout=timeout,
            )
            if token:
                return token
            _console_log(f"[Sentinel] in-process solver 失败: {err}", level="error")
            return None
        except Exception as e:
            _console_log(f"[Sentinel] in-process solver 调用失败: {e}", level="error")
            return None

    if not SENTINEL_SOLVER_URL:
        return None
    url = SENTINEL_SOLVER_URL.rstrip("/") + "/sentinel/token"
    body = {
        "flow": flow,
        "oai_did": device_id,
        "user_agent": user_agent,
    }
    if proxy:
        body["proxy"] = proxy
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return data.get("token")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        _console_log(f"[Sentinel] solver HTTP {e.code}: {err_body[:200]}", level="error")
    except Exception as e:
        _console_log(f"[Sentinel] solver 调用失败: {e}", level="error")
    return None


def _check_sentinel_solver_health(timeout=5):
    if SENTINEL_INPROCESS:
        try:
            runtime = _get_inprocess_sentinel_runtime()
            data = runtime.health(timeout=max(timeout, 30))
            return True, data
        except Exception as e:
            return False, str(e)

    if not SENTINEL_SOLVER_URL:
        return False, "SENTINEL_SOLVER_URL 未配置"
    url = SENTINEL_SOLVER_URL.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            if data.get("ok"):
                return True, data
            return False, data
    except Exception as e:
        return False, str(e)


def _extract_code_from_url(url: str):
    if not url or "code=" not in url:
        return None
    try:
        return parse_qs(urlparse(url).query).get("code", [None])[0]
    except Exception:
        return None


def _decode_jwt_payload(token: str):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _extract_code_from_input(raw: str):
    value = (raw or "").strip()
    if not value:
        return None
    return _extract_code_from_url(value) or value


def _infer_email_from_tokens(tokens: dict):
    access_payload = _decode_jwt_payload(tokens.get("access_token", ""))
    profile = access_payload.get("https://api.openai.com/profile", {})
    email = str(profile.get("email") or access_payload.get("email") or "").strip()
    if email:
        return email
    id_payload = _decode_jwt_payload(tokens.get("id_token", ""))
    return str(id_payload.get("email") or "").strip()


def _build_codex_token_data(email: str, tokens: dict):
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    id_token = tokens.get("id_token", "")
    if not access_token:
        return None

    payload = _decode_jwt_payload(access_token)
    auth_info = payload.get("https://api.openai.com/auth", {})
    account_id = auth_info.get("chatgpt_account_id", "")

    exp_timestamp = payload.get("exp")
    expired_str = ""
    if isinstance(exp_timestamp, int) and exp_timestamp > 0:
        from datetime import datetime, timezone, timedelta

        exp_dt = datetime.fromtimestamp(exp_timestamp, tz=timezone(timedelta(hours=8)))
        expired_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    from datetime import datetime, timezone, timedelta

    now = datetime.now(tz=timezone(timedelta(hours=8)))
    return {
        "type": "codex",
        "email": email,
        "expired": expired_str,
        "id_token": id_token,
        "account_id": account_id,
        "access_token": access_token,
        "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "refresh_token": refresh_token,
    }


def _make_auth_filename(filename_hint: str, fallback_stem: str = "auth"):
    raw = (filename_hint or "").strip()
    if not raw:
        raw = fallback_stem
    raw = os.path.basename(raw)
    if raw.lower().endswith(".json"):
        raw = raw[:-5]
    raw = re.sub(r'[\\/:*?"<>|]+', "_", raw).strip().strip(".")
    raw = raw or fallback_stem or "auth"
    return f"{raw}.json"


def _persist_codex_token_data(token_data: dict, filename_hint: str = "",
                              write_token_lines: bool = True, upload_to_cpa=None):
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if write_token_lines:
        if access_token:
            with _file_lock:
                with open(AK_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{access_token}\n")

        if refresh_token:
            with _file_lock:
                with open(RK_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{refresh_token}\n")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    token_dir = TOKEN_JSON_DIR if os.path.isabs(TOKEN_JSON_DIR) else os.path.join(base_dir, TOKEN_JSON_DIR)
    os.makedirs(token_dir, exist_ok=True)

    fallback_stem = token_data.get("email") or "auth"
    filename = _make_auth_filename(filename_hint, fallback_stem=fallback_stem)
    token_path = os.path.join(token_dir, filename)
    with _file_lock:
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False)

    if upload_to_cpa is None:
        upload_to_cpa = bool(UPLOAD_API_URL)
    if upload_to_cpa:
        _upload_token_json(token_path)
    return token_path


def _exchange_codex_auth_code(code: str, code_verifier: str, proxy: str = None):
    reg = ChatGPTRegister(proxy=proxy, tag="auth_from_code")
    reg._print("[AuthFile] 开始用 authorization code 换 token")
    token_resp = reg.session.post(
        f"{OAUTH_ISSUER}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": reg.ua},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        },
        timeout=60,
        impersonate=reg.impersonate,
    )
    reg._print(f"[AuthFile] /oauth/token -> {token_resp.status_code}")

    if token_resp.status_code != 200:
        reg._print(f"[AuthFile] token 交换失败: {token_resp.status_code} {token_resp.text[:200]}")
        return None

    try:
        data = token_resp.json()
    except Exception:
        reg._print("[AuthFile] token 响应解析失败")
        return None

    if not data.get("access_token"):
        reg._print("[AuthFile] token 响应缺少 access_token")
        return None

    reg._print("[AuthFile] Codex Token 获取成功")
    return data


PENDING_OAUTH_FILE = "pending_oauth.txt"


def _append_pending_oauth(email: str, password: str, email_pwd: str, mail_provider: str = ""):
    """注册成功但 OAuth 失败的账号写入 pending_oauth.txt, 供后续 --retry-oauth 使用。
    格式: email----password----email_pwd----mail_provider
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        path = PENDING_OAUTH_FILE if os.path.isabs(PENDING_OAUTH_FILE) else os.path.join(base_dir, PENDING_OAUTH_FILE)
        with _file_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{email}----{password}----{email_pwd}----{mail_provider or ''}\n")
    except Exception as e:
        _console_log(f"[Warn] 写 pending_oauth.txt 失败: {e}", level="warn")


def _finalize_pending_oauth_failure(
    reg,
    email: str,
    password: str,
    email_pwd: str,
    mail_provider: str,
    message: str,
    *,
    cause: Exception | None = None,
):
    """主注册已成功但 OAuth 未完成时，写入 pending 并按配置决定是否报失败。"""
    _append_pending_oauth(email, password, email_pwd, mail_provider)
    if OAUTH_REQUIRED:
        full_message = f"{message}（oauth_required=true，已记入 pending_oauth.txt）"
        if cause is not None:
            raise Exception(full_message) from cause
        raise Exception(full_message)
    if reg is not None:
        reg._print(f"[OAuth] {message}（按配置继续，已记入 pending_oauth.txt）")


def _save_codex_tokens(email: str, tokens: dict):
    token_data = _build_codex_token_data(email, tokens)
    if not token_data:
        return
    _persist_codex_token_data(token_data, filename_hint=email, write_token_lines=True, upload_to_cpa=None)


def _upload_token_json(filepath):
    """上传 Token JSON 文件到 CPA 管理平台"""
    mp = None
    try:
        from curl_cffi import CurlMime

        filename = os.path.basename(filepath)
        mp = CurlMime()
        mp.addpart(
            name="file",
            content_type="application/json",
            filename=filename,
            local_path=filepath,
        )

        session = curl_requests.Session()
        if DEFAULT_PROXY:
            session.proxies = {"http": DEFAULT_PROXY, "https": DEFAULT_PROXY}

        resp = session.post(
            UPLOAD_API_URL,
            multipart=mp,
            headers={"Authorization": f"Bearer {UPLOAD_API_TOKEN}"},
            verify=False,
            timeout=30,
        )

        if resp.status_code == 200:
            _console_log("  [CPA] Token JSON 已上传到 CPA 管理平台", level="success")
        else:
            _console_log(f"  [CPA] 上传失败: {resp.status_code} - {resp.text[:200]}", level="error")
    except Exception as e:
        _console_log(f"  [CPA] 上传异常: {e}", level="error")
    finally:
        if mp:
            mp.close()


def _generate_password(length=14):
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%&*"
    pwd = [random.choice(lower), random.choice(upper),
           random.choice(digits), random.choice(special)]
    all_chars = lower + upper + digits + special
    pwd += [random.choice(all_chars) for _ in range(length - 4)]
    random.shuffle(pwd)
    return "".join(pwd)


_EXIT_IP_PROBE_CACHE: dict = {}
_EXIT_IP_PROBE_LOCK = threading.Lock()


def _probe_exit_ip(proxy, timeout: float = 5.0) -> str:
    """通过 proxy 请求 ipify, 返回出口 IP。失败返回 'ERR(<类型>)'。"""
    try:
        request = urllib.request.Request(
            "https://api.ipify.org?format=json",
            headers={"User-Agent": "landbridge-probe/1.0"},
        )
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return str(data.get("ip") or "?")
    except Exception as e:
        return f"ERR({type(e).__name__})"


def _landbridge_exit_ip_banner(api_proxy, browser_proxy, *, tag: str = "") -> None:
    """启用 landbridge 时, 一次性自检并打印 banner。同一对 (api,browser) 仅探测一次。"""
    if not api_proxy or api_proxy == browser_proxy:
        return
    key = (api_proxy, browser_proxy)
    with _EXIT_IP_PROBE_LOCK:
        cached = _EXIT_IP_PROBE_CACHE.get(key)
        if cached is None:
            api_ip = _probe_exit_ip(api_proxy)
            br_ip = _probe_exit_ip(browser_proxy) if browser_proxy else "(none)"
            if api_ip.startswith("ERR"):
                verdict = "FAIL"
            elif api_ip == br_ip:
                verdict = "BYPASS (api==browser, 链未生效?)"
            else:
                verdict = "OK (api 走 landbridge)"
            cached = f"[landbridge] exit_ip api={api_ip}  browser={br_ip}  {verdict}"
            _EXIT_IP_PROBE_CACHE[key] = cached
    prefix = f"[{tag}] " if tag else ""
    _console_log(f"{prefix}{cached}")


def _duckmail_request(method, url, *, headers=None, payload=None, timeout=15, proxy=None):
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    if payload is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    try:
        with opener.open(request, timeout=timeout) as response:
            raw_text = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw_text) if raw_text else None
            return response.status, parsed, raw_text
    except urllib.error.HTTPError as e:
        raw_text = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw_text) if raw_text else None
        except Exception:
            parsed = None
        return e.code, parsed, raw_text


# ================= DuckMail 邮箱函数 =================


def create_temp_email():
    """创建 DuckMail 临时邮箱，返回 (email, password, mail_token)"""
    if not DUCKMAIL_BEARER:
        raise Exception("DUCKMAIL_BEARER 未设置，无法创建临时邮箱")

    # 生成随机邮箱前缀 8-13 位
    chars = string.ascii_lowercase + string.digits
    length = random.randint(8, 13)
    email_local = "".join(random.choice(chars) for _ in range(length))
    email = f"{email_local}@duckmail.sbs"
    password = _generate_password()

    api_base = DUCKMAIL_API_BASE.rstrip("/")
    headers = {"Authorization": f"Bearer {DUCKMAIL_BEARER}"}
    try:
        # 1. 创建账号
        payload = {"address": email, "password": password}
        status_code, _, raw_text = _duckmail_request(
            "POST",
            f"{api_base}/accounts",
            headers=headers,
            payload=payload,
            timeout=15,
        )
        if status_code not in [200, 201]:
            raise Exception(f"创建邮箱失败: {status_code} - {raw_text[:200]}")

        # 2. 获取 Token（用于读取邮件）
        time.sleep(0.5)
        token_payload = {"address": email, "password": password}
        token_status, token_data, _ = _duckmail_request(
            "POST",
            f"{api_base}/token",
            payload=token_payload,
            timeout=15,
        )

        if token_status == 200 and token_data:
            mail_token = token_data.get("token")
            if mail_token:
                return email, password, mail_token

        raise Exception(f"获取邮件 Token 失败: {token_status}")

    except Exception as e:
        raise Exception(f"DuckMail 创建邮箱失败: {e}")


def _fetch_emails_duckmail(mail_token: str):
    """从 DuckMail 获取邮件列表"""
    try:
        api_base = DUCKMAIL_API_BASE.rstrip("/")
        status_code, data, _ = _duckmail_request(
            "GET",
            f"{api_base}/messages",
            headers={"Authorization": f"Bearer {mail_token}"},
            timeout=15,
        )
        if status_code == 200 and data:
            # DuckMail API 返回格式可能是 hydra:member 或 member
            messages = data.get("hydra:member") or data.get("member") or data.get("data") or []
            return messages
        return []
    except Exception as e:
        return []


def _fetch_email_detail_duckmail(mail_token: str, msg_id: str):
    """获取 DuckMail 单封邮件详情"""
    try:
        api_base = DUCKMAIL_API_BASE.rstrip("/")
        # 处理 msg_id 格式
        if isinstance(msg_id, str) and msg_id.startswith("/messages/"):
            msg_id = msg_id.split("/")[-1]

        status_code, data, _ = _duckmail_request(
            "GET",
            f"{api_base}/messages/{msg_id}",
            headers={"Authorization": f"Bearer {mail_token}"},
            timeout=15,
        )
        if status_code == 200:
            return data
    except Exception:
        pass
    return None


def _extract_verification_code(email_content: str):
    """从邮件内容提取 6 位验证码"""
    if not email_content:
        return None

    patterns = [
        r"Verification code:?\s*(\d{6})",
        r"code is\s*(\d{6})",
        r"代码为[:：]?\s*(\d{6})",
        r"验证码[:：]?\s*(\d{6})",
        r">\s*(\d{6})\s*<",
        r"(?<![#&])\b(\d{6})\b",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, email_content, re.IGNORECASE)
        for code in matches:
            if code == "177010":  # 已知误判
                continue
            return code
    return None


def wait_for_verification_email(mail_token: str, timeout: int = 120):
    """等待并提取 OpenAI 验证码"""
    start_time = time.time()

    while time.time() - start_time < timeout:
        messages = _fetch_emails_duckmail(mail_token)
        if messages and len(messages) > 0:
            # 获取最新邮件详情
            first_msg = messages[0]
            msg_id = first_msg.get("id") or first_msg.get("@id")

            if msg_id:
                detail = _fetch_email_detail_duckmail(mail_token, msg_id)
                if detail:
                    # DuckMail 的邮件内容在 text 或 html 字段
                    content = detail.get("text") or detail.get("html") or ""
                    code = _extract_verification_code(content)
                    if code:
                        return code

        time.sleep(3)

    return None


def _random_name():
    first = random.choice([
        "James", "Emma", "Liam", "Olivia", "Noah", "Ava", "Ethan", "Sophia",
        "Lucas", "Mia", "Mason", "Isabella", "Logan", "Charlotte", "Alexander",
        "Amelia", "Benjamin", "Harper", "William", "Evelyn", "Henry", "Abigail",
        "Sebastian", "Emily", "Jack", "Elizabeth",
    ])
    last = random.choice([
        "Smith", "Johnson", "Brown", "Davis", "Wilson", "Moore", "Taylor",
        "Clark", "Hall", "Young", "Anderson", "Thomas", "Jackson", "White",
        "Harris", "Martin", "Thompson", "Garcia", "Robinson", "Lewis",
        "Walker", "Allen", "King", "Wright", "Scott", "Green",
    ])
    return f"{first} {last}"


def _random_birthdate():
    y = random.randint(1985, 2002)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"


class ChatGPTRegister:
    BASE = "https://chatgpt.com"
    AUTH = "https://auth.openai.com"

    def __init__(self, proxy: str = None, tag: str = "", oauth_add_phone_sms=None,
                 browser_proxy: str = None):
        """proxy: curl_cffi / urllib API 走的代理 (可能是 landbridge URL)
        browser_proxy: playwright/sentinel 浏览器走的代理 (永远是原始上游, 不走 landbridge)。
                       None 时退化为与 proxy 相同 (兼容老调用)。"""
        self.tag = tag  # 线程标识，用于日志
        self.account_id = None
        self.email = None
        self.oauth_add_phone_sms = (
            OAUTH_ADD_PHONE_SMS if oauth_add_phone_sms is None else bool(oauth_add_phone_sms)
        )
        self.device_id = str(uuid.uuid4())
        self.auth_session_logging_id = str(uuid.uuid4())
        self.impersonate, self.chrome_major, self.chrome_full, self.ua, self.sec_ch_ua = _random_chrome_version()

        self.session = curl_requests.Session(impersonate=self.impersonate)

        self.proxy = proxy
        self.browser_proxy = browser_proxy if browser_proxy is not None else proxy
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept-Language": random.choice([
                "en-US,en;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8",
                "en,en-US;q=0.9", "en-US,en;q=0.8",
            ]),
            "sec-ch-ua": self.sec_ch_ua, "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"', "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": f'"{self.chrome_full}"',
            "sec-ch-ua-platform-version": f'"{random.randint(10, 15)}.0.0"',
        })

        self.session.cookies.set("oai-did", self.device_id, domain="chatgpt.com")
        self._callback_url = None

        _landbridge_exit_ip_banner(self.proxy, self.browser_proxy, tag=self.tag)

    def _log(self, step, method, url, status, body=None):
        prefix = f"[{self.tag}] " if self.tag else ""
        lines = [
            f"\n{'='*60}",
            f"{prefix}[Step] {step}",
            f"{prefix}[{method}] {url}",
            f"{prefix}[Status] {status}",
        ]
        if body:
            try:
                lines.append(f"{prefix}[Response] {json.dumps(body, indent=2, ensure_ascii=False)[:1000]}")
            except Exception:
                lines.append(f"{prefix}[Response] {str(body)[:1000]}")
        lines.append(f"{'='*60}")
        _monitor_emit("worker", "\n".join(lines), step=step, account=_mask_email(getattr(self, "email", "")))

    def _print(self, msg):
        prefix = f"[{self.tag}] " if self.tag else ""
        step = None
        if str(msg).startswith("[") and "]" in str(msg):
            step = str(msg)[1:str(msg).find("]")]
        _monitor_emit(
            "worker",
            f"{prefix}{msg}",
            step=step,
            account=_mask_email(getattr(self, "email", "")),
        )

    def _channel_log(self, channel_name, msg, *, level="info", step=None):
        prefix = f"[{self.tag}] " if self.tag else ""
        _monitor_emit(
            channel_name,
            f"{prefix}{msg}",
            level=level,
            step=step,
            account=_mask_email(getattr(self, "email", "")),
        )

    def _is_retryable_oauth_exception(self, exc) -> bool:
        msg = str(exc).lower()
        retry_markers = (
            "recv failure",
            "connection reset",
            "reset by peer",
            "connection aborted",
            "connection refused",
            "connection closed",
            "timed out",
            "timeout",
            "network is unreachable",
            "temporarily unavailable",
            "temporary failure",
            "proxy connect",
        )
        return any(marker in msg for marker in retry_markers)

    def _oauth_request_with_backoff(self, method: str, url: str, *, step_label: str,
                                    retries: int = 2, base_delay: float = 1.0, **kwargs):
        request_fn = getattr(self.session, method.lower())
        last_exc = None

        for attempt in range(retries + 1):
            try:
                return request_fn(url, **kwargs)
            except Exception as exc:
                last_exc = exc
                retry_index = attempt + 1
                if retry_index > retries or not self._is_retryable_oauth_exception(exc):
                    raise

                delay = base_delay * (2 ** attempt)
                self._print(
                    f"[OAuth] {step_label} 网络异常，{delay:.1f}s 后进行第 "
                    f"{retry_index}/{retries} 次重试: {exc}"
                )
                time.sleep(delay)

        raise last_exc

    # ==================== DuckMail 临时邮箱 ====================

    def create_temp_email(self):
        """创建 DuckMail 临时邮箱，返回 (email, password, mail_token)"""
        if not DUCKMAIL_BEARER:
            raise Exception("DUCKMAIL_BEARER 未设置，无法创建临时邮箱")

        # 生成随机邮箱前缀 8-13 位
        chars = string.ascii_lowercase + string.digits
        length = random.randint(8, 13)
        email_local = "".join(random.choice(chars) for _ in range(length))
        email = f"{email_local}@duckmail.sbs"
        password = _generate_password()

        api_base = DUCKMAIL_API_BASE.rstrip("/")
        headers = {"Authorization": f"Bearer {DUCKMAIL_BEARER}"}
        try:
            # 1. 创建账号
            payload = {"address": email, "password": password}
            status_code, _, raw_text = _duckmail_request(
                "POST",
                f"{api_base}/accounts",
                headers=headers,
                payload=payload,
                timeout=15,
                proxy=self.proxy,
            )

            if status_code not in [200, 201]:
                raise Exception(f"创建邮箱失败: {status_code} - {raw_text[:200]}")

            # 2. 获取 Token（用于读取邮件）
            time.sleep(0.5)
            token_payload = {"address": email, "password": password}
            token_status, token_data, _ = _duckmail_request(
                "POST",
                f"{api_base}/token",
                payload=token_payload,
                timeout=15,
                proxy=self.proxy,
            )

            if token_status == 200 and token_data:
                mail_token = token_data.get("token")
                if mail_token:
                    return email, password, mail_token

            raise Exception(f"获取邮件 Token 失败: {token_status}")

        except Exception as e:
            raise Exception(f"DuckMail 创建邮箱失败: {e}")

    def _fetch_emails_duckmail(self, mail_token: str):
        """从 DuckMail 获取邮件列表"""
        try:
            api_base = DUCKMAIL_API_BASE.rstrip("/")
            status_code, data, _ = _duckmail_request(
                "GET",
                f"{api_base}/messages",
                headers={"Authorization": f"Bearer {mail_token}"},
                timeout=15,
                proxy=self.proxy,
            )

            if status_code == 200 and data:
                messages = data.get("hydra:member") or data.get("member") or data.get("data") or []
                return messages
            return []
        except Exception:
            return []

    def _fetch_email_detail_duckmail(self, mail_token: str, msg_id: str):
        """获取 DuckMail 单封邮件详情"""
        try:
            api_base = DUCKMAIL_API_BASE.rstrip("/")
            if isinstance(msg_id, str) and msg_id.startswith("/messages/"):
                msg_id = msg_id.split("/")[-1]

            status_code, data, _ = _duckmail_request(
                "GET",
                f"{api_base}/messages/{msg_id}",
                headers={"Authorization": f"Bearer {mail_token}"},
                timeout=15,
                proxy=self.proxy,
            )

            if status_code == 200:
                return data
        except Exception:
            pass
        return None

    def _extract_verification_code(self, email_content: str):
        """从邮件内容提取 6 位验证码"""
        if not email_content:
            return None

        patterns = [
            r"Verification code:?\s*(\d{6})",
            r"code is\s*(\d{6})",
            r"代码为[:：]?\s*(\d{6})",
            r"验证码[:：]?\s*(\d{6})",
            r">\s*(\d{6})\s*<",
            r"(?<![#&])\b(\d{6})\b",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, email_content, re.IGNORECASE)
            for code in matches:
                if code == "177010":  # 已知误判
                    continue
                return code
        return None

    def wait_for_verification_email(self, mail_token: str, timeout: int = 120):
        """等待并提取 OpenAI 验证码。
        优先级: IMAP 收件池 > DuckMail > 手动输入。
        """
        # 1. QQ 自有域名 catch-all 池
        qq_pool = getattr(self, "qq_pool", None)
        qq_addr = getattr(self, "qq_pool_email", None)
        if qq_pool and qq_addr:
            since = getattr(self, "qq_pool_since", None)
            self._print(f"[OTP] (IMAP) 等待 {qq_addr} 的 OTP, 最多 {timeout}s ...")
            code = qq_pool.wait_for_otp(qq_addr, timeout=timeout, since_ts=since)
            if code:
                self._print(f"[OTP] 验证码: {code}")
            else:
                self._print(f"[OTP] 超时 ({timeout}s)")
            return code

        # 2. 指定邮箱模式 (无 mail_token) -> 手动
        if not mail_token:
            return self._prompt_otp_manually(timeout=timeout)

        # 3. DuckMail
        self._print(f"[OTP] 等待验证码邮件 (最多 {timeout}s)...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            messages = self._fetch_emails_duckmail(mail_token)
            if messages and len(messages) > 0:
                first_msg = messages[0]
                msg_id = first_msg.get("id") or first_msg.get("@id")

                if msg_id:
                    detail = self._fetch_email_detail_duckmail(mail_token, msg_id)
                    if detail:
                        content = detail.get("text") or detail.get("html") or ""
                        code = self._extract_verification_code(content)
                        if code:
                            self._print(f"[OTP] 验证码: {code}")
                            return code

            elapsed = int(time.time() - start_time)
            self._print(f"[OTP] 等待中... ({elapsed}s/{timeout}s)")
            time.sleep(3)

        self._print(f"[OTP] 超时 ({timeout}s)")
        return None

    def _prompt_otp_manually(self, timeout: int = 600):
        """让用户手动输入 6 位 OTP（指定邮箱模式下使用）"""
        prompt = f"[{self.tag}] [OTP] 请检查邮箱并输入 6 位验证码 (留空跳过): "
        for attempt in range(5):
            try:
                with _print_lock:
                    code = input(prompt).strip()
            except EOFError:
                self._print("[OTP] 标准输入已关闭，无法手动输入")
                return None
            if not code:
                self._print("[OTP] 用户跳过手动输入")
                return None
            if re.fullmatch(r"\d{6}", code):
                return code
            self._print(f"[OTP] 输入无效（需 6 位数字），剩余 {4 - attempt} 次重试")
        return None

    # ==================== 注册流程 ====================

    def visit_homepage(self):
        url = f"{self.BASE}/"
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        self._log("0. Visit homepage", "GET", url, r.status_code,
                   {"cookies_count": len(self.session.cookies)})

    def get_csrf(self) -> str:
        url = f"{self.BASE}/api/auth/csrf"
        r = self.session.get(url, headers={"Accept": "application/json", "Referer": f"{self.BASE}/"})
        data = r.json()
        token = data.get("csrfToken", "")
        self._log("1. Get CSRF", "GET", url, r.status_code, data)
        if not token:
            raise Exception("Failed to get CSRF token")
        return token

    def signin(self, email: str, csrf: str) -> str:
        url = f"{self.BASE}/api/auth/signin/openai"
        params = {
            "prompt": "login", "ext-oai-did": self.device_id,
            "auth_session_logging_id": self.auth_session_logging_id,
            "screen_hint": "login_or_signup", "login_hint": email,
        }
        form_data = {"callbackUrl": f"{self.BASE}/", "csrfToken": csrf, "json": "true"}
        r = self.session.post(url, params=params, data=form_data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "Referer": f"{self.BASE}/", "Origin": self.BASE,
        })
        data = r.json()
        authorize_url = data.get("url", "")
        self._log("2. Signin", "POST", url, r.status_code, data)
        if not authorize_url:
            raise Exception("Failed to get authorize URL")
        return authorize_url

    def authorize(self, url: str) -> str:
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        final_url = str(r.url)
        self._log("3. Authorize", "GET", url, r.status_code, {"final_url": final_url})
        return final_url

    def register(self, email: str, password: str):
        url = f"{self.AUTH}/api/accounts/user/register"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"{self.AUTH}/create-account/password", "Origin": self.AUTH}
        headers.update(_make_trace_headers())

        sentinel = _request_sentinel_token(
            "username_password_create", self.device_id, self.ua, proxy=self.browser_proxy,
        )
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
            self._print("[Sentinel] register token 已附加")
        else:
            self._print("[Sentinel] register token 获取失败，继续裸跑")

        r = self.session.post(url, json={"username": email, "password": password}, headers=headers)
        try: data = r.json()
        except Exception: data = {"text": r.text[:500]}
        self._log("4. Register", "POST", url, r.status_code, data)
        return r.status_code, data

    def send_otp(self):
        url = f"{self.AUTH}/api/accounts/email-otp/send"
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{self.AUTH}/create-account/password", "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        try: data = r.json()
        except Exception: data = {"final_url": str(r.url), "status": r.status_code}
        self._log("5. Send OTP", "GET", url, r.status_code, data)
        return r.status_code, data

    def validate_otp(self, code: str):
        url = f"{self.AUTH}/api/accounts/email-otp/validate"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"{self.AUTH}/email-verification", "Origin": self.AUTH}
        headers.update(_make_trace_headers())
        r = self.session.post(url, json={"code": code}, headers=headers)
        try: data = r.json()
        except Exception: data = {"text": r.text[:500]}
        self._log("6. Validate OTP", "POST", url, r.status_code, data)
        return r.status_code, data

    def create_account(self, name: str, birthdate: str):
        url = f"{self.AUTH}/api/accounts/create_account"
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"{self.AUTH}/about-you", "Origin": self.AUTH}
        headers.update(_make_trace_headers())

        sentinel = _request_sentinel_token(
            "oauth_create_account", self.device_id, self.ua, proxy=self.browser_proxy,
        )
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
            self._print("[Sentinel] create_account token 已附加")
        else:
            self._print("[Sentinel] create_account token 获取失败，继续裸跑")

        r = self.session.post(url, json={"name": name, "birthdate": birthdate}, headers=headers)
        try: data = r.json()
        except Exception: data = {"text": r.text[:500]}
        self._log("7. Create Account", "POST", url, r.status_code, data)
        if isinstance(data, dict):
            cb = data.get("continue_url") or data.get("url") or data.get("redirect_url")
            if cb:
                self._callback_url = cb
        return r.status_code, data

    def callback(self, url: str = None):
        if not url:
            url = self._callback_url
        if not url:
            self._print("[!] No callback URL, skipping.")
            return None, None
        r = self.session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }, allow_redirects=True)
        self._log("8. Callback", "GET", url, r.status_code, {"final_url": str(r.url)})
        return r.status_code, {"final_url": str(r.url)}

    # ==================== 自动注册主流程 ====================

    def run_register(self, email, password, name, birthdate, mail_token):
        """使用 DuckMail 的注册流程"""
        self.email = email
        self.account_id = email
        self.visit_homepage()
        _random_delay(0.3, 0.8)
        csrf = self.get_csrf()
        _random_delay(0.2, 0.5)
        auth_url = self.signin(email, csrf)
        _random_delay(0.3, 0.8)

        final_url = self.authorize(auth_url)
        final_path = urlparse(final_url).path
        _random_delay(0.3, 0.8)

        self._print(f"Authorize → {final_path}")

        need_otp = False

        if "create-account/password" in final_path:
            self._print("全新注册流程")
            _random_delay(0.5, 1.0)
            status, data = self.register(email, password)
            if status != 200:
                raise Exception(f"Register 失败 ({status}): {data}")
            # register 之后可能还需要 send_otp（全新注册流程中 OTP 不一定在 authorize 时发送）
            _random_delay(0.3, 0.8)
            self.send_otp()
            need_otp = True
        elif "email-verification" in final_path or "email-otp" in final_path:
            self._print("跳到 OTP 验证阶段 (authorize 已触发 OTP，不再重复发送)")
            # 不调用 send_otp()，因为 authorize 重定向到 email-verification 时服务器已发送 OTP
            need_otp = True
        elif "about-you" in final_path:
            self._print("跳到填写信息阶段")
            _random_delay(0.5, 1.0)
            self.create_account(name, birthdate)
            _random_delay(0.3, 0.5)
            self.callback()
            return True
        elif "callback" in final_path or "chatgpt.com" in final_url:
            self._print("账号已完成注册")
            return True
        else:
            self._print(f"未知跳转: {final_url}")
            self.register(email, password)
            self.send_otp()
            need_otp = True

        if need_otp:
            # 使用 DuckMail 等待验证码
            otp_code = self.wait_for_verification_email(mail_token)
            if not otp_code:
                raise Exception("未能获取验证码")

            _random_delay(0.3, 0.8)
            status, data = self.validate_otp(otp_code)
            if status != 200:
                self._print("验证码失败，重试...")
                self.send_otp()
                _random_delay(1.0, 2.0)
                otp_code = self.wait_for_verification_email(mail_token, timeout=60)
                if not otp_code:
                    raise Exception("重试后仍未获取验证码")
                _random_delay(0.3, 0.8)
                status, data = self.validate_otp(otp_code)
                if status != 200:
                    raise Exception(f"验证码失败 ({status}): {data}")

        _random_delay(0.5, 1.5)
        status, data = self.create_account(name, birthdate)
        if status != 200:
            raise Exception(f"Create account 失败 ({status}): {data}")
        _random_delay(0.2, 0.5)
        self.callback()
        return True

    # ==================== add_phone (SMS 验证) ====================

    def _get_sms_provider(self):
        """lazy 构造 sms provider, 失败 raise。"""
        if not get_provider:
            raise RuntimeError("sms_provider 模块未导入, 无法处理 add_phone")
        if not SMS_PROVIDER_NAME:
            raise RuntimeError("config.sms_provider 未配置")
        provider = get_provider(SMS_PROVIDER_NAME, _CONFIG)
        self._print(f"[Phone] 使用 sms provider: {provider.name}")
        return provider

    def _post_phone_send(self, phone_e164: str):
        """POST /api/accounts/add-phone/send (端点 + referer 已对齐真实 curl)"""
        url = f"{OAUTH_ISSUER}/api/accounts/add-phone/send"
        h = {
            "Accept": "application/json",
            "Accept-Language": "en",
            "Content-Type": "application/json",
            "Origin": OAUTH_ISSUER,
            "Referer": f"{OAUTH_ISSUER}/add-phone",
            "User-Agent": self.ua,
        }
        h.update(_make_trace_headers())
        body = {"phone_number": phone_e164}
        try:
            r = self.session.post(url, json=body, headers=h, timeout=30,
                                   allow_redirects=False,
                                   impersonate=self.impersonate)
        except Exception as e:
            self._print(f"[Phone] send 异常: {e}")
            return None, None
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:300]}
        self._print(f"[Phone] send {phone_e164} -> {r.status_code} {data}")
        return r.status_code, data

    def _post_phone_validate(self, code: str):
        """POST /api/accounts/phone-otp/validate (端点 + referer 已对齐真实 curl)"""
        url = f"{OAUTH_ISSUER}/api/accounts/phone-otp/validate"
        h = {
            "Accept": "application/json",
            "Accept-Language": "en",
            "Content-Type": "application/json",
            "Origin": OAUTH_ISSUER,
            "Referer": f"{OAUTH_ISSUER}/phone-verification",
            "User-Agent": self.ua,
        }
        h.update(_make_trace_headers())
        body = {"code": code}
        try:
            r = self.session.post(url, json=body, headers=h, timeout=30,
                                   allow_redirects=False,
                                   impersonate=self.impersonate)
        except Exception as e:
            self._print(f"[Phone] validate 异常: {e}")
            return None, None
        try:
            data = r.json()
        except Exception:
            data = {"text": r.text[:300]}
        self._print(f"[Phone] validate -> {r.status_code} {data}")
        return r.status_code, data

    def _handle_add_phone(self, referer_url: str):
        """走完 add_phone: 拿号→send→wait_otp→validate, 失败换号重试。

        优先走 phone_pool 复用同号注册多账号 (省钱); 池不可用时回退到原始
        provider.acquire 单号单账号路径.

        return: (continue_url, page_type) 或 (None, None) 失败。
        """
        if not self.oauth_add_phone_sms:
            raise OAuthPendingRequired(
                "OAuth 流程命中 add_phone，但当前未启用 --oauth-add-phone-sms，默认不会通过 SMS 平台接码"
            )
        pool = _get_phone_pool()
        if pool is not None:
            return self._handle_add_phone_with_pool(pool)
        # 回退: 旧路径 (不走复用池)
        return self._handle_add_phone_legacy()

    def _handle_add_phone_with_pool(self, pool):
        """复用池路径: pool.acquire_or_reuse → heartbeat → wait_otp(since_sms_ids).

        语义:
          - validate 成功 → mark_used (used_count++; 满 max_reuse 时自动 finish)
          - validate 失败 / OpenAI 拒号 / 没收到码 → mark_dead 并换号重试
        """
        from herosms_pool import HeroSmsProvider
        provider = pool.provider
        if not isinstance(provider, HeroSmsProvider):
            self._print(f"[Phone] pool provider 类型异常, 走 legacy 路径")
            return self._handle_add_phone_legacy()

        last_err = None
        for attempt in range(1, SMS_MAX_RETRIES + 1):
            self._print(f"[Phone] attempt {attempt}/{SMS_MAX_RETRIES} "
                        f"acquire_or_reuse...")
            try:
                lease = pool.acquire_or_reuse()
            except NoNumberAvailable as e:
                self._print(f"[Phone] {e}")
                last_err = e
                if attempt < SMS_MAX_RETRIES:
                    time.sleep(2)
                    continue
                return None, None
            except PhonePoolCapacityExhausted as e:
                _bump_metric("cap_skipped")
                self._print(
                    f"[Phone] pool cap exhausted {e.current_active}/{e.max_active}, "
                    "skip current account"
                )
                _bump_metric("warn")
                return None, None
            except (AcquireFailed, SmsProviderError) as e:
                self._print(f"[Phone] acquire 异常: {e}")
                last_err = e
                time.sleep(2)
                continue

            phone_e164 = "+" + str(lease.phone_number).lstrip("+")
            label = "REUSE" if lease.is_reused else "FRESH"
            self._print(f"[Phone] {label} number={phone_e164} "
                        f"handle={lease.activation_id} "
                        f"used_count={lease.used_count}/{pool.max_reuse} "
                        f"cost=${lease.cost}")

            lease.start_heartbeat()
            try:
                # OpenAI: send OTP
                send_status, send_data = self._post_phone_send(phone_e164)
                if send_status is None or send_status >= 400:
                    self._print(f"[Phone] OpenAI 拒绝该号 ({send_status}), "
                                f"mark_dead")
                    lease.mark_dead(reason=f"openai_send_{send_status}")
                    continue

                if lease.lease_lost_check():
                    self._print(f"[Phone] 租约丢失, 放弃 (别的进程在用)")
                    continue

                # OpenAI: wait OTP (跳过历史 SMS)
                baseline = lease.baseline_sms_ids()
                self._print(f"[Phone] 等待 SMS, 最多 {SMS_WAIT_OTP_TIMEOUT}s "
                            f"(skip {len(baseline)} 历史 sms_id)")
                wait_exc = None
                try:
                    result = provider.wait_otp_with_id(
                        lease.to_session(), timeout=SMS_WAIT_OTP_TIMEOUT,
                        poll_interval=SMS_POLL_INTERVAL,
                        log=lambda s: self._channel_log("sms", s, step="Phone"),
                        since_sms_ids=baseline,
                        on_lease_lost=lease.lease_lost_check,
                    )
                except Exception as e:
                    self._print(f"[Phone] wait_otp 异常: {e}")
                    wait_exc = e
                    result = None

                if not result:
                    if lease.lease_lost_check():
                        self._print(f"[Phone] 租约丢失中途退出, 不 mark_dead")
                        # 租约已经被别人续上, 别清别人的状态; 直接换号
                        # (心跳线程已经把 _released=False 但没动 DB)
                        lease._released = True
                        lease.stop_heartbeat()
                    elif wait_exc is not None:
                        # poll 循环抛异常 (transport/TLS/网络/未知 bug) → 号本身没问题。
                        # 不 mark_dead, 还回池子让别的 worker 复用; 当前 worker 换号重试。
                        self._print(f"[Phone] wait_otp 抛异常, 释放 lease (不 mark_dead) "
                                    f"留给后续复用")
                        lease.release_lease_only()
                    else:
                        self._print(f"[Phone] 没收到 SMS, mark_dead")
                        lease.mark_dead(reason="no_otp_2min")
                    continue
                otp, sms_id = result

                # OpenAI: validate
                self._print(f"[Phone] 收到 OTP={otp} sms_id={sms_id}, 提交验证")
                v_status, v_data = self._post_phone_validate(otp)
                if v_status == 200:
                    account_id = getattr(self, "account_id", None) \
                        or getattr(self, "email", None)
                    lease.mark_used(sms_id=sms_id, code=otp,
                                     account_id=account_id)
                    continue_url = (v_data or {}).get("continue_url", "")
                    page_type = ((v_data or {}).get("page") or {}).get("type", "")
                    self._print(f"[Phone] OK page={page_type} "
                                f"next={continue_url[:140]}")
                    return continue_url, page_type

                # validate 失败 → 算消耗, 直接 dead
                self._print(f"[Phone] validate 失败 status={v_status}, mark_dead")
                lease.mark_dead(reason=f"validate_{v_status}")
            finally:
                # 兜底: 如果上面任何分支 raise, 至少把心跳停掉
                if not lease._released:
                    self._print(f"[Phone] 异常退出, 释放 lease")
                    try:
                        lease.mark_dead(reason="exception")
                    except Exception:
                        lease.stop_heartbeat()

        self._print(f"[Phone] 重试 {SMS_MAX_RETRIES} 次仍失败"
                    + (f" last_err={last_err}" if last_err else ""))
        return None, None

    def _handle_add_phone_legacy(self):
        """旧路径: provider.acquire 单号单账号 (quackr / pool 不可用时用)。"""
        try:
            provider = self._get_sms_provider()
        except Exception as e:
            self._print(f"[Phone] provider 初始化失败: {e}")
            return None, None

        last_err = None
        for attempt in range(1, SMS_MAX_RETRIES + 1):
            self._print(f"[Phone] attempt {attempt}/{SMS_MAX_RETRIES} acquire...")
            try:
                sess = provider.acquire()
            except NoNumberAvailable as e:
                self._print(f"[Phone] {e}")
                last_err = e
                if attempt < SMS_MAX_RETRIES:
                    time.sleep(2)
                    continue
                return None, None
            except (AcquireFailed, SmsProviderError) as e:
                self._print(f"[Phone] acquire 异常: {e}")
                last_err = e
                time.sleep(2)
                continue

            phone_e164 = "+" + str(sess.number).lstrip("+")
            self._print(f"[Phone] got number={phone_e164} handle={sess.handle} "
                        f"cost=${sess.cost}")

            send_status, send_data = self._post_phone_send(phone_e164)
            if send_status is None or send_status >= 400:
                self._print(f"[Phone] OpenAI 拒绝该号, release_no_sms 换号")
                try:
                    provider.release_no_sms(sess)
                except Exception as e:
                    self._print(f"[Phone] release_no_sms 异常: {e}")
                continue

            self._print(f"[Phone] 等待 SMS, 最多 {SMS_WAIT_OTP_TIMEOUT}s")
            wait_exc = None
            try:
                otp = provider.wait_otp(
                    sess, timeout=SMS_WAIT_OTP_TIMEOUT,
                    poll_interval=SMS_POLL_INTERVAL,
                    log=lambda s: self._channel_log("sms", s, step="Phone"))
            except Exception as e:
                self._print(f"[Phone] wait_otp 异常: {e}")
                wait_exc = e
                otp = None

            if not otp:
                if wait_exc is not None:
                    # transport/网络异常 → 号没毛病, 别 cancel 烧钱; 让号自然过期/复用
                    self._print(f"[Phone] wait_otp 抛异常, 不 release_no_sms, 直接换号")
                else:
                    self._print(f"[Phone] 没收到 SMS, release_no_sms 换号")
                    try:
                        provider.release_no_sms(sess)
                    except Exception as e:
                        self._print(f"[Phone] release_no_sms 异常: {e}")
                continue

            self._print(f"[Phone] 收到 OTP={otp}, 提交验证")
            v_status, v_data = self._post_phone_validate(otp)
            if v_status == 200:
                try:
                    provider.release_ok(sess)
                except Exception as e:
                    self._print(f"[Phone] release_ok 异常 (忽略): {e}")
                continue_url = (v_data or {}).get("continue_url", "")
                page_type = ((v_data or {}).get("page") or {}).get("type", "")
                self._print(f"[Phone] OK page={page_type} next={continue_url[:140]}")
                return continue_url, page_type

            self._print(f"[Phone] validate 失败 status={v_status}, 换号重试")
            try:
                provider.release_bad(sess, reason=f"validate={v_status}")
            except Exception as e:
                self._print(f"[Phone] release_bad 异常: {e}")

        self._print(f"[Phone] 重试 {SMS_MAX_RETRIES} 次仍失败"
                    + (f" last_err={last_err}" if last_err else ""))
        return None, None

    def _decode_oauth_session_cookie(self):
        """解码 oai-client-auth-session cookie。
        参考 GptCrate: cookie 是 JWT (header.payload.signature),
        workspaces 信息在 payload 段, 需遍历所有段找到含 'workspaces' 的那段。
        多个同名 cookie (不同 domain) 时, 优先返回有非空 workspaces 的。
        """
        from urllib.parse import unquote

        def _try_decode_segment(seg: str):
            seg = (seg or "").strip()
            if not seg:
                return None
            pad = "=" * ((4 - len(seg) % 4) % 4)
            try:
                raw = base64.urlsafe_b64decode((seg + pad).encode("ascii"))
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else None
            except Exception:
                return None

        jar = getattr(self.session.cookies, "jar", None)
        cookie_items = list(jar) if jar is not None else []

        fallback = None  # 任何能解码的 dict, 没找到带 workspaces 的就用它
        for c in cookie_items:
            name = getattr(c, "name", "") or ""
            if "oai-client-auth-session" not in name:
                continue

            raw_val = (getattr(c, "value", "") or "").strip()
            if not raw_val:
                continue

            candidates = [raw_val]
            try:
                decoded = unquote(raw_val)
                if decoded != raw_val:
                    candidates.append(decoded)
            except Exception:
                pass

            for val in candidates:
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                # 遍历所有 JWT 段, 优先返回带非空 workspaces 的那段
                for part in val.split("."):
                    data = _try_decode_segment(part)
                    if not data:
                        continue
                    if data.get("workspaces"):
                        return data
                    if fallback is None:
                        fallback = data
        return fallback

    def _oauth_allow_redirect_extract_code(self, url: str, referer: str = None):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": self.ua,
        }
        if referer:
            headers["Referer"] = referer

        try:
            resp = self._oauth_request_with_backoff(
                "get",
                url,
                step_label="allow_redirect",
                headers=headers,
                allow_redirects=True,
                timeout=30,
                impersonate=self.impersonate,
            )
            final_url = str(resp.url)
            code = _extract_code_from_url(final_url)
            if code:
                self._print("[OAuth] allow_redirect 命中最终 URL code")
                return code

            for r in getattr(resp, "history", []) or []:
                loc = r.headers.get("Location", "")
                code = _extract_code_from_url(loc)
                if code:
                    self._print("[OAuth] allow_redirect 命中 history Location code")
                    return code
                code = _extract_code_from_url(str(r.url))
                if code:
                    self._print("[OAuth] allow_redirect 命中 history URL code")
                    return code
        except Exception as e:
            maybe_localhost = re.search(r'(https?://localhost[^\s\'\"]+)', str(e))
            if maybe_localhost:
                code = _extract_code_from_url(maybe_localhost.group(1))
                if code:
                    self._print("[OAuth] allow_redirect 从 localhost 异常提取 code")
                    return code
            self._print(f"[OAuth] allow_redirect 异常: {e}")

        return None

    def _oauth_follow_for_code(self, start_url: str, referer: str = None, max_hops: int = 16):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": self.ua,
        }
        if referer:
            headers["Referer"] = referer

        current_url = start_url
        last_url = start_url

        for hop in range(max_hops):
            try:
                resp = self._oauth_request_with_backoff(
                    "get",
                    current_url,
                    step_label=f"follow[{hop + 1}]",
                    headers=headers,
                    allow_redirects=False,
                    timeout=30,
                    impersonate=self.impersonate,
                )
            except Exception as e:
                maybe_localhost = re.search(r'(https?://localhost[^\s\'\"]+)', str(e))
                if maybe_localhost:
                    code = _extract_code_from_url(maybe_localhost.group(1))
                    if code:
                        self._print(f"[OAuth] follow[{hop + 1}] 命中 localhost 回调")
                        return code, maybe_localhost.group(1)
                self._print(f"[OAuth] follow[{hop + 1}] 请求异常: {e}")
                return None, last_url

            last_url = str(resp.url)
            self._print(f"[OAuth] follow[{hop + 1}] {resp.status_code} {last_url[:140]}")
            code = _extract_code_from_url(last_url)
            if code:
                return code, last_url

            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", "")
                if not loc:
                    return None, last_url
                if loc.startswith("/"):
                    loc = f"{OAUTH_ISSUER}{loc}"
                self._print(f"[OAuth] follow[{hop + 1}] -> Location={loc[:160]}")
                code = _extract_code_from_url(loc)
                if not code:
                    # localhost callback URL 兜底: query 解析失败时用 regex 直接抓 code=
                    m = re.search(r"[?&]code=([^&\s'\"]+)", loc)
                    if m:
                        code = m.group(1)
                if code:
                    return code, loc
                # 如果 Location 指向 localhost 但没抓到 code, 不要再去 GET (会被代理超时 30s)
                if loc.startswith("http://localhost") or loc.startswith("https://localhost"):
                    self._print(f"[OAuth] follow[{hop + 1}] Location 是 localhost 但无 code, 终止")
                    return None, loc
                current_url = loc
                headers["Referer"] = last_url
                continue

            return None, last_url

        return None, last_url

    def _oauth_submit_workspace_and_org(self, consent_url: str):
        # 先 GET 一次 consent 页面, 让服务端 set-cookie 把最新 oai-client-auth-session
        # (含本次 add_phone/account_create 之后真实的 workspaces) 落到本地 jar 里,
        # 否则可能拿到 add_phone 之前的陈旧 session, workspace_id 已失效 → 400
        try:
            self._oauth_request_with_backoff(
                "get",
                consent_url,
                step_label="预 GET consent",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": f"{OAUTH_ISSUER}/log-in/password",
                    "Upgrade-Insecure-Requests": "1",
                    "User-Agent": self.ua,
                },
                allow_redirects=True,
                timeout=20,
                impersonate=self.impersonate,
            )
        except Exception as e:
            self._print(f"[OAuth] 预 GET consent 失败 (忽略): {e}")

        session_data = self._decode_oauth_session_cookie()
        if not session_data:
            jar = getattr(self.session.cookies, "jar", None)
            if jar is not None:
                cookie_names = [getattr(c, "name", "") for c in list(jar)]
            else:
                cookie_names = list(self.session.cookies.keys())
            self._print(f"[OAuth] 无法解码 oai-client-auth-session, cookies={cookie_names[:12]}")
            return None

        workspaces = session_data.get("workspaces", [])
        if not workspaces:
            self._print(f"[OAuth] session 中没有 workspace 信息, keys={list(session_data.keys())[:10]}")
            return None

        workspace_id = (workspaces[0] or {}).get("id")
        if not workspace_id:
            self._print(f"[OAuth] workspace_id 为空, workspaces[0]={workspaces[0]}")
            return None
        self._print(f"[OAuth] 选用 workspace_id={workspace_id}")

        h = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": OAUTH_ISSUER,
            "Referer": consent_url,
            "User-Agent": self.ua,
            "oai-device-id": self.device_id,
        }
        h.update(_make_trace_headers())

        def _is_duplicate_error(text: str) -> bool:
            t = (text or "").lower()
            return ('"code": "duplicate"' in t
                    or '"code":"duplicate"' in t
                    or "already has a default project" in t
                    or "already has" in t and "project" in t)

        def _advance_via_authorize(referer: str):
            # workspace/org 选择已在服务端推进 (200 / 3xx / duplicate),
            # 重新 GET authorize_url, 让服务端经 consent → consent_verifier
            # → http://localhost:1455/auth/callback?code=... 自己跳完
            authorize_url = getattr(self, "_current_authorize_url", None)
            if authorize_url:
                code = self._oauth_allow_redirect_extract_code(authorize_url, referer=referer)
                if code:
                    self._print("[OAuth] advance: authorize_url 命中 callback code")
                    return code
                code, _ = self._oauth_follow_for_code(authorize_url, referer=referer)
                if code:
                    self._print("[OAuth] advance: authorize_url follow 命中 code")
                    return code
            return None

        resp = self._oauth_request_with_backoff(
            "post",
            f"{OAUTH_ISSUER}/api/accounts/workspace/select",
            step_label="POST /api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=h,
            allow_redirects=False,
            timeout=30,
            impersonate=self.impersonate,
        )
        self._print(f"[OAuth] workspace/select -> {resp.status_code}")

        ws_data = {}
        ws_next = ""
        ws_page = ""

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if loc.startswith("/"):
                loc = f"{OAUTH_ISSUER}{loc}"
            code = _extract_code_from_url(loc)
            if code:
                return code
            code, _ = self._oauth_follow_for_code(loc, referer=consent_url)
            if not code:
                code = self._oauth_allow_redirect_extract_code(loc, referer=consent_url)
            if code:
                return code
            # 3xx 跟完没 code, 进 advance 兜底
            return _advance_via_authorize(consent_url)

        if resp.status_code == 400 and _is_duplicate_error(resp.text):
            self._print("[OAuth] workspace/select 已幂等完成 (duplicate), 走 authorize 推进")
            return _advance_via_authorize(consent_url)

        if resp.status_code != 200:
            self._print(f"[OAuth] workspace/select 失败: {resp.status_code} {resp.text[:240]}")
            return None

        try:
            ws_data = resp.json()
        except Exception:
            self._print("[OAuth] workspace/select 响应不是 JSON, 走 authorize 推进")
            return _advance_via_authorize(consent_url)

        ws_next = ws_data.get("continue_url", "")
        orgs = ws_data.get("data", {}).get("orgs", []) or ws_data.get("orgs", []) or []
        ws_page = (ws_data.get("page") or {}).get("type", "")
        self._print(f"[OAuth] workspace/select page={ws_page or '-'} next={(ws_next or '-')[:140]}")
        self._print(f"[OAuth] workspace/select keys={list(ws_data.keys())} "
                    f"data_keys={list((ws_data.get('data') or {}).keys())} orgs_len={len(orgs)}")

        # 仅在服务端真的把页面推到 org 选择态、且确实有多个 org 需要选时, 才发 organization/select
        # 单 org + 默认 project 的新号: 服务端早已 auto-select, 再发只会得到 duplicate, 直接走 advance
        need_org_select = (
            ws_page == "sign_in_with_chatgpt_codex_org"
            and len(orgs) > 1
        )

        if need_org_select:
            org_id = (orgs[0] or {}).get("id")
            projects = (orgs[0] or {}).get("projects", []) or []
            project_id = (projects[0] or {}).get("id") if projects else None

            if not org_id:
                self._print("[OAuth] org_id 为空, 跳过 organization/select, 直接 advance")
                return _advance_via_authorize(consent_url) or self._fallback_follow_ws_next(ws_next, consent_url)

            org_body = {"org_id": org_id}
            if project_id:
                org_body["project_id"] = project_id

            h_org = dict(h)
            if ws_next:
                h_org["Referer"] = ws_next if ws_next.startswith("http") else f"{OAUTH_ISSUER}{ws_next}"

            resp_org = self._oauth_request_with_backoff(
                "post",
                f"{OAUTH_ISSUER}/api/accounts/organization/select",
                step_label="POST /api/accounts/organization/select",
                json=org_body,
                headers=h_org,
                allow_redirects=False,
                timeout=30,
                impersonate=self.impersonate,
            )
            self._print(f"[OAuth] organization/select -> {resp_org.status_code}")

            if resp_org.status_code in (301, 302, 303, 307, 308):
                loc = resp_org.headers.get("Location", "")
                if loc.startswith("/"):
                    loc = f"{OAUTH_ISSUER}{loc}"
                code = _extract_code_from_url(loc)
                if code:
                    return code
                code, _ = self._oauth_follow_for_code(loc, referer=h_org.get("Referer"))
                if not code:
                    code = self._oauth_allow_redirect_extract_code(loc, referer=h_org.get("Referer"))
                if code:
                    return code

            elif resp_org.status_code == 400 and _is_duplicate_error(resp_org.text):
                self._print("[OAuth] organization/select 已幂等完成 (duplicate), 走 authorize 推进")

            elif resp_org.status_code == 200:
                self._print("[OAuth] organization/select 200, 走 authorize 推进")

            else:
                self._print(f"[OAuth] organization/select 非预期: {resp_org.status_code} "
                            f"body={resp_org.text[:240]}")

        # 优先级 1: ws_next 已经是带 login_verifier 的 oauth2/auth (page=external_url),
        # 这是服务端给的规范链路, 直接 follow 就能拿到 code (HAR 验证过)
        # 必须先于 advance, 因为 advance 带 prompt=login 会强制重登录, 把 cookie state 搞坏
        ws_next_full = ws_next
        if ws_next_full.startswith("/"):
            ws_next_full = f"{OAUTH_ISSUER}{ws_next_full}"

        if ws_next_full and (
            ws_page == "external_url"
            or "/api/oauth/oauth2/auth" in ws_next_full
            or "login_verifier=" in ws_next_full
            or "consent_verifier=" in ws_next_full
        ):
            self._print(f"[OAuth] ws_next 是规范 oauth 链路, 优先 follow: {ws_next_full[:160]}")
            code, _ = self._oauth_follow_for_code(ws_next_full, referer=consent_url)
            if code:
                return code
            code = self._oauth_allow_redirect_extract_code(ws_next_full, referer=consent_url)
            if code:
                return code

        # 优先级 2: 兜底用 advance 重新 GET authorize_url
        code = _advance_via_authorize(consent_url)
        if code:
            return code

        # 优先级 3: 老路径 follow ws_next (即使不像规范链路, 也再试一次)
        return self._fallback_follow_ws_next(ws_next, consent_url)

    def _fallback_follow_ws_next(self, ws_next: str, consent_url: str):
        if not ws_next:
            return None
        if ws_next.startswith("/"):
            ws_next = f"{OAUTH_ISSUER}{ws_next}"
        code, _ = self._oauth_follow_for_code(ws_next, referer=consent_url)
        if not code:
            code = self._oauth_allow_redirect_extract_code(ws_next, referer=consent_url)
        return code

    def perform_codex_oauth_login_http(self, email: str, password: str, mail_token: str = None):
        self._print("[OAuth] 开始执行 Codex OAuth 纯协议流程...")

        # 兼容两种 domain 形式，确保 auth 域也带 oai-did
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

        code_verifier, code_challenge = _generate_pkce()
        state = secrets.token_urlsafe(24)

        authorize_params = {
            "response_type": "code",
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": "openid email profile offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            # codex CLI 简化流: 服务端会自动处理 workspace/org/project, 直接 302 出 consent_verifier
            # 不带这三个 flag 时 workspace/select 之后会被强制进入 codex_org 页面,
            # 而 organization/select 又会因 add_phone 已建过默认 project 而返回 duplicate
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
            "prompt": "login",
        }
        authorize_url = f"{OAUTH_ISSUER}/oauth/authorize?{urlencode(authorize_params)}"
        self._current_authorize_url = authorize_url

        def _oauth_json_headers(referer: str):
            h = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": OAUTH_ISSUER,
                "Referer": referer,
                "User-Agent": self.ua,
                "oai-device-id": self.device_id,
            }
            h.update(_make_trace_headers())
            return h

        def _bootstrap_oauth_session():
            self._print("[OAuth] 1/7 GET /oauth/authorize")
            try:
                r = self._oauth_request_with_backoff(
                    "get",
                    authorize_url,
                    step_label="GET /oauth/authorize",
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": f"{self.BASE}/",
                        "Upgrade-Insecure-Requests": "1",
                        "User-Agent": self.ua,
                    },
                    allow_redirects=True,
                    timeout=30,
                    impersonate=self.impersonate,
                )
            except Exception as e:
                self._print(f"[OAuth] /oauth/authorize 异常: {e}")
                return False, ""

            final_url = str(r.url)
            redirects = len(getattr(r, "history", []) or [])
            self._print(f"[OAuth] /oauth/authorize -> {r.status_code}, final={(final_url or '-')[:140]}, redirects={redirects}")

            has_login = any(getattr(c, "name", "") == "login_session" for c in self.session.cookies)
            self._print(f"[OAuth] login_session: {'已获取' if has_login else '未获取'}")

            if not has_login:
                self._print("[OAuth] 未拿到 login_session，尝试访问 oauth2 auth 入口")
                oauth2_url = f"{OAUTH_ISSUER}/api/oauth/oauth2/auth"
                try:
                    r2 = self._oauth_request_with_backoff(
                        "get",
                        oauth2_url,
                        step_label="GET /api/oauth/oauth2/auth",
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": authorize_url,
                            "Upgrade-Insecure-Requests": "1",
                            "User-Agent": self.ua,
                        },
                        params=authorize_params,
                        allow_redirects=True,
                        timeout=30,
                        impersonate=self.impersonate,
                    )
                    final_url = str(r2.url)
                    redirects2 = len(getattr(r2, "history", []) or [])
                    self._print(f"[OAuth] /api/oauth/oauth2/auth -> {r2.status_code}, final={(final_url or '-')[:140]}, redirects={redirects2}")
                except Exception as e:
                    self._print(f"[OAuth] /api/oauth/oauth2/auth 异常: {e}")

                has_login = any(getattr(c, "name", "") == "login_session" for c in self.session.cookies)
                self._print(f"[OAuth] login_session(重试): {'已获取' if has_login else '未获取'}")

            return has_login, final_url

        def _post_authorize_continue(referer_url: str):
            sentinel_authorize = _request_sentinel_token(
                "authorize_continue", self.device_id, self.ua, proxy=self.browser_proxy,
            )
            if not sentinel_authorize:
                self._print("[OAuth] authorize_continue 的 sentinel token 获取失败")
                return None

            headers_continue = _oauth_json_headers(referer_url)
            headers_continue["openai-sentinel-token"] = sentinel_authorize

            try:
                return self._oauth_request_with_backoff(
                    "post",
                    f"{OAUTH_ISSUER}/api/accounts/authorize/continue",
                    step_label="POST /api/accounts/authorize/continue",
                    json={"username": {"kind": "email", "value": email}},
                    headers=headers_continue,
                    timeout=30,
                    allow_redirects=False,
                    impersonate=self.impersonate,
                )
            except Exception as e:
                self._print(f"[OAuth] authorize/continue 异常: {e}")
                return None

        has_login_session, authorize_final_url = _bootstrap_oauth_session()
        if not authorize_final_url:
            return None

        continue_referer = authorize_final_url if authorize_final_url.startswith(OAUTH_ISSUER) else f"{OAUTH_ISSUER}/log-in"

        self._print("[OAuth] 2/7 POST /api/accounts/authorize/continue")
        resp_continue = _post_authorize_continue(continue_referer)
        if resp_continue is None:
            return None

        self._print(f"[OAuth] /authorize/continue -> {resp_continue.status_code}")
        if resp_continue.status_code == 400 and "invalid_auth_step" in (resp_continue.text or ""):
            self._print("[OAuth] invalid_auth_step，重新 bootstrap 后重试一次")
            has_login_session, authorize_final_url = _bootstrap_oauth_session()
            if not authorize_final_url:
                return None
            continue_referer = authorize_final_url if authorize_final_url.startswith(OAUTH_ISSUER) else f"{OAUTH_ISSUER}/log-in"
            resp_continue = _post_authorize_continue(continue_referer)
            if resp_continue is None:
                return None
            self._print(f"[OAuth] /authorize/continue(重试) -> {resp_continue.status_code}")

        if resp_continue.status_code != 200:
            self._print(f"[OAuth] 邮箱提交失败: {resp_continue.text[:180]}")
            return None

        try:
            continue_data = resp_continue.json()
        except Exception:
            self._print("[OAuth] authorize/continue 响应解析失败")
            return None

        continue_url = continue_data.get("continue_url", "")
        page_type = (continue_data.get("page") or {}).get("type", "")
        self._print(f"[OAuth] continue page={page_type or '-'} next={(continue_url or '-')[:140]}")

        self._print("[OAuth] 3/7 POST /api/accounts/password/verify")
        sentinel_pwd = _request_sentinel_token(
            "password_verify", self.device_id, self.ua, proxy=self.browser_proxy,
        )
        if not sentinel_pwd:
            self._print("[OAuth] password_verify 的 sentinel token 获取失败")
            return None

        headers_verify = _oauth_json_headers(f"{OAUTH_ISSUER}/log-in/password")
        headers_verify["openai-sentinel-token"] = sentinel_pwd

        try:
            resp_verify = self._oauth_request_with_backoff(
                "post",
                f"{OAUTH_ISSUER}/api/accounts/password/verify",
                step_label="POST /api/accounts/password/verify",
                json={"password": password},
                headers=headers_verify,
                timeout=30,
                allow_redirects=False,
                impersonate=self.impersonate,
            )
        except Exception as e:
            self._print(f"[OAuth] password/verify 异常: {e}")
            return None

        self._print(f"[OAuth] /password/verify -> {resp_verify.status_code}")
        if resp_verify.status_code != 200:
            self._print(f"[OAuth] 密码校验失败: {resp_verify.text[:180]}")
            return None

        try:
            verify_data = resp_verify.json()
        except Exception:
            self._print("[OAuth] password/verify 响应解析失败")
            return None

        continue_url = verify_data.get("continue_url", "") or continue_url
        page_type = (verify_data.get("page") or {}).get("type", "") or page_type
        self._print(f"[OAuth] verify page={page_type or '-'} next={(continue_url or '-')[:140]}")

        # MFA challenge (密码通过后服务端要求二次验证, 走邮件 OTP 因子)
        need_mfa_challenge = (
            page_type == "mfa_challenge"
            or "/mfa-challenge/" in (continue_url or "")
        )
        if need_mfa_challenge:
            self._print("[OAuth] 4/7 检测到 MFA challenge, 走邮件 OTP 因子")

            session_data = self._decode_oauth_session_cookie() or {}
            factors = (
                session_data.get("mfa_challenge_factors")
                or session_data.get("mfa_factors")
                or []
            )
            email_factor = next(
                (f for f in factors if (f or {}).get("factor_type") == "email"),
                None,
            )
            if not email_factor:
                self._print(f"[OAuth] mfa_challenge 未找到 email 因子, "
                            f"factors={[(f or {}).get('factor_type') for f in factors]}")
                return None
            factor_id = email_factor.get("id") or "email-otp"
            self._print(f"[OAuth] 选用 MFA email 因子 id={factor_id}")

            headers_mfa = _oauth_json_headers(f"{OAUTH_ISSUER}/mfa-challenge")
            try:
                resp_issue = self._oauth_request_with_backoff(
                    "post",
                    f"{OAUTH_ISSUER}/api/accounts/mfa/issue_challenge",
                    step_label="POST /api/accounts/mfa/issue_challenge",
                    json={"id": factor_id, "type": "email", "force_fresh_challenge": False},
                    headers=headers_mfa,
                    timeout=30,
                    allow_redirects=False,
                    impersonate=self.impersonate,
                )
            except Exception as e:
                self._print(f"[OAuth] mfa/issue_challenge 异常: {e}")
                return None
            self._print(f"[OAuth] /mfa/issue_challenge -> {resp_issue.status_code}")
            if resp_issue.status_code not in (200, 201):
                self._print(f"[OAuth] mfa/issue_challenge 失败: {resp_issue.text[:200]}")
                return None

            headers_verify_mfa = _oauth_json_headers(f"{OAUTH_ISSUER}/mfa-challenge/email-otp")
            tried_codes = set()
            mfa_success = False
            mfa_deadline = time.time() + 120

            qq_pool = getattr(self, "qq_pool", None)
            qq_addr = getattr(self, "qq_pool_email", None)
            qq_since = max(time.time() - 10, getattr(self, "qq_pool_since", 0) or 0)
            self.qq_pool_since = qq_since

            def _submit_mfa_code(code_):
                try:
                    r_ = self._oauth_request_with_backoff(
                        "post",
                        f"{OAUTH_ISSUER}/api/accounts/mfa/verify",
                        step_label="POST /api/accounts/mfa/verify",
                        json={"id": factor_id, "type": "email", "code": code_},
                        headers=headers_verify_mfa,
                        timeout=30,
                        allow_redirects=False,
                        impersonate=self.impersonate,
                    )
                except Exception as e:
                    self._print(f"[OAuth] mfa/verify 异常: {e}")
                    return None
                self._print(f"[OAuth] /mfa/verify -> {r_.status_code}")
                if r_.status_code != 200:
                    self._print(f"[OAuth] MFA OTP 无效: {r_.text[:160]}")
                    return None
                try:
                    return r_.json()
                except Exception:
                    self._print("[OAuth] mfa/verify 响应解析失败")
                    return None

            if qq_pool and qq_addr:
                self._print(f"[OAuth] (IMAP) MFA OTP 基线时间戳设为 {int(qq_since)}")
                while not mfa_success and time.time() < mfa_deadline:
                    items = qq_pool.get_messages_since(qq_addr, since_ts=qq_since or 0)
                    candidate_codes = []
                    for item in items[:12]:
                        code_ = _qq_extract_otp(item.get("body", "")) if _qq_extract_otp else None
                        if code_ and code_ not in tried_codes:
                            candidate_codes.append(code_)
                    if not candidate_codes:
                        elapsed = int(120 - max(0, mfa_deadline - time.time()))
                        self._print(f"[OAuth] (IMAP) MFA OTP 等待中... ({elapsed}s/120s)")
                        time.sleep(2)
                        continue
                    for otp_code in candidate_codes:
                        tried_codes.add(otp_code)
                        self._print(f"[OAuth] (IMAP) 尝试 MFA OTP: {otp_code}")
                        data = _submit_mfa_code(otp_code)
                        if data is None:
                            continue
                        continue_url = data.get("continue_url", "") or continue_url
                        page_type = (data.get("page") or {}).get("type", "") or page_type
                        self._print(f"[OAuth] MFA OTP 验证通过 page={page_type or '-'} "
                                    f"next={(continue_url or '-')[:140]}")
                        mfa_success = True
                        break
                    if not mfa_success:
                        time.sleep(2)
                if not mfa_success:
                    self._print(f"[OAuth] (IMAP) MFA OTP 验证失败, 已尝试 {len(tried_codes)} 个")
                    return None

            elif not mail_token:
                while time.time() < mfa_deadline and not mfa_success:
                    manual_code = self._prompt_otp_manually(
                        timeout=int(max(0, mfa_deadline - time.time()))
                    )
                    if not manual_code:
                        self._print("[OAuth] 用户未提供 MFA OTP, 放弃")
                        return None
                    if manual_code in tried_codes:
                        self._print("[OAuth] 该 MFA OTP 已尝试过, 请重新输入")
                        continue
                    tried_codes.add(manual_code)
                    data = _submit_mfa_code(manual_code)
                    if data is None:
                        continue
                    continue_url = data.get("continue_url", "") or continue_url
                    page_type = (data.get("page") or {}).get("type", "") or page_type
                    self._print(f"[OAuth] MFA OTP 验证通过 page={page_type or '-'} "
                                f"next={(continue_url or '-')[:140]}")
                    mfa_success = True
                if not mfa_success:
                    self._print("[OAuth] 手动 MFA OTP 验证失败")
                    return None

            else:
                while not mfa_success and time.time() < mfa_deadline:
                    messages = self._fetch_emails_duckmail(mail_token) or []
                    candidate_codes = []
                    for msg in messages[:12]:
                        msg_id = msg.get("id") or msg.get("@id")
                        if not msg_id:
                            continue
                        detail = self._fetch_email_detail_duckmail(mail_token, msg_id)
                        if not detail:
                            continue
                        content = detail.get("text") or detail.get("html") or ""
                        code_ = self._extract_verification_code(content)
                        if code_ and code_ not in tried_codes:
                            candidate_codes.append(code_)
                    if not candidate_codes:
                        elapsed = int(120 - max(0, mfa_deadline - time.time()))
                        self._print(f"[OAuth] MFA OTP 等待中... ({elapsed}s/120s)")
                        time.sleep(2)
                        continue
                    for otp_code in candidate_codes:
                        tried_codes.add(otp_code)
                        self._print(f"[OAuth] 尝试 MFA OTP: {otp_code}")
                        data = _submit_mfa_code(otp_code)
                        if data is None:
                            continue
                        continue_url = data.get("continue_url", "") or continue_url
                        page_type = (data.get("page") or {}).get("type", "") or page_type
                        self._print(f"[OAuth] MFA OTP 验证通过 page={page_type or '-'} "
                                    f"next={(continue_url or '-')[:140]}")
                        mfa_success = True
                        break
                    if not mfa_success:
                        time.sleep(2)
                if not mfa_success:
                    self._print(f"[OAuth] MFA OTP 验证失败, 已尝试 {len(tried_codes)} 个")
                    return None

        need_oauth_otp = (
            page_type == "email_otp_verification"
            or "email-verification" in (continue_url or "")
            or ("email-otp" in (continue_url or "") and "/mfa-challenge/" not in (continue_url or ""))
        )

        if need_oauth_otp:
            self._print("[OAuth] 4/7 检测到邮箱 OTP 验证")

            headers_otp = _oauth_json_headers(f"{OAUTH_ISSUER}/email-verification")
            tried_codes = set()
            otp_success = False
            otp_deadline = time.time() + 120

            # IMAP 收件池: 复用 candidate_codes 模式 (OpenAI 可能多次重发, 取最新)
            qq_pool = getattr(self, "qq_pool", None)
            qq_addr = getattr(self, "qq_pool_email", None)
            # OAuth 阶段触发的是一封"新"OTP, 必须忽略注册阶段或上一次 OAuth 留下的旧 OTP。
            # 取"现在 - 10s"作为基线: -10s 容忍邮件路由延迟 (CF/QQ 投递可能有几秒抖动)。
            # 如果调用方显式设过 qq_pool_since (注册阶段), 这里强制覆盖为新基线。
            qq_since = max(time.time() - 10, getattr(self, "qq_pool_since", 0) or 0)
            self.qq_pool_since = qq_since  # 让 wait_for_verification_email 等其它路径也共用
            if qq_pool and qq_addr:
                self._print(f"[OAuth] (IMAP) OTP 基线时间戳设为 {int(qq_since)} (只看之后到达的邮件)")
                while not otp_success and time.time() < otp_deadline:
                    items = qq_pool.get_messages_since(qq_addr, since_ts=qq_since or 0)
                    candidate_codes = []
                    for item in items[:12]:
                        code = _qq_extract_otp(item.get("body", "")) if _qq_extract_otp else None
                        if code and code not in tried_codes:
                            candidate_codes.append(code)
                    if not candidate_codes:
                        elapsed = int(120 - max(0, otp_deadline - time.time()))
                        self._print(f"[OAuth] (IMAP) OTP 等待中... ({elapsed}s/120s)")
                        time.sleep(2)
                        continue
                    for otp_code in candidate_codes:
                        tried_codes.add(otp_code)
                        self._print(f"[OAuth] (IMAP) 尝试 OTP: {otp_code}")
                        try:
                            resp_otp = self._oauth_request_with_backoff(
                                "post",
                                f"{OAUTH_ISSUER}/api/accounts/email-otp/validate",
                                step_label="POST /api/accounts/email-otp/validate",
                                json={"code": otp_code},
                                headers=headers_otp,
                                timeout=30,
                                allow_redirects=False,
                                impersonate=self.impersonate,
                            )
                        except Exception as e:
                            self._print(f"[OAuth] email-otp/validate 异常: {e}")
                            continue
                        self._print(f"[OAuth] /email-otp/validate -> {resp_otp.status_code}")
                        if resp_otp.status_code != 200:
                            self._print(f"[OAuth] OTP 无效，继续尝试下一条: {resp_otp.text[:160]}")
                            continue
                        try:
                            otp_data = resp_otp.json()
                        except Exception:
                            self._print("[OAuth] email-otp/validate 响应解析失败")
                            continue
                        continue_url = otp_data.get("continue_url", "") or continue_url
                        page_type = (otp_data.get("page") or {}).get("type", "") or page_type
                        self._print(f"[OAuth] OTP 验证通过 page={page_type or '-'} next={(continue_url or '-')[:140]}")
                        otp_success = True
                        break
                    if not otp_success:
                        time.sleep(2)
                if not otp_success:
                    self._print(f"[OAuth] (IMAP) OTP 验证失败, 已尝试 {len(tried_codes)} 个")
                    return None

            # 指定邮箱模式：直接询问用户手动输入
            elif not mail_token:
                while time.time() < otp_deadline and not otp_success:
                    manual_code = self._prompt_otp_manually(timeout=int(max(0, otp_deadline - time.time())))
                    if not manual_code:
                        self._print("[OAuth] 用户未提供 OTP，放弃")
                        return None
                    if manual_code in tried_codes:
                        self._print("[OAuth] 该 OTP 已尝试过，请重新输入")
                        continue
                    tried_codes.add(manual_code)
                    try:
                        resp_otp = self._oauth_request_with_backoff(
                            "post",
                            f"{OAUTH_ISSUER}/api/accounts/email-otp/validate",
                            step_label="POST /api/accounts/email-otp/validate",
                            json={"code": manual_code},
                            headers=headers_otp,
                            timeout=30,
                            allow_redirects=False,
                            impersonate=self.impersonate,
                        )
                    except Exception as e:
                        self._print(f"[OAuth] email-otp/validate 异常: {e}")
                        continue
                    self._print(f"[OAuth] /email-otp/validate -> {resp_otp.status_code}")
                    if resp_otp.status_code != 200:
                        self._print(f"[OAuth] OTP 无效: {resp_otp.text[:160]}")
                        continue
                    try:
                        otp_data = resp_otp.json()
                    except Exception:
                        self._print("[OAuth] email-otp/validate 响应解析失败")
                        continue
                    continue_url = otp_data.get("continue_url", "") or continue_url
                    page_type = (otp_data.get("page") or {}).get("type", "") or page_type
                    self._print(f"[OAuth] OTP 验证通过 page={page_type or '-'} next={(continue_url or '-')[:140]}")
                    otp_success = True

                if not otp_success:
                    self._print("[OAuth] 手动 OTP 验证失败")
                    return None

            while not otp_success and mail_token and time.time() < otp_deadline:
                messages = self._fetch_emails_duckmail(mail_token) or []
                candidate_codes = []

                for msg in messages[:12]:
                    msg_id = msg.get("id") or msg.get("@id")
                    if not msg_id:
                        continue
                    detail = self._fetch_email_detail_duckmail(mail_token, msg_id)
                    if not detail:
                        continue
                    content = detail.get("text") or detail.get("html") or ""
                    code = self._extract_verification_code(content)
                    if code and code not in tried_codes:
                        candidate_codes.append(code)

                if not candidate_codes:
                    elapsed = int(120 - max(0, otp_deadline - time.time()))
                    self._print(f"[OAuth] OTP 等待中... ({elapsed}s/120s)")
                    time.sleep(2)
                    continue

                for otp_code in candidate_codes:
                    tried_codes.add(otp_code)
                    self._print(f"[OAuth] 尝试 OTP: {otp_code}")
                    try:
                        resp_otp = self._oauth_request_with_backoff(
                            "post",
                            f"{OAUTH_ISSUER}/api/accounts/email-otp/validate",
                            step_label="POST /api/accounts/email-otp/validate",
                            json={"code": otp_code},
                            headers=headers_otp,
                            timeout=30,
                            allow_redirects=False,
                            impersonate=self.impersonate,
                        )
                    except Exception as e:
                        self._print(f"[OAuth] email-otp/validate 异常: {e}")
                        continue

                    self._print(f"[OAuth] /email-otp/validate -> {resp_otp.status_code}")
                    if resp_otp.status_code != 200:
                        self._print(f"[OAuth] OTP 无效，继续尝试下一条: {resp_otp.text[:160]}")
                        continue

                    try:
                        otp_data = resp_otp.json()
                    except Exception:
                        self._print("[OAuth] email-otp/validate 响应解析失败")
                        continue

                    continue_url = otp_data.get("continue_url", "") or continue_url
                    page_type = (otp_data.get("page") or {}).get("type", "") or page_type
                    self._print(f"[OAuth] OTP 验证通过 page={page_type or '-'} next={(continue_url or '-')[:140]}")
                    otp_success = True
                    break

                if not otp_success:
                    time.sleep(2)

            if not otp_success:
                self._print(f"[OAuth] OAuth 阶段 OTP 验证失败，已尝试 {len(tried_codes)} 个验证码")
                return None

        # 邮箱 OTP 之后, OpenAI 可能再要求手机号验证 (page=add_phone)
        need_add_phone = (
            page_type == "add_phone"
            or "add-phone" in (continue_url or "")
            or "add_phone" in (continue_url or "")
            or "phone-number" in (continue_url or "")
        )
        if need_add_phone:
            self._print(f"[OAuth] 4.5/7 检测到 add_phone (page={page_type}, "
                        f"next={(continue_url or '')[:140]})")
            phone_referer = continue_url
            if phone_referer and phone_referer.startswith("/"):
                phone_referer = f"{OAUTH_ISSUER}{phone_referer}"
            if not phone_referer:
                phone_referer = f"{OAUTH_ISSUER}/add-phone-number"
            new_continue, new_page = self._handle_add_phone(phone_referer)
            if not new_continue and not new_page:
                self._print("[OAuth] add_phone 失败")
                return None
            continue_url = new_continue or continue_url
            page_type = new_page or page_type
            self._print(f"[OAuth] add_phone 完成 page={page_type} "
                        f"next={(continue_url or '-')[:140]}")

        code = None
        consent_url = continue_url
        if consent_url and consent_url.startswith("/"):
            consent_url = f"{OAUTH_ISSUER}{consent_url}"

        if not consent_url and "consent" in page_type:
            consent_url = f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent"

        if consent_url:
            code = _extract_code_from_url(consent_url)

        if not code and consent_url:
            self._print("[OAuth] 5/7 跟随 continue_url 提取 code")
            code, _ = self._oauth_follow_for_code(consent_url, referer=f"{OAUTH_ISSUER}/log-in/password")

        consent_hint = (
            ("consent" in (consent_url or ""))
            or ("sign-in-with-chatgpt" in (consent_url or ""))
            or ("workspace" in (consent_url or ""))
            or ("organization" in (consent_url or ""))
            or ("consent" in page_type)
            or ("organization" in page_type)
        )

        if not code and consent_hint:
            if not consent_url:
                consent_url = f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent"
            self._print("[OAuth] 6/7 执行 workspace/org 选择")
            code = self._oauth_submit_workspace_and_org(consent_url)

        if not code:
            fallback_consent = f"{OAUTH_ISSUER}/sign-in-with-chatgpt/codex/consent"
            self._print("[OAuth] 6/7 回退 consent 路径重试")
            code = self._oauth_submit_workspace_and_org(fallback_consent)
            if not code:
                code, _ = self._oauth_follow_for_code(fallback_consent, referer=f"{OAUTH_ISSUER}/log-in/password")

        if not code:
            self._print("[OAuth] 未获取到 authorization code")
            return None

        self._print("[OAuth] 7/7 POST /oauth/token")
        token_resp = self._oauth_request_with_backoff(
            "post",
            f"{OAUTH_ISSUER}/oauth/token",
            step_label="POST /oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.ua},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "client_id": OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            timeout=60,
            impersonate=self.impersonate,
        )
        self._print(f"[OAuth] /oauth/token -> {token_resp.status_code}")

        if token_resp.status_code != 200:
            self._print(f"[OAuth] token 交换失败: {token_resp.status_code} {token_resp.text[:200]}")
            return None

        try:
            data = token_resp.json()
        except Exception:
            self._print("[OAuth] token 响应解析失败")
            return None

        if not data.get("access_token"):
            self._print("[OAuth] token 响应缺少 access_token")
            return None

        self._print("[OAuth] Codex Token 获取成功")
        return data


# ==================== 并发批量注册 ====================

def _register_one(idx, total, proxy, output_file, custom_email=None,
                  mail_provider="duckmail", *, browser_proxy=None):
    """单个注册任务 (在线程中运行)
    mail_provider:
      - "duckmail"        : DuckMail 临时邮箱 (默认)
      - "source:<key>"    : 使用 email_sources 中定义的注册来源
      - "custom"          : custom_email 指定邮箱 + 手动 OTP
    proxy 是 API (curl_cffi/urllib) 走的代理；browser_proxy 是浏览器走的代理
    (默认与 proxy 一致；landbridge 启用时由调用方覆盖以让浏览器走原始上游)。
    """
    reg = None
    qq_pool = None
    qq_addr = None
    try:
        reg = ChatGPTRegister(proxy=proxy, tag=f"{idx}", browser_proxy=browser_proxy)
        mail_provider = _normalize_mail_provider(mail_provider)
        source = _get_email_source_for_provider(mail_provider)

        if mail_provider == "custom" or custom_email:
            email = custom_email
            email_pwd = "(user-provided)"
            mail_token = None
            reg._print(f"[Custom] 使用指定邮箱: {email}")
        elif _mail_provider_is_imap(mail_provider):
            profile = _get_receiver_profile_for_source(source)
            qq_pool = _get_mail_pool_for_provider(mail_provider)
            if not profile:
                raise Exception("所选邮箱来源缺少有效 receiver 配置")
            if not qq_pool:
                raise Exception(
                    "IMAP 收件池未初始化, 检查 config.json 中 "
                    "imap_profiles / email_sources 配置"
                )
            if source["type"] == "forward_domain":
                qq_addr = qq_pool.acquire_email(domain=source["domain"])
                email = qq_addr
                email_pwd = "(forward-domain→IMAP)"
                reg._print(
                    f"[MailSource] 域名转发: {email} -> {profile['user']} | source={source['name']}"
                )
            elif source["type"] == "addy":
                qq_addr = qq_pool.acquire_email(domain=source["domain"])
                email = qq_addr
                email_pwd = "(addy.io alias→IMAP)"
                reg._print(
                    f"[MailSource] addy.io 别名: {email} -> {profile['user']} | source={source['name']}"
                )
            else:
                base_address = source["address"] or profile["user"]
                if _email_source_uses_suffix_alias(source):
                    qq_addr = qq_pool.acquire_email(base_address=base_address)
                    email = qq_addr
                    email_pwd = "(imap-mailbox-alias)"
                    reg._print(
                        f"[MailSource] IMAP 子邮箱: {email} -> {profile['user']} | source={source['name']}"
                    )
                else:
                    email = base_address
                    qq_addr = email
                    email_pwd = "(imap-mailbox)"
                    reg._print(
                        f"[MailSource] 使用 IMAP 邮箱: {email} | source={source['name']}"
                    )
            mail_token = None
            reg.qq_pool = qq_pool
            reg.qq_pool_email = email
            reg.qq_pool_since = None
        else:
            reg._print("[DuckMail] 创建临时邮箱...")
            email, email_pwd, mail_token = reg.create_temp_email()
        tag = email.split("@")[0]
        reg.tag = tag  # 更新 tag

        chatgpt_password = _generate_password()
        name = _random_name()
        birthdate = _random_birthdate()
        reg.email = email
        _worker_log(
            "\n".join([
                f"{'='*60}",
                f"  [{idx}/{total}] 注册: {email}",
                f"  ChatGPT密码: {chatgpt_password}",
                f"  邮箱密码: {email_pwd}",
                f"  姓名: {name} | 生日: {birthdate}",
                f"{'='*60}",
            ]),
            account=_mask_email(email),
            step="register",
        )

        # 2. 执行注册流程
        reg.run_register(email, chatgpt_password, name, birthdate, mail_token)

        # 3. OAuth（可选）
        oauth_ok = True
        if ENABLE_OAUTH:
            reg._print("[OAuth] 开始获取 Codex Token...")
            try:
                tokens = reg.perform_codex_oauth_login_http(email, chatgpt_password, mail_token=mail_token)
            except Exception as e:
                oauth_ok = False
                _finalize_pending_oauth_failure(
                    reg,
                    email,
                    chatgpt_password,
                    email_pwd,
                    mail_provider,
                    f"OAuth 异常: {e}",
                    cause=e,
                )
            else:
                oauth_ok = bool(tokens and tokens.get("access_token"))
                if oauth_ok:
                    _save_codex_tokens(email, tokens)
                    reg._print("[OAuth] Token 已保存")
                else:
                    _finalize_pending_oauth_failure(
                        reg,
                        email,
                        chatgpt_password,
                        email_pwd,
                        mail_provider,
                        "OAuth 获取失败",
                    )

        # 4. 线程安全写入结果
        with _file_lock:
            with open(output_file, "a", encoding="utf-8") as out:
                out.write(f"{email}----{chatgpt_password}----{email_pwd}----oauth={'ok' if oauth_ok else 'fail'}\n")

        _worker_log(f"[OK] [{tag}] {email} 注册成功!", level="success",
                    account=_mask_email(email), step="done")
        return True, email, None

    except Exception as e:
        error_msg = str(e)
        _worker_log(f"[FAIL] [{idx}] 注册失败: {error_msg}", level="error", step="failed")
        _monitor_emit("system", traceback.format_exc(), level="error")
        # 失败也释放, 避免内存堆积
        if qq_pool and qq_addr:
            try:
                qq_pool.release(qq_addr)
            except Exception:
                pass
        return False, None, error_msg
    finally:
        # 成功时也清掉收件桶里的旧件
        if qq_pool and qq_addr:
            try:
                qq_pool.release(qq_addr)
            except Exception:
                pass


def _read_pending_oauth(input_file: str):
    """读 pending_oauth.txt, 返回 list[(email, password, email_pwd, mail_provider, raw_line)]
    跳过空行/注释/格式不全的行。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = input_file if os.path.isabs(input_file) else os.path.join(base_dir, input_file)
    if not os.path.exists(path):
        return [], path
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split("----")
            if len(parts) < 2:
                continue
            email = parts[0].strip()
            password = parts[1].strip()
            email_pwd = parts[2].strip() if len(parts) > 2 else ""
            mp = parts[3].strip() if len(parts) > 3 else ""
            if not email or not password:
                continue
            items.append((email, password, email_pwd, mp, raw))
    return items, path


def _rewrite_pending_oauth(path: str, remaining_raw_lines):
    """把仍未成功的 raw_line 写回 pending_oauth.txt (覆盖)。"""
    with _file_lock:
        with open(path, "w", encoding="utf-8") as f:
            for line in remaining_raw_lines:
                f.write(line + "\n")


def _retry_oauth_one(idx, total, email, password, email_pwd, mail_provider, proxy, qq_pool,
                     *, browser_proxy=None):
    """对单个账号只跑 OAuth 流程, 返回 (ok, email, error)。
    proxy 是 API 走的代理；browser_proxy 是浏览器走的代理 (默认与 proxy 一致)。"""
    reg = None
    qq_addr = None
    try:
        reg = ChatGPTRegister(proxy=proxy, tag=email.split("@")[0], browser_proxy=browser_proxy)
        mail_provider = _normalize_mail_provider(mail_provider)

        if _mail_provider_is_imap(mail_provider) and qq_pool:
            reg.qq_pool = qq_pool
            reg.qq_pool_email = email
            reg.qq_pool_since = None
            qq_addr = email
            reg._print(f"[Retry] 已注入 IMAP 收件源: {email}")

        _worker_log(
            "\n".join([
                f"{'='*60}",
                f"  [{idx}/{total}] Retry OAuth: {email}",
                f"  password: {password[:4]}**** | mail_provider={mail_provider or '?'}",
                f"{'='*60}",
            ]),
            account=_mask_email(email),
            step="retry_oauth",
        )

        reg._print("[Retry] 开始执行 Codex OAuth (跳过注册阶段)")
        tokens = reg.perform_codex_oauth_login_http(email, password, mail_token=None)
        ok = bool(tokens and tokens.get("access_token"))
        if not ok:
            reg._print("[Retry] OAuth 仍然失败")
            return False, email, "oauth_failed"

        _save_codex_tokens(email, tokens)
        reg._print("[Retry] Token 已保存")
        _worker_log(f"[OK] [{email}] OAuth 补救成功!", level="success",
                    account=_mask_email(email), step="done")
        return True, email, None

    except OAuthPendingRequired as e:
        msg = str(e)
        reg._print(f"[Retry] {msg}，保留在 pending_oauth.txt")
        _worker_log(
            f"[Retry] [{email}] {msg}，已保留 pending",
            level="warn",
            account=_mask_email(email),
            step="retry_oauth",
        )
        return False, email, "pending_oauth"
    except Exception as e:
        msg = str(e)
        _worker_log(f"[FAIL] [{email}] retry 异常: {msg}", level="error",
                    account=_mask_email(email), step="failed")
        _monitor_emit("system", traceback.format_exc(), level="error")
        return False, email, msg
    finally:
        if qq_pool and qq_addr:
            try:
                qq_pool.release(qq_addr)
            except Exception:
                pass


def retry_oauth_only(input_file: str = PENDING_OAUTH_FILE,
                     output_file: str = "registered_accounts.txt",
                     proxy=None,
                     max_workers: int = RETRY_OAUTH_DEFAULT_WORKERS,
                     mail_provider_override: str = "",
                     *,
                     with_monitor_runtime: bool = True):
    """补救入口: 读 pending_oauth.txt 中的 email+password, 只跑 OAuth + 存 token。
    成功的从 pending 文件移除并追加到 output_file (oauth=ok)。
    mail_provider_override: 强制覆盖每行的 mail_provider (例如统一用 domain_catchall)。
    """
    items, path = _read_pending_oauth(input_file)
    if not items:
        _console_log(f"[Retry] {path} 无可补救账号", level="warn")
        return

    actual_workers = max(1, min(max_workers, len(items)))
    if with_monitor_runtime and monitor is not None:
        _reset_run_metrics()
        _intake_paused_event.clear()
        _shutdown_event.clear()

        def _run_callable():
            return retry_oauth_only(
                input_file=input_file,
                output_file=output_file,
                proxy=proxy,
                max_workers=max_workers,
                mail_provider_override=mail_provider_override,
                with_monitor_runtime=False,
            )

        result = monitor.run_with_monitor(
            _run_callable,
            tui_enabled=_is_tui_enabled(),
            max_workers=actual_workers,
            summary_getter=_summary_snapshot,
            inflight_getter=_inflight_workers_snapshot,
            intake_paused=_intake_paused_event,
            shutdown_event=_shutdown_event,
        )
        summary = _summary_snapshot()
        _console_log(
            f"retry summary: success={summary['success']} fail={summary['fail']}"
            f" dropped={monitor.stats().get('dropped_events', 0)}",
            level="success" if summary["success"] else "warn",
        )
        return result

    _system_log(f"\n{'#'*60}")
    _system_log("  OAuth 补救模式")
    _system_log(f"  输入: {path} ({len(items)} 个账号)")
    _system_log(f"  并发: {actual_workers}")
    override_provider = _normalize_mail_provider(mail_provider_override) if mail_provider_override else ""
    if override_provider:
        _system_log(f"  强制 mail_provider: {override_provider}")
    _system_log(f"{'#'*60}\n")

    needed_imap_providers = set()
    if _mail_provider_is_imap(override_provider):
        needed_imap_providers.add(override_provider)
    for _, _, _, mp, _ in items:
        effective_mp = override_provider or _normalize_mail_provider(mp)
        if _mail_provider_is_imap(effective_mp):
            needed_imap_providers.add(effective_mp)

    qq_pools = {}
    for provider in sorted(needed_imap_providers):
        source = _get_email_source_for_provider(provider)
        profile = _get_receiver_profile_for_source(source)
        if not profile:
            _system_log(
                "\n".join([
                    f"[Retry] ⚠️ 找不到 IMAP 配置: {provider}",
                    "        将退化为手动 OTP 模式 (出现 OTP 时终端会 prompt)",
                ]),
                level="warn",
            )
            continue
        qq_pool = _get_mail_pool_for_provider(provider)
        if qq_pool:
            qq_pools[provider] = qq_pool
            _system_log(
                f"[Retry] IMAP 池已就绪: {source['name']} ({profile['user']})",
                level="success",
            )
        else:
            _system_log(f"[Retry] ⚠️ IMAP 邮箱池初始化失败: {source['name']}", level="error")

    success_emails = set()
    fail_emails = set()
    worker_slots: queue.Queue[str] = queue.Queue()
    worker_ids = [f"W{i + 1}" for i in range(actual_workers)]
    for wid in worker_ids:
        worker_slots.put(wid)

    worker_proxy_map = {}
    if _landbridge.is_enabled():
        try:
            _landbridge.start_for_workers(worker_ids)
            worker_proxy_map = _landbridge.assign_worker_landings(worker_ids)
            _system_log(
                f"  landbridge: 已启用, landings={_landbridge.landing_ids()}, "
                f"分配={worker_proxy_map}"
            )
        except Exception as e:
            _system_log(f"  landbridge: 启动失败, 回退到原 proxy: {e}", level="error")
            worker_proxy_map = {}

    def _job(args):
        i, (email, password, email_pwd, mp, raw) = args
        effective_mp = override_provider or _normalize_mail_provider(mp)
        qq_pool = qq_pools.get(effective_mp)
        worker_id = worker_slots.get()
        _mark_worker_active(worker_id)
        if monitor is not None:
            monitor.set_current_worker(worker_id)
        try:
            api_proxy = worker_proxy_map.get(worker_id, proxy)
            ok, em, err = _retry_oauth_one(
                i + 1, len(items), email, password, email_pwd, effective_mp, api_proxy, qq_pool,
                browser_proxy=proxy,
            )
            return ok, em, raw
        finally:
            if monitor is not None:
                monitor.clear_current_worker()
            _mark_worker_idle(worker_id)
            worker_slots.put(worker_id)

    if actual_workers == 1:
        results = [_job(x) for x in enumerate(items)]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=actual_workers) as pool:
            futures = [pool.submit(_job, (i, it)) for i, it in enumerate(items)]
            for fut in as_completed(futures):
                results.append(fut.result())

    # 收集结果
    for ok, em, raw in results:
        if ok:
            success_emails.add(raw)
            _bump_metric("success")
        else:
            fail_emails.add(raw)
            _bump_metric("fail")
        _bump_metric("done")

    # 把成功的从 pending 移除 (按原 raw 行匹配)
    remaining = [raw for (_, _, _, _, raw) in items if raw not in success_emails]
    _rewrite_pending_oauth(path, remaining)

    # 成功的账号补写到 output_file (oauth=ok)
    if success_emails:
        out_path = output_file if os.path.isabs(output_file) else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), output_file)
        with _file_lock:
            with open(out_path, "a", encoding="utf-8") as out:
                for email, password, email_pwd, _, raw in items:
                    if raw in success_emails:
                        out.write(f"{email}----{password}----{email_pwd}----oauth=ok (retry)\n")

    _system_log(f"\n{'#'*60}")
    _system_log("  OAuth 补救完成", level="success" if success_emails else "warn")
    _system_log(
        f"  成功: {len(success_emails)} | 失败: {len(fail_emails)}",
        level="success" if success_emails else "warn",
    )
    _system_log(
        f"  pending_oauth.txt 剩余: {len(remaining)}",
        level="success" if success_emails else "warn",
    )
    _system_log(f"{'#'*60}\n", level="success" if success_emails else "warn")


def run_batch(total_accounts: int = 1, output_file="registered_accounts.txt",
              max_workers=1, proxy=None, custom_email=None,
              mail_provider="duckmail", *, with_monitor_runtime=True):
    """并发批量注册
    mail_provider: "duckmail" | "custom" | "source:<key>"
    """
    mail_provider = _normalize_mail_provider(mail_provider)

    requested_workers = 1 if (custom_email or mail_provider == "custom") else max_workers
    actual_workers = min(requested_workers, total_accounts)
    if with_monitor_runtime and monitor is not None:
        _reset_run_metrics()
        _intake_paused_event.clear()
        _shutdown_event.clear()

        def _run_callable():
            return run_batch(
                total_accounts=total_accounts,
                output_file=output_file,
                max_workers=max_workers,
                proxy=proxy,
                custom_email=custom_email,
                mail_provider=mail_provider,
                with_monitor_runtime=False,
            )

        result = monitor.run_with_monitor(
            _run_callable,
            tui_enabled=_is_tui_enabled(),
            max_workers=max(1, actual_workers),
            pool_getter=_get_phone_pool,
            summary_getter=_summary_snapshot,
            inflight_getter=_inflight_workers_snapshot,
            intake_paused=_intake_paused_event,
            shutdown_event=_shutdown_event,
        )
        summary = _summary_snapshot()
        _console_log(
            "summary:"
            f" success={summary['success']}"
            f" fail={summary['fail']}"
            f" spent=${summary['spent']:.4f}"
            f" dropped={monitor.stats().get('dropped_events', 0)}"
            f" cap_skipped={summary['cap_skipped']}",
            level="success" if summary["success"] else "warn",
        )
        return result

    if custom_email or mail_provider == "custom":
        mail_provider = "custom"
        if total_accounts != 1 or max_workers != 1:
            _system_log("[Info] 指定邮箱模式：强制 total_accounts=1, max_workers=1")
        total_accounts = 1
        max_workers = 1
    elif _mail_provider_is_imap(mail_provider):
        source = _get_email_source_for_provider(mail_provider)
        profile = _get_receiver_profile_for_source(source)
        if not source or not profile:
            _system_log("❌ 错误: 所选邮箱来源不存在或未配置完整", level="error")
            return
        pool = _get_mail_pool_for_provider(mail_provider)
        if not pool:
            _system_log(f"❌ 错误: IMAP 邮箱池初始化失败: {source['name']}", level="error")
            return
        if _email_source_requires_single_address(source) and (total_accounts != 1 or max_workers != 1):
            _system_log("[Info] IMAP 单邮箱模式：强制 total_accounts=1, max_workers=1")
            total_accounts = 1
            max_workers = 1
        _system_log(f"[MailSource] 池已就绪: {_describe_email_source(source)}")
    elif not DUCKMAIL_BEARER:
        _system_log("❌ 错误: 未设置 DUCKMAIL_BEARER 环境变量", level="error")
        _system_log("   请设置: export DUCKMAIL_BEARER='your_api_key_here'", level="error")
        _system_log("   或: set DUCKMAIL_BEARER=your_api_key_here (Windows)", level="error")
        return

    actual_workers = min(max_workers, total_accounts)
    if SENTINEL_INPROCESS:
        sentinel_thread_overridden = SENTINEL_SOLVER_THREAD > 0
        sentinel_thread = SENTINEL_SOLVER_THREAD if sentinel_thread_overridden else (actual_workers + 1)
        _configure_inprocess_sentinel_runtime(thread=sentinel_thread, default_proxy=proxy)

    _system_log(f"\n{'#'*60}")
    if mail_provider == "custom":
        mode_label = f"指定邮箱: {custom_email}"
    elif _mail_provider_is_imap(mail_provider):
        source = _get_email_source_for_provider(mail_provider)
        mode_label = _describe_email_source(source)
    else:
        mode_label = "DuckMail 临时邮箱"
    _system_log(f"  ChatGPT 批量自动注册 ({mode_label})")
    _system_log(f"  注册数量: {total_accounts} | 并发数: {actual_workers}")
    if SENTINEL_INPROCESS:
        sentinel_note = "配置覆盖" if sentinel_thread_overridden else f"注册并发{actual_workers} + 1"
        _system_log(f"  Sentinel池: {sentinel_thread} ({sentinel_note})")
    if mail_provider == "duckmail":
        _system_log(f"  DuckMail: {DUCKMAIL_API_BASE}")
    _system_log(f"  OAuth: {'开启' if ENABLE_OAUTH else '关闭'} | required: {'是' if OAUTH_REQUIRED else '否'}")
    if ENABLE_OAUTH:
        _system_log(f"  OAuth Issuer: {OAUTH_ISSUER}")
        _system_log(f"  OAuth Client: {OAUTH_CLIENT_ID}")
        _system_log(f"  OAuth add_phone SMS: {'开启' if OAUTH_ADD_PHONE_SMS else '关闭'}")
        _system_log(f"  Token输出: {TOKEN_JSON_DIR}/, {AK_FILE}, {RK_FILE}")
    if OAUTH_ADD_PHONE_SMS:
        _system_log(
            f"  PhonePool: max_workers={actual_workers} max_active={PHONE_MAX_ACTIVE or actual_workers} "
            f"max_reuse={PHONE_MAX_REUSE} acquire_timeout={PHONE_ACQUIRE_TIMEOUT}"
        )
    _system_log(f"  输出文件: {output_file}")
    _system_log(f"{'#'*60}\n")

    success_count = 0
    fail_count = 0
    start_time = time.time()
    worker_slots: queue.Queue[str] = queue.Queue()
    worker_ids = [f"W{i + 1}" for i in range(actual_workers)]
    for wid in worker_ids:
        worker_slots.put(wid)

    # landbridge: 按当前 worker 数生成 N 个 Landing, 每个 worker 一个 (username 独立)
    worker_proxy_map = {}
    if _landbridge.is_enabled():
        try:
            _landbridge.start_for_workers(worker_ids)
            worker_proxy_map = _landbridge.assign_worker_landings(worker_ids)
            _system_log(
                f"  landbridge: 已启用, landings={_landbridge.landing_ids()}, "
                f"分配={worker_proxy_map}"
            )
        except Exception as e:
            _system_log(f"  landbridge: 启动失败, 回退到原 proxy: {e}", level="error")
            worker_proxy_map = {}

    def _worker_job(account_idx):
        worker_id = worker_slots.get()
        _mark_worker_active(worker_id)
        if monitor is not None:
            monitor.set_current_worker(worker_id)
        try:
            api_proxy = worker_proxy_map.get(worker_id, proxy)
            return _register_one(
                account_idx, total_accounts, api_proxy, output_file,
                custom_email, mail_provider,
                browser_proxy=proxy,
            )
        finally:
            if monitor is not None:
                monitor.clear_current_worker()
            _mark_worker_idle(worker_id)
            worker_slots.put(worker_id)

    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {}
        for idx in range(1, total_accounts + 1):
            while _intake_paused_event.is_set() and not _shutdown_event.is_set():
                time.sleep(0.1)
            if _shutdown_event.is_set():
                break
            future = executor.submit(_worker_job, idx)
            futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            try:
                ok, email, err = future.result()
                if ok:
                    success_count += 1
                    _bump_metric("success")
                else:
                    fail_count += 1
                    _bump_metric("fail")
                    _system_log(f"  [账号 {idx}] 失败: {err}", level="error")
                _bump_metric("done")
            except Exception as e:
                fail_count += 1
                _bump_metric("done")
                _bump_metric("fail")
                _system_log(f"[FAIL] 账号 {idx} 线程异常: {e}", level="error")

    elapsed = time.time() - start_time
    avg = elapsed / total_accounts if total_accounts else 0
    pool = _get_phone_pool()
    if pool is not None and hasattr(pool, "stats"):
        with _run_metrics_lock:
            _run_metrics["spent"] = float(pool.stats().get("spent", 0.0))
    _system_log(f"\n{'#'*60}")
    _system_log(f"  注册完成! 耗时 {elapsed:.1f} 秒")
    _system_log(f"  总数: {total_accounts} | 成功: {success_count} | 失败: {fail_count}")
    _system_log(f"  平均速度: {avg:.1f} 秒/个")
    if success_count > 0:
        _system_log(f"  结果文件: {output_file}")
    _system_log(f"{'#'*60}")


def _positive_int_arg(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {raw_value!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return value


def _build_runtime_parent_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--proxy", help="代理地址，例如 http://127.0.0.1:7890")
    parser.add_argument(
        "-l",
        "--log-level",
        dest="log_level",
        metavar="LEVEL",
        help="日志级别: debug / info / success / warn / error",
    )
    parser.add_argument(
        "-d",
        "--debug",
        dest="log_level",
        action="store_const",
        const="debug",
        help="等价于 --log-level debug",
    )
    parser.add_argument(
        "--no-landbridge-prompt",
        dest="no_landbridge_prompt",
        action="store_true",
        help="跳过 landbridge 启用/配置交互, 直接读 config 中已保存的 enabled + template",
    )
    parser.set_defaults(log_level=None, tui_mode=None, no_landbridge_prompt=False)
    return parser


def _add_tui_flags(parser, *, visible: bool):
    tui_help = "启用 TUI（默认关闭）" if visible else argparse.SUPPRESS
    parser.add_argument(
        "-t",
        "--tui",
        dest="tui_mode",
        action="store_const",
        const=True,
        help=tui_help,
    )
    parser.add_argument(
        "-T",
        "--no-tui",
        dest="tui_mode",
        action="store_const",
        const=False,
        help=argparse.SUPPRESS,
    )


def _add_oauth_add_phone_flags(parser):
    parser.add_argument(
        "-s",
        "--oauth-add-phone-sms",
        "--oauth-add-phone-via-sms",
        dest="oauth_add_phone_sms",
        action="store_const",
        const=True,
        help="允许 OAuth add_phone 时通过 SMS 平台自动接码",
    )
    parser.add_argument(
        "-S",
        "--no-oauth-add-phone-sms",
        dest="oauth_add_phone_sms",
        action="store_const",
        const=False,
        help="禁用 OAuth add_phone 自动接码；命中 add_phone 时写入 pending_oauth.txt（默认）",
    )
    parser.set_defaults(oauth_add_phone_sms=None)


def _build_register_arg_parser():
    parser = argparse.ArgumentParser(
        prog="python3 chatgpt_register.py",
        description="默认模式：批量注册 ChatGPT 账号，并按配置执行 Codex OAuth。",
        formatter_class=argparse.RawTextHelpFormatter,
        parents=[_build_runtime_parent_parser()],
    )
    parser.add_argument(
        "-m",
        "--mail-provider",
        help="注册邮箱来源 key；custom 需配合 --email。",
    )
    parser.add_argument(
        "-e",
        "--email",
        help="指定单个邮箱地址；启用后强制单账号、单线程模式。",
    )
    parser.add_argument("-c", "--count", type=_positive_int_arg, help="注册账号数量。")
    parser.add_argument("-w", "--workers", type=_positive_int_arg, help="并发数。")
    _add_tui_flags(parser, visible=True)
    _add_oauth_add_phone_flags(parser)
    parser.epilog = (
        "示例:\n"
        "  python3 chatgpt_register.py --count 5 --workers 2\n"
        "  python3 chatgpt_register.py --mail-provider custom --email you@example.com\n"
        "  python3 chatgpt_register.py --oauth-add-phone-sms --proxy http://127.0.0.1:7890"
    )
    return parser


def _build_retry_oauth_arg_parser():
    parser = argparse.ArgumentParser(
        prog="python3 chatgpt_register.py",
        description="补救模式：只对 pending_oauth.txt 中账号重试 OAuth。",
        formatter_class=argparse.RawTextHelpFormatter,
        parents=[_build_runtime_parent_parser()],
    )
    parser.add_argument(
        "-r",
        "--retry-oauth",
        nargs="?",
        const=PENDING_OAUTH_FILE,
        metavar="FILE",
        help=f"待补救文件，省略时默认 {PENDING_OAUTH_FILE}",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=_positive_int_arg,
        help=f"OAuth 补救并发数；省略时交互输入或默认 {RETRY_OAUTH_DEFAULT_WORKERS}",
    )
    parser.add_argument(
        "-m",
        "--mail-provider",
        help="覆盖 pending 文件中记录的收件源。",
    )
    _add_tui_flags(parser, visible=True)
    _add_oauth_add_phone_flags(parser)
    parser.epilog = (
        "示例:\n"
        "  python3 chatgpt_register.py --retry-oauth\n"
        "  python3 chatgpt_register.py --retry-oauth pending_oauth.txt --workers 3\n"
        "  python3 chatgpt_register.py --retry-oauth --oauth-add-phone-sms"
    )
    return parser


def _build_auth_from_code_arg_parser():
    parser = argparse.ArgumentParser(
        prog="python3 chatgpt_register.py",
        description="认证文件模式：用 localhost callback URL 或 authorization code 直接换 token。",
        formatter_class=argparse.RawTextHelpFormatter,
        parents=[_build_runtime_parent_parser()],
    )
    parser.add_argument(
        "-a",
        "--auth-from-code",
        nargs="?",
        metavar="CALLBACK_OR_CODE",
        help="localhost 回调地址或 authorization code",
    )
    parser.add_argument("-v", "--code-verifier", help="与该 authorization code 匹配的 PKCE code_verifier")
    parser.add_argument("-e", "--email", help="认证文件中的 email 字段覆盖值")
    parser.add_argument("-n", "--account-name", help="认证文件文件名提示/账号名")
    _add_tui_flags(parser, visible=False)
    parser.add_argument(
        "-u",
        "--upload-cpa",
        dest="upload_cpa",
        action="store_const",
        const=True,
        help="生成认证文件后上传到 CPA",
    )
    parser.add_argument(
        "-U",
        "--no-upload-cpa",
        dest="upload_cpa",
        action="store_const",
        const=False,
        help="不要上传到 CPA",
    )
    parser.add_argument(
        "-k",
        "--write-ak-rk",
        dest="write_token_lines",
        action="store_const",
        const=True,
        help="同步写入 ak.txt / rk.txt",
    )
    parser.add_argument(
        "-K",
        "--no-write-ak-rk",
        dest="write_token_lines",
        action="store_const",
        const=False,
        help="不要写入 ak.txt / rk.txt",
    )
    parser.set_defaults(upload_cpa=None, write_token_lines=None)
    parser.epilog = (
        "示例:\n"
        "  python3 chatgpt_register.py --auth-from-code 'http://localhost:1455/auth/callback?code=ac_xxx' --code-verifier xxx\n"
        "  python3 chatgpt_register.py --auth-from-code ac_xxx --code-verifier xxx --email you@example.com"
    )
    return parser


def _indent_help_block(text: str, prefix: str = "  ") -> str:
    return "\n".join((prefix + line) if line else "" for line in text.rstrip().splitlines())


def _print_root_help():
    text = "\n".join([
        _banner_text(),
        "",
        "总用法:",
        "  python3 chatgpt_register.py [register options]",
        "  python3 chatgpt_register.py --retry-oauth [FILE] [retry options]",
        "  python3 chatgpt_register.py --auth-from-code [CALLBACK_OR_CODE] [auth options]",
        "",
        "说明:",
        "  默认模式是批量注册。",
        "  如果 OAuth 流程命中 add_phone，默认不会调用 SMS 平台，",
        "  会记入 pending_oauth.txt；显式传 --oauth-add-phone-sms 才会自动接码。",
        "",
        "注册模式参数:",
        _indent_help_block(_build_register_arg_parser().format_help()),
        "",
        "Retry OAuth 模式参数:",
        _indent_help_block(_build_retry_oauth_arg_parser().format_help()),
        "",
        "Auth From Code 模式参数:",
        _indent_help_block(_build_auth_from_code_arg_parser().format_help()),
    ])
    with _print_lock:
        print(text)


def _detect_cli_mode(argv):
    has_retry = "--retry-oauth" in argv or "-r" in argv
    has_auth_from_code = "--auth-from-code" in argv or "-a" in argv
    if has_retry and has_auth_from_code:
        raise SystemExit("不能同时使用 --retry-oauth 和 --auth-from-code")
    if has_auth_from_code:
        return "auth_from_code"
    if has_retry:
        return "retry_oauth"
    return "register"


def _parse_cli_args(argv):
    mode = _detect_cli_mode(argv)
    if mode == "register" and any(token in ("-h", "--help") for token in argv):
        _print_root_help()
        raise SystemExit(0)

    if mode == "auth_from_code":
        parser = _build_auth_from_code_arg_parser()
    elif mode == "retry_oauth":
        parser = _build_retry_oauth_arg_parser()
    else:
        parser = _build_register_arg_parser()
    return mode, parser.parse_args(argv)


def _apply_runtime_cli_flags(cli_args):
    global _force_no_tui, _force_tui
    if getattr(cli_args, "log_level", None) is not None:
        _set_log_level(cli_args.log_level)

    tui_mode = getattr(cli_args, "tui_mode", None)
    if tui_mode is False:
        _force_no_tui = True
        os.environ["CHATGPT_REGISTER_NO_TUI"] = "1"
    elif tui_mode is True:
        _force_tui = True
        _force_no_tui = False
        os.environ.pop("CHATGPT_REGISTER_NO_TUI", None)

    _set_oauth_add_phone_sms_enabled(getattr(cli_args, "oauth_add_phone_sms", None))


def _run_auth_from_code_flow(auth_args, proxy=None):
    interactive = _is_interactive()

    raw_input = (auth_args.get("raw_input") or "").strip()
    if not raw_input and interactive:
        raw_input = _prompt_text("请输入 localhost 回调地址或 authorization code").strip()
    code = _extract_code_from_input(raw_input)
    if not code:
        _console_log("[AuthFile] 未解析到 authorization code", level="error")
        return 2

    code_verifier = (auth_args.get("code_verifier") or "").strip()
    if not code_verifier and interactive:
        _console_block([
            "[AuthFile] 当前是 PKCE 流程，仅有 callback URL 还不够。",
            "[AuthFile] 还需要与这次登录匹配的 code_verifier 才能换 token。",
        ], level="warn")
        code_verifier = _prompt_text("请输入 code_verifier").strip()
    if not code_verifier:
        _console_log("[AuthFile] 缺少 code_verifier，无法继续", level="error")
        return 2

    account_name = (auth_args.get("account_name") or "").strip()
    email_override = (auth_args.get("email") or "").strip()
    if interactive and not account_name:
        account_name = _prompt_text("账号名/文件名（留空则默认用 token 里的邮箱）").strip()

    tokens = _exchange_codex_auth_code(code, code_verifier, proxy=proxy)
    if not tokens:
        _console_log("[AuthFile] 根据 code 换 token 失败", level="error")
        return 1

    inferred_email = _infer_email_from_tokens(tokens)
    email = email_override or inferred_email
    if interactive and not email_override:
        prompt = f"认证文件 email 字段（默认 {inferred_email or '从 token 未解析到，请手填'}）"
        email_input = _prompt_text(prompt, inferred_email or "").strip()
        email = email_input or inferred_email
    if not email:
        _console_log("[AuthFile] 无法从 token 解析 email，且未手动提供 --email", level="error")
        return 2

    token_data = _build_codex_token_data(email, tokens)
    if not token_data:
        _console_log("[AuthFile] token 数据不完整，未生成认证文件", level="error")
        return 1

    filename_hint = account_name or email
    _console_block([
        f"[AuthFile] 文件名: {_make_auth_filename(filename_hint, fallback_stem=email)}",
        f"[AuthFile] email: {token_data.get('email')}",
        f"[AuthFile] account_id: {token_data.get('account_id') or '-'}",
        f"[AuthFile] expired: {token_data.get('expired') or '-'}",
        f"[AuthFile] refresh_token: {'有' if token_data.get('refresh_token') else '无'}",
        f"[AuthFile] id_token: {'有' if token_data.get('id_token') else '无'}",
    ])
    if interactive and not _prompt_confirm("确认按以上信息生成认证文件?", default=True):
        _console_log("[AuthFile] 已取消写入", level="warn")
        return 0

    write_token_lines = auth_args.get("write_token_lines")
    if write_token_lines is None:
        if interactive:
            write_token_lines = _prompt_confirm("同步写入 ak.txt / rk.txt ?", default=True)
        else:
            write_token_lines = True

    upload_cpa = auth_args.get("upload_cpa")
    upload_ready = bool(UPLOAD_API_URL and UPLOAD_API_TOKEN)
    if upload_cpa is None:
        if interactive:
            upload_cpa = _prompt_confirm("上传到 CPA ?", default=upload_ready)
        else:
            upload_cpa = upload_ready
    if upload_cpa and not upload_ready:
        _console_log("[AuthFile] upload_api_url 或 upload_api_token 未配置，跳过 CPA 上传", level="warn")
        upload_cpa = False

    token_path = _persist_codex_token_data(
        token_data,
        filename_hint=filename_hint,
        write_token_lines=write_token_lines,
        upload_to_cpa=upload_cpa,
    )
    _console_log(f"[AuthFile] 认证文件已生成: {token_path}", level="success")
    return 0


def main():
    try:
        mode, cli_args = _parse_cli_args(sys.argv[1:])
        _apply_runtime_cli_flags(cli_args)

        # landbridge: 启动向导 (auth_from_code 不走 worker, 跳过)
        if mode != "auth_from_code":
            no_lb_prompt = bool(getattr(cli_args, "no_landbridge_prompt", False)) or not _is_interactive()
            try:
                _landbridge_interactive(no_lb_prompt)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                _console_log(f"[landbridge] 向导异常, 沿用 config 配置: {e}", level="warn")

        if mode == "auth_from_code":
            auth_args = {
                "raw_input": (cli_args.auth_from_code or "").strip(),
                "code_verifier": (cli_args.code_verifier or "").strip(),
                "email": (cli_args.email or "").strip(),
                "account_name": (cli_args.account_name or "").strip(),
                "upload_cpa": cli_args.upload_cpa,
                "write_token_lines": cli_args.write_token_lines,
            }
            proxy = cli_args.proxy if cli_args.proxy is not None else DEFAULT_PROXY
            env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
            if not proxy and env_proxy:
                proxy = env_proxy
            if proxy:
                _console_log(f"[AuthFile] 使用代理: {proxy}")
            else:
                _console_log("[AuthFile] 不使用代理")
            sys.exit(_run_auth_from_code_flow(auth_args, proxy=proxy))

        if mode == "retry_oauth":
            input_file = cli_args.retry_oauth or PENDING_OAUTH_FILE
            max_workers = cli_args.workers or RETRY_OAUTH_DEFAULT_WORKERS
            mp_override = (cli_args.mail_provider or "").strip()
            workers_explicit = cli_args.workers is not None
            interactive = _is_interactive()
            if interactive and cli_args.oauth_add_phone_sms is None:
                _set_oauth_add_phone_sms_enabled(
                    _prompt_oauth_add_phone_sms(OAUTH_ADD_PHONE_SMS, label="Retry OAuth")
                )
            if interactive and not workers_explicit:
                max_workers = _prompt_positive_int("OAuth 补救并发数", RETRY_OAUTH_DEFAULT_WORKERS)
            proxy = cli_args.proxy if cli_args.proxy is not None else DEFAULT_PROXY
            if not proxy:
                proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                    or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or None
            if proxy:
                _console_log(f"[Retry] 使用代理: {proxy}")
            else:
                _console_log("[Retry] 不使用代理")
            retry_oauth_only(
                input_file=input_file,
                output_file=DEFAULT_OUTPUT_FILE,
                proxy=proxy,
                max_workers=max_workers,
                mail_provider_override=mp_override,
            )
            return

        main_args = {
            "mail_provider": cli_args.mail_provider,
            "email": cli_args.email,
            "proxy": cli_args.proxy,
            "count": cli_args.count,
            "workers": cli_args.workers,
        }

        _print_banner()

        interactive = _is_interactive()
        if interactive and questionary is None:
            _console_log(
                f"[Warn] questionary 不可用，当前回退到基础 input 交互: {_QUESTIONARY_IMPORT_ERROR}",
                level="warn",
            )

        skip_solver_check = _as_bool(os.environ.get("SKIP_SOLVER_CHECK"))
        if not skip_solver_check:
            ok, info = _check_sentinel_solver_health()
            if ok:
                mode = "in-process" if SENTINEL_INPROCESS else (SENTINEL_SOLVER_URL or "http")
                _console_log(f"[Sentinel] solver 健康检查通过: {mode} {info}")
            else:
                _console_log(f"[Sentinel] ⚠️ solver 不可用: {info}", level="error")
                if SENTINEL_INPROCESS:
                    _console_log("           当前已配置为同进程模式，请检查 patchright/quart 依赖与浏览器可执行文件", level="warn")
                else:
                    _console_log("           请先启动: python3 sentinel_solver.py --thread 2", level="warn")
                _console_log("           或设置 SKIP_SOLVER_CHECK=1 强制跳过", level="warn")
                sys.exit(2)

        # 邮箱来源选择
        custom_email = (main_args["email"] or "").strip() or None
        mail_provider = _normalize_mail_provider((main_args["mail_provider"] or "").strip() or "duckmail")
        if custom_email:
            mail_provider = "custom"
        if not custom_email and mail_provider == "custom":
            if not interactive:
                _console_log("[Error] --mail-provider custom 需要同时提供 --email", level="error")
                sys.exit(2)
            while True:
                custom_email = _prompt_text("请输入邮箱地址").strip()
                if "@" in custom_email and "." in custom_email.split("@")[-1]:
                    break
                _console_log("  邮箱格式无效，请重新输入", level="warn")
        if not custom_email and main_args["mail_provider"] is None and interactive:
            choices = [
                {"title": "DuckMail 临时邮箱", "value": "duckmail"},
                {"title": "指定单个邮箱（OTP 手动输入）", "value": "custom"},
            ]
            for source in EMAIL_SOURCES:
                choices.append({
                    "title": f"{source['name']} | {_describe_email_source(source)}",
                    "value": _source_provider_key(source["key"]),
                })
            default_choice = choices[0]["value"] if choices else "duckmail"
            choice = _prompt_select("选择注册邮箱来源", choices, default=default_choice)
            if choice == "custom":
                mail_provider = "custom"
                while True:
                    custom_email = _prompt_text("请输入邮箱地址").strip()
                    if "@" in custom_email and "." in custom_email.split("@")[-1]:
                        break
                    _console_log("  邮箱格式无效，请重新输入", level="warn")
            elif choice:
                mail_provider = _normalize_mail_provider(choice)
            if custom_email:
                _console_log(f"[Info] 将使用指定邮箱: {custom_email}（OTP 需手动输入，仅注册 1 个账号）")
            elif _mail_provider_is_imap(mail_provider):
                source = _get_email_source_for_provider(mail_provider)
                if source:
                    _console_log(f"[Info] 将使用: {source['name']} | {_describe_email_source(source)}")
        elif custom_email:
            _console_log(f"[Info] 将使用指定邮箱: {custom_email}（OTP 需手动输入，仅注册 1 个账号）")
        elif _mail_provider_is_imap(mail_provider):
            source = _get_email_source_for_provider(mail_provider)
            profile = _get_receiver_profile_for_source(source)
            if not source or not profile:
                _console_log("[Error] 所选邮箱来源不存在或未配置完整", level="error")
                sys.exit(2)
            _console_log(f"[Info] 将使用: {source['name']} | {_describe_email_source(source)}")

        # 检查 DuckMail 配置（仅在 DuckMail 模式下需要）
        if mail_provider == "duckmail" and not DUCKMAIL_BEARER:
            _console_block([
                "\n⚠️  警告: 未设置 DUCKMAIL_BEARER",
                "   请编辑 config.json 设置 duckmail_bearer，或设置环境变量:",
                "   Windows: set DUCKMAIL_BEARER=your_api_key_here",
                "   Linux/Mac: export DUCKMAIL_BEARER='your_api_key_here'",
            ], level="warn")
            if interactive:
                _prompt_text("按 Enter 继续尝试运行", "")
            else:
                _console_log("   当前为非交互环境，继续按默认配置执行。", level="warn")

        # 交互式代理配置
        proxy = main_args["proxy"] if main_args["proxy"] is not None else DEFAULT_PROXY
        env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                 or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
        if main_args["proxy"] is None and interactive:
            if proxy:
                _console_log(f"[Info] 检测到默认代理: {proxy}")
                if not _prompt_confirm("使用此代理?", default=True):
                    proxy = _prompt_text("输入代理地址 (留空=不使用代理)").strip() or None
            elif env_proxy:
                _console_log(f"[Info] 检测到环境变量代理: {env_proxy}")
                if not _prompt_confirm("使用此代理?", default=True):
                    proxy = _prompt_text("输入代理地址 (留空=不使用代理)").strip() or None
                else:
                    proxy = env_proxy
            else:
                proxy = _prompt_text("输入代理地址 (如 http://127.0.0.1:7890，留空=不使用代理)").strip() or None
        else:
            if proxy:
                _console_log(f"[Info] 非交互环境，使用 config.json 中的代理: {proxy}")
            elif env_proxy:
                proxy = env_proxy
                _console_log(f"[Info] 非交互环境，使用环境变量代理: {proxy}")
            else:
                proxy = None
                _console_log("[Info] 非交互环境，未检测到代理配置", level="warn")

        if proxy:
            _console_log(f"[Info] 使用代理: {proxy}")
        else:
            _console_log("[Info] 不使用代理", level="warn")

        # 输入注册数量（指定邮箱模式跳过，只注册 1 个）
        source = _get_email_source_for_provider(mail_provider)
        single_address_mode = _email_source_requires_single_address(source)
        if custom_email or single_address_mode:
            total_accounts = 1
            max_workers = 1
        elif main_args["count"] is not None or main_args["workers"] is not None:
            total_accounts = main_args["count"] or DEFAULT_TOTAL_ACCOUNTS
            max_workers = main_args["workers"] or DEFAULT_MAX_WORKERS
        elif interactive:
            total_accounts = _prompt_positive_int("注册账号数量", DEFAULT_TOTAL_ACCOUNTS)
            max_workers = _prompt_positive_int("并发数", DEFAULT_MAX_WORKERS)
        else:
            total_accounts = DEFAULT_TOTAL_ACCOUNTS
            max_workers = DEFAULT_MAX_WORKERS
            _console_log(f"[Info] 非交互环境，注册数量: {total_accounts}")
            _console_log(f"[Info] 非交互环境，并发数: {max_workers}")

        if ENABLE_OAUTH and interactive and cli_args.oauth_add_phone_sms is None:
            _set_oauth_add_phone_sms_enabled(
                _prompt_oauth_add_phone_sms(OAUTH_ADD_PHONE_SMS, label="OAuth")
            )

        if total_accounts <= 1 and not _force_tui:
            _force_no_tui = True
            os.environ["CHATGPT_REGISTER_NO_TUI"] = "1"

        run_batch(total_accounts=total_accounts, output_file=DEFAULT_OUTPUT_FILE,
                  max_workers=max_workers, proxy=proxy, custom_email=custom_email,
                  mail_provider=mail_provider)
    except KeyboardInterrupt:
        _shutdown_event.set()
        _console_log("\n[Cancel] Interrupted by user.", level="warn")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
