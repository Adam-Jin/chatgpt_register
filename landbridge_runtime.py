"""landbridge (proxychain) 进程级单例

链路: 客户端 -> LocalProxyServer(本地) -> SharedUpstream(xray, 订阅节点) -> arxlabs gateway -> target

每个 worker 一个 Landing, 共享 gateway host/port/password, 仅 username 按 JS Ra() 规则模板拼接,
且每个 worker 独立 sid (Rotating 也加, 强制 username 唯一以走独立 CONNECT 隧道).

对外:
  - configure(cfg, config_path)       记下配置 + config.json 路径 (持久化用)
  - is_enabled()                      读 cfg["enabled"]
  - set_enabled(b, persist=False)     Q1 之后调
  - apply_overrides(overrides)        合并 Q3-Q7 答案到 proxy_user_template
  - persist_to_config()               把当前 cfg 写回 config.json
  - start_for_workers(worker_ids)     起 upstream + N 个 Landing + LocalProxyServer
  - stop()                            幂等关闭 (atexit 已注册)
  - assign_worker_landings(worker_ids) -> {wid: proxy_url}
  - landing_ids() -> list[str]
"""
from __future__ import annotations

import atexit
import json
import os
import secrets
import string
import threading
from typing import Any, Dict, List, Optional


_lock = threading.Lock()
_cfg: Dict[str, Any] = {}
_config_path: Optional[str] = None
_started: bool = False
_upstream = None
_pool = None
_server = None
_landing_ids: List[str] = []
_worker_to_landing: Dict[str, str] = {}


def configure(cfg: Optional[Dict[str, Any]], config_path: Optional[str] = None) -> None:
    global _cfg, _config_path
    with _lock:
        _cfg = dict(cfg or {})
        if config_path:
            _config_path = config_path


def is_enabled() -> bool:
    return bool(_cfg.get("enabled"))


def is_started() -> bool:
    return _started


def get_cfg() -> Dict[str, Any]:
    return dict(_cfg)


def landing_ids() -> List[str]:
    return list(_landing_ids)


def set_enabled(enabled: bool, persist: bool = False) -> None:
    with _lock:
        _cfg["enabled"] = bool(enabled)
    if persist:
        persist_to_config()


def apply_overrides(overrides: Optional[Dict[str, Any]]) -> None:
    """合并 Q3-Q7 的答案到 proxy_user_template (只覆盖给定 key)。"""
    if not overrides:
        return
    with _lock:
        tpl = dict(_cfg.get("proxy_user_template") or {})
        for k, v in overrides.items():
            tpl[k] = v
        _cfg["proxy_user_template"] = tpl


def persist_to_config() -> None:
    """把当前 _cfg 写回 config.json 的 landbridge 段。"""
    if not _config_path or not os.path.exists(_config_path):
        return
    with _lock:
        cfg_snapshot = dict(_cfg)
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            full = json.load(f)
        full["landbridge"] = cfg_snapshot
        tmp = _config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _config_path)
    except Exception:
        # 配置写失败不应阻塞主流程
        pass


def _rand_sid(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _build_proxy_user(account: str, template: Dict[str, Any], sid: str, t_minutes: int) -> str:
    """按 arxlabs 前端 Ra() 规则拼装 username。

    <account>
      + (country ? `-region-${country}` : "")
      + (state   ? `-st-${state}`       : "")
      + (city    ? `-city-${city}`      : "")
      + `-sid-${sid}-t-${t_minutes}`     ← 永远加 (Rotating 也加, 用 t=1)
    """
    parts: List[str] = [account]
    country = (template.get("country") or "").strip()
    if country:
        parts.append(f"-region-{country}")
    state = (template.get("state") or "").strip()
    if state:
        parts.append(f"-st-{state}")
    city = (template.get("city") or "").strip()
    if city:
        parts.append(f"-city-{city}")
    parts.append(f"-sid-{sid}-t-{int(t_minutes)}")
    return "".join(parts)


def _build_worker_landings(worker_ids: List[str]):
    """为每个 worker 构造一个 Landing。返回 (landings_dict, default_id)。"""
    from proxychain import Landing  # 延迟 import, enabled=false 时不需要装

    gw = _cfg.get("gateway") or {}
    if not gw.get("host") or not gw.get("port") or not gw.get("account"):
        raise RuntimeError("landbridge.gateway 必须包含 host / port / account")

    host = str(gw["host"])
    port = int(gw["port"])
    account = str(gw["account"])
    password = str(gw.get("password", ""))
    tls = bool(gw.get("tls", False))
    sni = str(gw.get("sni", ""))
    skip_cert_verify = bool(gw.get("skip_cert_verify", False))

    template = _cfg.get("proxy_user_template") or {}
    ip_mode = str(template.get("ip_mode", "Sticky"))
    if ip_mode == "Sticky":
        t_minutes = int(template.get("sticky_minutes", 5))
    else:
        t_minutes = 1  # Rotating 也写 sid+t, 但 t=1 让上游频繁轮换

    landings: Dict[str, Any] = {}
    for wid in worker_ids:
        sid = _rand_sid(8)
        username = _build_proxy_user(account, template, sid, t_minutes)
        landings[wid] = Landing(
            server=host,
            port=port,
            username=username,
            password=password,
            name=wid,
            tls=tls,
            sni=sni,
            skip_cert_verify=skip_cert_verify,
        )
    default_id = worker_ids[0] if worker_ids else None
    return landings, default_id


def start_for_workers(worker_ids: List[str]) -> None:
    """按 worker 数构建 N 个 Landing, 起 upstream + LocalProxyServer。幂等。"""
    global _started, _upstream, _pool, _server, _landing_ids, _worker_to_landing

    if not is_enabled():
        return
    if not worker_ids:
        return

    with _lock:
        if _started:
            return

        from proxychain import (
            LandingPool, LocalProxyServer, SharedUpstream,
            load_subscription_node,
        )

        sub = _cfg.get("subscription") or {}
        sub_source = sub.get("path") or sub.get("url")
        node_name = sub.get("node_name")
        if not sub_source or not node_name:
            raise RuntimeError(
                "landbridge.subscription 必须包含 path 或 url, 以及 node_name"
            )
        allowed_types = sub.get("allowed_types")

        landings, default_id = _build_worker_landings(worker_ids)
        if not landings:
            raise RuntimeError("landbridge: worker_ids 为空, 无法生成 landing")

        upstream_node = load_subscription_node(
            sub_source, node_name,
            allowed_types=set(allowed_types) if allowed_types else None,
        )

        xray_cfg = _cfg.get("xray") or {}
        upstream = SharedUpstream.from_node(
            upstream_node,
            listen_host=str(xray_cfg.get("local_socks_host", "127.0.0.1")),
            listen_port=int(xray_cfg.get("local_socks_port", 10808)),
            startup_timeout=float(xray_cfg.get("startup_timeout_seconds", 10)),
            loglevel=str(xray_cfg.get("loglevel", "error")),
        )
        upstream.start()

        pool = LandingPool(landings, default=default_id)

        server_cfg = _cfg.get("server") or {}
        server = LocalProxyServer(
            upstream, pool,
            bind=str(server_cfg.get("bind", "127.0.0.1")),
            port=int(server_cfg.get("port", 0)),
            dial_timeout=int(server_cfg.get("dial_timeout", 10)),
        )
        server.start()

        _upstream = upstream
        _pool = pool
        _server = server
        _landing_ids = list(landings.keys())
        _worker_to_landing = {wid: wid for wid in worker_ids}
        _started = True


def stop() -> None:
    global _started, _upstream, _pool, _server
    with _lock:
        if not _started:
            return
        try:
            if _server is not None:
                _server.stop()
        except Exception:
            pass
        try:
            if _upstream is not None:
                _upstream.stop()
        except Exception:
            pass
        _server = None
        _pool = None
        _upstream = None
        _started = False


def proxy_url(landing_id: Optional[str] = None, password: str = "_") -> str:
    if not _started or _server is None:
        raise RuntimeError("landbridge runtime not started; call start_for_workers() first")
    target = landing_id or (_landing_ids[0] if _landing_ids else None)
    if target is None:
        raise RuntimeError("landbridge: 没有可用 landing")
    return _server.proxy_url_for(target, password=password)


def assign_worker_landings(worker_ids: List[str]) -> Dict[str, str]:
    """每个 worker 用自己专属的 landing (worker_id == landing_id)。

    未启动时返回空 dict (调用方应回退到原 proxy)。
    """
    if not _started or not _landing_ids:
        return {}
    mapping: Dict[str, str] = {}
    for wid in worker_ids:
        landing_id = _worker_to_landing.get(wid)
        if not landing_id:
            # 这个 worker 不在 start_for_workers 时给定的列表里, 退化用第一个
            landing_id = _landing_ids[0]
        mapping[wid] = proxy_url(landing_id)
    return mapping


atexit.register(stop)
