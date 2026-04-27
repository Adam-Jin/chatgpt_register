#!/usr/bin/env python3

"""
sentinel_solver.py
独立 HTTP 服务：用真 Chromium 跑 OpenAI sentinel SDK，返回 sentinel token。
chatgpt_register.py 通过 POST /sentinel/token 调用本服务，避免本地硬算 PoW + Turnstile VM。
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Callable, Optional

from patchright.async_api import async_playwright
from log_config import DEFAULT_LOG_LEVEL, normalize_log_level, should_log

try:
    from quart import Quart, jsonify, request
except Exception:
    Quart = None
    jsonify = None
    request = None

from browser_configs import browser_config


# 直接从 auth.openai.com 入口加载，减少不必要的页面跳转
CANDIDATE_PAGES = [
    "https://auth.openai.com/",
]
SDK_READY_JS = (
    "() => typeof window.SentinelSDK !== 'undefined' "
    "&& typeof window.SentinelSDK.token === 'function'"
)
# 兜底：如果页面没自带，手动注入 SDK 脚本。需要从 DOM 里抓 sv 版本号。
INJECT_SDK_JS = """
async () => {
    if (window.SentinelSDK && window.SentinelSDK.token) return 'already_loaded';
    // 试图从已有 script 里找 sentinel sdk url
    let sdkUrl = null;
    for (const s of document.scripts) {
        if (s.src && /sentinel\\.openai\\.com\\/sentinel\\/[^/]+\\/sdk\\.js/.test(s.src)) {
            sdkUrl = s.src; break;
        }
    }
    if (!sdkUrl) return 'no_sdk_url_in_page';
    await new Promise((res, rej) => {
        const s = document.createElement('script');
        s.src = sdkUrl;
        s.onload = res;
        s.onerror = () => rej(new Error('script load failed'));
        document.head.appendChild(s);
    });
    return 'injected';
}
"""
DEFAULT_FLOW = "username_password_create"
DUMP_DIR = "/tmp/sentinel_solver_dumps"


def _parse_proxy(proxy_url: str):
    """把 'http://user:pass@host:port' 拆成 playwright proxy dict。"""
    if not proxy_url:
        return None
    from urllib.parse import urlparse
    p = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    out = {"server": f"{p.scheme}://{p.hostname}:{p.port}" if p.port else f"{p.scheme}://{p.hostname}"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _to_logging_level(level: str) -> int:
    normalized = normalize_log_level(level)
    if normalized == "debug":
        return logging.DEBUG
    if normalized in {"warn", "error"}:
        return logging.WARNING if normalized == "warn" else logging.ERROR
    return logging.INFO


def _emit_callback(callback: Callable[..., None], message: str, *, level: str) -> None:
    try:
        callback(message, level=level)
        return
    except TypeError:
        callback(message)


COLORS = {
    "MAGENTA": "\033[35m", "BLUE": "\033[34m", "GREEN": "\033[32m",
    "YELLOW": "\033[33m", "RED": "\033[31m", "RESET": "\033[0m",
}


def _fmt(level, color, msg):
    ts = time.strftime("%H:%M:%S")
    return f"[{ts}] [{COLORS[color]}{level}{COLORS['RESET']}] -> {msg}"


class _LogProxy:
    def __init__(self, base_logger):
        self._base = base_logger

    def setLevel(self, level):
        self._base.setLevel(level)

    def info(self, msg, *a, **kw):
        self._base.info(_fmt("INFO", "BLUE", msg), *a, **kw)

    def success(self, msg, *a, **kw):
        self._base.info(_fmt("SUCCESS", "GREEN", msg), *a, **kw)

    def warning(self, msg, *a, **kw):
        self._base.warning(_fmt("WARN", "YELLOW", msg), *a, **kw)

    def error(self, msg, *a, **kw):
        self._base.error(_fmt("ERROR", "RED", msg), *a, **kw)

    def debug(self, msg, *a, **kw):
        self._base.debug(_fmt("DEBUG", "MAGENTA", msg), *a, **kw)


class _CallbackLogger:
    def __init__(self, callback: Callable[[str], None], min_level: str):
        self._callback = callback
        self._min_level = normalize_log_level(min_level)

    def setLevel(self, level):
        return None

    def info(self, msg, *a, **kw):
        if should_log("info", self._min_level):
            _emit_callback(self._callback, str(msg), level="info")

    def warning(self, msg, *a, **kw):
        if should_log("warn", self._min_level):
            _emit_callback(self._callback, str(msg), level="warn")

    def error(self, msg, *a, **kw):
        if should_log("error", self._min_level):
            _emit_callback(self._callback, str(msg), level="error")

    def debug(self, msg, *a, **kw):
        if should_log("debug", self._min_level):
            _emit_callback(self._callback, str(msg), level="debug")


def _load_runtime_config():
    config = {
        "thread": 2,
        "headless": True,
        "channel": "chromium",
        "log_level": DEFAULT_LOG_LEVEL,
    }
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                config["thread"] = int(payload.get("sentinel_solver_thread", config["thread"]) or config["thread"])
                config["headless"] = _as_bool(payload.get("sentinel_solver_headless", config["headless"]))
                config["channel"] = str(payload.get("sentinel_solver_channel", config["channel"]) or config["channel"])
                config["log_level"] = normalize_log_level(
                    payload.get("log_level", config["log_level"]),
                    default=config["log_level"],
                )
        except Exception:
            pass
    config["thread"] = int(os.environ.get("SENTINEL_SOLVER_THREAD", config["thread"]) or config["thread"])
    config["headless"] = _as_bool(os.environ.get("SENTINEL_SOLVER_HEADLESS", config["headless"]))
    config["channel"] = str(os.environ.get("SENTINEL_SOLVER_CHANNEL", config["channel"]) or config["channel"])
    config["log_level"] = normalize_log_level(
        os.environ.get("SENTINEL_SOLVER_LOG_LEVEL", os.environ.get("LOG_LEVEL", config["log_level"])),
        default=config["log_level"],
    )
    return config


_base_logger = logging.getLogger("SentinelSolver")
if not _base_logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _base_logger.addHandler(_h)
_base_logger.setLevel(logging.INFO)
_base_logger.propagate = False
logger = _LogProxy(_base_logger)


class SentinelSolver:
    def __init__(self, headless: bool, thread: int, debug: bool, channel: str,
                 default_proxy: Optional[str] = None, enable_http: bool = True,
                 log: Optional[Callable[[str], None]] = None,
                 log_level: Optional[str] = None):
        global logger
        self.app = None
        self.headless = headless
        self.thread = thread
        self.debug = debug
        self.log_level = normalize_log_level(log_level, default="debug" if debug else DEFAULT_LOG_LEVEL)
        self.channel = channel
        self.default_proxy = default_proxy
        self.browser_pool: asyncio.Queue = asyncio.Queue()
        self.resource_cache = {}
        self.busy = 0
        self._playwright = None
        self._started = False
        self._start_lock = asyncio.Lock()

        if log is not None:
            logger = _LogProxy(_CallbackLogger(log, min_level=self.log_level))
        else:
            logger = _LogProxy(_base_logger)
            logger.setLevel(_to_logging_level(self.log_level))

        if enable_http:
            self._setup_http_routes()

    def _setup_http_routes(self):
        if Quart is None:
            raise RuntimeError("quart 未安装，无法启用 HTTP 模式")
        self.app = Quart(__name__)
        self.app.before_serving(self._startup)
        self.app.after_serving(self._shutdown)
        self.app.route("/sentinel/token", methods=["POST"])(self.solve)
        self.app.route("/health", methods=["GET"])(self.health)
        # 同步 Turnstile 接口：POST {url,sitekey,...} → {token}（统一到这个服务）
        self.app.route("/turnstile/token", methods=["POST"])(self.solve_turnstile)

    async def _startup(self):
        async with self._start_lock:
            if self._started:
                return
            logger.info(f"启动 Patchright {self.channel} 浏览器池, thread={self.thread}, headless={self.headless}")
            self._playwright = await async_playwright().start()
            for i in range(self.thread):
                try:
                    browser = await self._playwright.chromium.launch(
                        channel=self.channel,
                        headless=self.headless,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                except Exception as e:
                    logger.warning(f"channel={self.channel} 启动失败: {e}; 回退到默认 chromium")
                    browser = await self._playwright.chromium.launch(
                        headless=self.headless,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                await self.browser_pool.put((i + 1, browser))
                logger.info(f"Browser #{i + 1} 就绪")
            logger.success(f"浏览器池初始化完成, size={self.browser_pool.qsize()}")
            self._started = True

    async def _shutdown(self):
        async with self._start_lock:
            if not self._started:
                return
            while not self.browser_pool.empty():
                _, browser = await self.browser_pool.get()
                try:
                    await browser.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._playwright = None
            self._started = False

    async def ensure_started(self):
        await self._startup()

    async def health_data(self):
        await self.ensure_started()
        return {
            "ok": True,
            "pool_size": self.thread,
            "available": self.browser_pool.qsize(),
            "busy": self.busy,
            "mode": "inprocess" if self.app is None else "http",
        }

    async def health(self):
        return jsonify(await self.health_data())

    async def solve(self):
        try:
            payload = await request.get_json(force=True)
        except Exception:
            return jsonify({"error": "invalid json"}), 400
        token, err = await self.solve_token(
            flow=(payload or {}).get("flow"),
            oai_did=(payload or {}).get("oai_did"),
            ua=(payload or {}).get("user_agent"),
            proxy=(payload or {}).get("proxy"),
        )
        if token:
            return jsonify({"token": token})
        return jsonify({"error": err or "unknown"}), 500

    async def solve_token(self, flow=None, oai_did=None, ua=None, proxy=None):
        await self.ensure_started()
        flow = flow or DEFAULT_FLOW
        oai_did = oai_did or str(uuid.uuid4())
        proxy = proxy or self.default_proxy

        if not ua:
            _, _, ua, _ = browser_config.get_random_browser_config("chromium")

        req_id = uuid.uuid4().hex[:8]
        logger.info(f"[{req_id}] 收到请求 flow={flow} oai_did={oai_did[:8]}… proxy={proxy or '-'}")
        return await self._do_solve(req_id, flow, oai_did, ua, proxy)

    async def _do_solve(self, req_id, flow, oai_did, ua, proxy=None):
        import re as _re
        index, browser = await self.browser_pool.get()
        self.busy += 1
        context = None
        page = None
        cdp = None
        start = time.time()
        sdk_seen = []  # 嗅探到的 sentinel SDK URL（用作 SDK 已加载完成的信号）
        try:
            ctx_opts = {
                "user_agent": ua,
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
            }
            if proxy:
                ctx_opts["proxy"] = _parse_proxy(proxy)
            context = await browser.new_context(**ctx_opts)
            await context.add_cookies([
                {"name": "oai-did", "value": oai_did, "domain": ".chatgpt.com", "path": "/"},
                {"name": "oai-did", "value": oai_did, "domain": ".openai.com", "path": "/"},
            ])
            page = await context.new_page()
            await self._block_rendering(page)
            self._attach_resource_cache(page, req_id)
            page.on("console", lambda m: logger.debug(f"[{req_id}] console {m.type}: {m.text[:200]}"))
            page.on("pageerror", lambda e: logger.debug(f"[{req_id}] pageerror: {str(e)[:200]}"))

            def _on_request_failed(request):
                logger.debug(
                    f"[{req_id}] requestfailed: {request.method} {request.resource_type} "
                    f"{request.url} err={request.failure or 'unknown'}"
                )
            page.on("requestfailed", _on_request_failed)

            sdk_re = _re.compile(r"https://sentinel\.openai\.com/sentinel/[^/]+/sdk\.js")
            def _on_resp(resp):
                if sdk_re.search(resp.url) and resp.url not in sdk_seen:
                    sdk_seen.append(resp.url)
                    logger.info(f"[{req_id}] 嗅探到 sentinel sdk: {resp.url}")
            page.on("response", _on_resp)

            # CDP session：用 Runtime.evaluate 在主世界跑 JS（绕开 patchright 的 isolated world）
            cdp = await context.new_cdp_session(page)
            await cdp.send("Runtime.enable")

            async def cdp_eval(expression, await_promise=False):
                """通过 CDP 在主世界执行 JS，与 devtools console 等价。"""
                r = await cdp.send("Runtime.evaluate", {
                    "expression": expression,
                    "awaitPromise": await_promise,
                    "returnByValue": True,
                    "userGesture": True,
                })
                return r

            PROBE_EXPR = (
                "({type: typeof SentinelSDK, "
                "tokenType: (typeof SentinelSDK !== 'undefined' && SentinelSDK) ? typeof SentinelSDK.token : null, "
                "winType: typeof window.SentinelSDK})"
            )
            DOM_SCAN_EXPR = (
                "(() => { for (const s of document.scripts) {"
                " if (s.src && /sentinel\\.openai\\.com\\/sentinel\\/[^/]+\\/sdk\\.js/.test(s.src))"
                " return s.src; } return null; })()"
            )

            async def _probe_sdk_alive():
                p = await cdp_eval(PROBE_EXPR)
                v = (p.get("result") or {}).get("value") or {}
                return v.get("tokenType") == "function", v

            loaded_url = None
            sdk_alive = False
            probe_val = None
            for cand in CANDIDATE_PAGES:
                sdk_seen.clear()  # 每个候选页独立判定, 防止跨页残留误判
                try:
                    logger.info(f"[{req_id}] (browser#{index}) goto {cand}")
                    resp = await page.goto(cand, wait_until="domcontentloaded", timeout=30000)
                    loaded_url = page.url
                    logger.info(f"[{req_id}] loaded -> {loaded_url} status={resp.status if resp else '-'}")
                except Exception as e:
                    logger.warning(f"[{req_id}] goto {cand} 失败: {e}")
                    continue

                # 1) 等 sdk 资源 (network) 加载完, 最多 8s
                for _ in range(40):
                    if sdk_seen:
                        break
                    await asyncio.sleep(0.2)

                # 2) network 没嗅到? 扫 DOM <script> 兜底 (cache 命中时 response 不触发)
                if not sdk_seen:
                    try:
                        r = await cdp_eval(DOM_SCAN_EXPR)
                        dom_url = (r.get("result") or {}).get("value")
                    except Exception as e:
                        logger.debug(f"[{req_id}] DOM 扫描失败: {e}")
                        dom_url = None
                    if dom_url:
                        sdk_seen.append(dom_url)
                        logger.info(f"[{req_id}] 从 DOM 抓到 sentinel sdk: {dom_url}")

                if not sdk_seen:
                    logger.info(f"[{req_id}] 该页面未触发 sentinel sdk 加载, 换下一个")
                    continue

                # 3) 给 SDK 顶层 IIFE 时间, 然后 probe
                for _ in range(10):  # 最多 5s
                    await asyncio.sleep(0.5)
                    sdk_alive, probe_val = await _probe_sdk_alive()
                    if sdk_alive:
                        break

                if sdk_alive:
                    logger.info(f"[{req_id}] SDK 可用 on {page.url}")
                    break

                # 4) 嗅到了 sdk 但 window.SentinelSDK 不在 (常见: 中转 redirect 把上一个 document 冲掉)
                # → 主动 inject 当前页面里残留的 sdk url
                logger.info(f"[{req_id}] SDK 不在 window (probe={probe_val}), 尝试 inject")
                try:
                    ij = await cdp_eval(f"({INJECT_SDK_JS.strip()})()", await_promise=True)
                    ij_val = (ij.get("result") or {}).get("value")
                    logger.info(f"[{req_id}] inject 结果: {ij_val}")
                except Exception as e:
                    logger.warning(f"[{req_id}] inject 异常: {e}")

                # 5) inject 后再 probe
                for _ in range(6):  # 最多 3s
                    await asyncio.sleep(0.5)
                    sdk_alive, probe_val = await _probe_sdk_alive()
                    if sdk_alive:
                        break

                if sdk_alive:
                    logger.info(f"[{req_id}] inject 后 SDK 可用 on {page.url}")
                    break

                logger.info(f"[{req_id}] inject 后仍不可用 (probe={probe_val}), 换下一个候选页")

            if not sdk_seen:
                await self._dump_diag(req_id, page)
                return None, "未嗅探到 sentinel sdk URL；可能代理/Cloudflare 阻断"

            if not sdk_alive:
                logger.info(f"[{req_id}] SDK probe (final): {probe_val}")
                await self._dump_diag(req_id, page)
                return None, f"SentinelSDK 不可见: {probe_val}"

            logger.info(f"[{req_id}] SDK probe: {probe_val}")

            # 调 token —— 完全模拟 console 输入
            expr = (
                "(async () => {"
                "  let ie = null;"
                "  try { try { await SentinelSDK.init(" + json.dumps(flow) + "); } catch(e) { ie = String(e && e.stack || e); } "
                "    const t = await SentinelSDK.token(" + json.dumps(flow) + ");"
                "    return { ok: true, token: t, init_err: ie };"
                "  } catch(e) { return { ok: false, error: String(e && e.stack || e), init_err: ie }; }"
                "})()"
            )
            r = await cdp_eval(expr, await_promise=True)
            if r.get("exceptionDetails"):
                err = json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400]
                logger.warning(f"[{req_id}] CDP eval 异常: {err}")
                await self._dump_diag(req_id, page)
                return None, err
            result = (r.get("result") or {}).get("value")

            elapsed = round(time.time() - start, 2)
            if isinstance(result, dict):
                ie = result.get("init_err")
                if ie:
                    logger.warning(f"[{req_id}] SentinelSDK.init 报错(已被吞): {str(ie)[:400]}")
            if isinstance(result, dict) and result.get("ok"):
                token = result.get("token")
                if isinstance(token, str) and token.startswith("{") and '"c"' in token:
                    logger.success(f"[{req_id}] token ok ({elapsed}s) {token[:60]}…")
                    return token, None
                logger.warning(f"[{req_id}] token 形态异常: {str(token)[:200]}")
                await self._dump_diag(req_id, page)
                return None, f"unexpected token shape: {str(token)[:200]}"
            err = (result or {}).get("error", "unknown") if isinstance(result, dict) else f"raw={result}"
            logger.warning(f"[{req_id}] SDK 调用失败 ({elapsed}s): {err[:300]}")
            await self._dump_diag(req_id, page)
            return None, err
        except Exception as e:
            elapsed = round(time.time() - start, 2)
            logger.error(f"[{req_id}] solve 失败 ({elapsed}s): {e}")
            if page is not None:
                await self._dump_diag(req_id, page)
            return None, str(e)
        finally:
            if cdp is not None:
                try: await cdp.detach()
                except Exception: pass
            if context is not None:
                try: await context.close()
                except Exception: pass
            self.busy -= 1
            await self.browser_pool.put((index, browser))

    async def _dump_diag(self, req_id, page):
        try:
            os.makedirs(DUMP_DIR, exist_ok=True)
            png = os.path.join(DUMP_DIR, f"{req_id}.png")
            html = os.path.join(DUMP_DIR, f"{req_id}.html")
            await page.screenshot(path=png, full_page=True)
            content = await page.content()
            with open(html, "w", encoding="utf-8") as f:
                f.write(content)
            logger.warning(f"[{req_id}] 诊断已保存: {png} / {html} (url={page.url})")
        except Exception as e:
            logger.warning(f"[{req_id}] 诊断保存失败: {e}")


# ============================================================
# Turnstile (从 D3vin/Turnstile-Solver-NEW 移植，复用同一个浏览器池)
# ============================================================

    async def solve_turnstile(self):
        """POST /turnstile/token  body: {url, sitekey, action?, cdata?, proxy?}
        同步返回 {token} 或 500 {error}。和 /sentinel/token 同风格。"""
        try:
            payload = await request.get_json(force=True)
        except Exception:
            return jsonify({"error": "invalid json"}), 400
        url = (payload or {}).get("url")
        sitekey = (payload or {}).get("sitekey")
        action = (payload or {}).get("action")
        cdata = (payload or {}).get("cdata")
        if not url or not sitekey:
            return jsonify({"error": "url and sitekey are required"}), 400

        token, err = await self.solve_turnstile_token(
            url=url,
            sitekey=sitekey,
            action=action,
            cdata=cdata,
            proxy=(payload or {}).get("proxy"),
        )
        if token:
            return jsonify({"token": token})
        return jsonify({"error": err or "unknown"}), 500

    async def solve_turnstile_token(self, url, sitekey, action=None, cdata=None, proxy=None):
        await self.ensure_started()
        req_id = uuid.uuid4().hex[:8]
        proxy = proxy or self.default_proxy
        logger.info(f"[ts-{req_id}] 收到请求 url={url[:80]} sitekey={sitekey} proxy={proxy or '-'}")
        return await self._do_turnstile(req_id, url, sitekey, action, cdata, proxy)

    async def _do_turnstile(self, req_id, url, sitekey, action, cdata, proxy):
        """从 D3vin/Turnstile-Solver-NEW 移植：导航 → 等 input[name=cf-turnstile-response] 填充。"""
        index, browser = await self.browser_pool.get()
        self.busy += 1
        context = None
        page = None
        start_time = time.time()
        try:
            _, _, ua, sec_ch_ua = browser_config.get_random_browser_config("chromium")
            ctx_opts = {
                "user_agent": ua,
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
            }
            if sec_ch_ua:
                ctx_opts["extra_http_headers"] = {"sec-ch-ua": sec_ch_ua}
            if proxy:
                ctx_opts["proxy"] = _parse_proxy(proxy)
            context = await browser.new_context(**ctx_opts)
            page = await context.new_page()
            await self._block_rendering(page)
            self._attach_resource_cache(page, req_id)

            logger.info(f"[ts-{req_id}] (browser#{index}) goto {url[:100]}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._unblock_rendering(page)
            await asyncio.sleep(3)

            locator = page.locator('input[name="cf-turnstile-response"]')
            max_attempts = 20
            for attempt in range(max_attempts):
                try:
                    try:
                        count = await locator.count()
                    except Exception:
                        count = 0
                    if count == 1:
                        try:
                            token = await locator.input_value(timeout=500)
                            if token:
                                elapsed = round(time.time() - start_time, 3)
                                logger.success(f"[ts-{req_id}] solved {token[:10]}… in {elapsed}s")
                                return token, None
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"[ts-{req_id}] token check err: {e}")
                    elif count > 1:
                        for i in range(count):
                            try:
                                t = await locator.nth(i).input_value(timeout=500)
                                if t:
                                    elapsed = round(time.time() - start_time, 3)
                                    logger.success(f"[ts-{req_id}] solved {t[:10]}… in {elapsed}s")
                                    return t, None
                            except Exception:
                                continue
                    if attempt > 2 and attempt % 3 == 0:
                        await self._try_click_strategies(page, index)
                    if attempt == 10:
                        try:
                            cur = await locator.count()
                        except Exception:
                            cur = 0
                        if cur == 0:
                            if self.debug:
                                logger.debug(f"[ts-{req_id}] inject overlay fallback")
                            await self._load_captcha_overlay(page, sitekey, action or "")
                            await asyncio.sleep(2)
                    await asyncio.sleep(min(0.5 + attempt * 0.05, 2.0))
                except Exception as e:
                    if self.debug:
                        logger.debug(f"[ts-{req_id}] attempt {attempt+1} err: {e}")
            elapsed = round(time.time() - start_time, 3)
            logger.warning(f"[ts-{req_id}] FAIL after {elapsed}s")
            return None, f"timeout after {elapsed}s"
        except Exception as e:
            elapsed = round(time.time() - start_time, 3)
            logger.error(f"[ts-{req_id}] solve exc ({elapsed}s): {e}")
            return None, str(e)
        finally:
            if context is not None:
                try: await context.close()
                except Exception: pass
            self.busy -= 1
            await self.browser_pool.put((index, browser))

    # ---------- helpers from D3vin ----------

    async def _optimized_route_handler(self, route):
        req = route.request
        rt = req.resource_type
        if rt == "script" and req.method == "GET":
            cached = self.resource_cache.get(req.url)
            if cached:
                await route.fulfill(
                    status=cached["status"],
                    headers=cached["headers"],
                    body=cached["body"],
                )
                return
        if rt in {"document", "script", "xhr", "fetch"}:
            await route.continue_()
        else:
            await route.abort()

    async def _block_rendering(self, page):
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        await page.unroute("**/*", self._optimized_route_handler)

    def _attach_resource_cache(self, page, req_id):
        def _on_response(response):
            asyncio.create_task(self._maybe_cache_script(response, req_id))
        page.on("response", _on_response)

    async def _maybe_cache_script(self, response, req_id):
        try:
            req = response.request
            if req.method != "GET" or req.resource_type != "script" or response.status != 200:
                return
            body = await response.body()
            if not body:
                return
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers.pop("content-encoding", None)
            headers.pop("transfer-encoding", None)
            headers.pop("connection", None)
            if req.url not in self.resource_cache:
                self.resource_cache[req.url] = {
                    "status": response.status,
                    "headers": headers,
                    "body": body,
                }
                if self.debug:
                    logger.debug(f"[{req_id}] cache script: {req.url}")
        except Exception as e:
            if self.debug:
                logger.debug(f"[{req_id}] cache script 失败: {e}")

    async def _safe_click(self, page, selector, index):
        try:
            await page.locator(selector).first.click(timeout=1000)
            return True
        except Exception:
            return False

    async def _find_and_click_checkbox(self, page, index):
        iframe_selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
        ]
        iframe_locator = None
        for sel in iframe_selectors:
            try:
                test = page.locator(sel).first
                if await test.count() > 0:
                    iframe_locator = test
                    break
            except Exception:
                continue
        if not iframe_locator:
            return False
        try:
            handle = await iframe_locator.element_handle()
            frame = await handle.content_frame()
            if frame:
                for sel in ['input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]']:
                    try:
                        await frame.locator(sel).first.click(timeout=2000)
                        return True
                    except Exception:
                        continue
            try:
                await iframe_locator.click(timeout=1000)
                return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    async def _try_click_strategies(self, page, index):
        strategies = [
            ("checkbox_click", lambda: self._find_and_click_checkbox(page, index)),
            ("direct_widget", lambda: self._safe_click(page, ".cf-turnstile", index)),
            ("iframe_click", lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ("js_click", lambda: page.evaluate(
                "document.querySelector('.cf-turnstile')?.click()")),
            ("sitekey_attr", lambda: self._safe_click(page, "[data-sitekey]", index)),
        ]
        for name, fn in strategies:
            try:
                r = await fn()
                if r is True or r is None:
                    if self.debug:
                        logger.debug(f"click strategy '{name}' ok")
                    return True
            except Exception:
                continue
        return False

    async def _load_captcha_overlay(self, page, sitekey, action=""):
        script = f"""
        const existing = document.querySelector('#captcha-overlay');
        if (existing) existing.remove();
        const overlay = document.createElement('div');
        overlay.id = 'captcha-overlay';
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,.5);z-index:1000;';
        const div = document.createElement('div');
        div.className = 'cf-turnstile';
        div.setAttribute('data-sitekey', '{sitekey}');
        div.setAttribute('data-action', '{action}');
        overlay.appendChild(div);
        document.body.appendChild(overlay);
        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
        s.async = true; s.defer = true;
        document.head.appendChild(s);
        """
        await page.evaluate(script)


def main():
    runtime_cfg = _load_runtime_config()
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5732)
    p.add_argument("--thread", type=int, default=runtime_cfg["thread"])
    p.add_argument("--headless", dest="headless", action="store_true", default=runtime_cfg["headless"])
    p.add_argument("--no-headless", dest="headless", action="store_false")
    p.add_argument("--channel", default=runtime_cfg["channel"],
                   help="patchright 浏览器 channel: chromium / chrome / msedge")
    p.add_argument("--proxy", default=None,
                   help="默认代理 URL（如 http://127.0.0.1:7890），请求 body 里的 proxy 字段优先")
    p.add_argument("--log-level", choices=["debug", "info", "warn", "error"], default=None)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    effective_log_level = normalize_log_level(
        args.log_level,
        default="debug" if args.debug else runtime_cfg["log_level"],
    )

    default_proxy = args.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
                    or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")

    solver = SentinelSolver(
        headless=args.headless,
        thread=args.thread,
        debug=args.debug,
        channel=args.channel,
        default_proxy=default_proxy,
        log_level=effective_log_level,
    )
    if default_proxy:
        logger.info(f"使用默认代理: {default_proxy}")
    logger.info(f"监听 http://{args.host}:{args.port}")
    solver.app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
