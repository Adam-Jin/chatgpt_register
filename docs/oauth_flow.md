# Codex OAuth 链路速查

记录 ChatGPT/Codex CLI OAuth 注册/登录的完整 HTTP 链路、关键参数、常见踩坑点和修复历史。
代码主入口: `chatgpt_register.py` 的 `perform_codex_oauth_login_http`
(`_oauth_submit_workspace_and_org` / `_oauth_follow_for_code` / `_oauth_allow_redirect_extract_code`)。

---

## 1. 一图速览 (HAR 验证过的成功链路)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CLIENT                                            SERVER                   │
└─────────────────────────────────────────────────────────────────────────────┘

[1] GET  /oauth/authorize                                                ── 必带 3 flag ──┐
       ?response_type=code                                                                │
       &client_id=app_EMoamEEZ73f0CkXaXp7hrann                                            │
       &redirect_uri=http://localhost:1455/auth/callback                                  │
       &scope=openid email profile offline_access                                         │
       &code_challenge=<S256>                                                             │
       &state=<随机>                                                                       │
       &codex_cli_simplified_flow=true   ◀── 没它就被当通用 web 客户端, 强制进 codex_org    │
       &id_token_add_organizations=true                                                   │
       &prompt=login                                                                      │
                                          ──► 200, 重定向链拿到 login_session cookie     │
                                                                                          │
[2] POST /api/accounts/authorize/continue              (sentinel token)                   │
                                          ──► 200 page=login_password                     │
                                              next=/log-in/password                       │
                                                                                          │
[3] POST /api/accounts/password/verify                 (sentinel token)                   │
                                          ──► 200 page=email_otp_verification             │
                                              next=/email-verification                    │
                                                                                          │
[4] (轮询 IMAP) → POST /api/accounts/email-otp/validate { otp_code }                      │
                                          ──► 200 page=add_phone   (新号)                  │
                                              next=/add-phone                              │
                                              或                                           │
                                          ──► 200 page=sign_in_with_chatgpt_codex_consent │
                                              next=/sign-in-with-chatgpt/codex/consent    │
                                                                                          │
[4.5] (新号才走) POST /api/accounts/add-phone/send → POST /add-phone/validate { sms_otp }  │
                                          ──► 200 page=sign_in_with_chatgpt_codex_consent │
                                              next=/sign-in-with-chatgpt/codex/consent    │
                                                                                          │
[5] GET  /sign-in-with-chatgpt/codex/consent           (预热: 拿最新 oai-client-auth-      │
                                                        session cookie, 含真实 workspaces) │
                                          ──► 200 (HTML)                                  │
                                                                                          │
[6] POST /api/accounts/workspace/select                                                   │
       body: {"workspace_id": "<从 cookie JWT 解出>"}                                      │
                                          ──► 200 page=external_url     ◀── 关键!        │
                                              continue_url=                               │
                                                /api/oauth/oauth2/auth?                   │
                                                  client_id=...                           │
                                                  &code_challenge=...                     │
                                                  &codex_cli_simplified_flow=true         │
                                                  &id_token_add_organizations=true        │
                                                  &login_verifier=<huge>   ◀── 凭证!     │
                                                  &prompt=login                           │
                                                  &redirect_uri=...                       │
                                                  &response_type=code                     │
                                                  &scope=...                              │
                                                  &state=...                              │
                                                                                          │
[7] GET  ${ws_next}   (上面 continue_url, 跟 302/303 链)                                  │
                                          ──► 302 Location:                               │
                                              /api/accounts/consent?consent_challenge=... │
                                          ──► 302 Location:                               │
                                              /api/oauth/oauth2/auth?consent_verifier=... │
                                          ──► 303 Location:                               │
                                              http://localhost:1455/auth/callback?code=ac_x│
                                                                                          │
[8] regex 从 Location 提 code, 不要真去 GET localhost (代理会超时 30s)                    │
                                                                                          │
[9] POST /oauth/token                                                                     │
       body (form): grant_type=authorization_code                                         │
                    &code=ac_xxx                                                          │
                    &redirect_uri=http://localhost:1455/auth/callback                     │
                    &client_id=app_EMoamEEZ73f0CkXaXp7hrann                               │
                    &code_verifier=<step1 PKCE 的明文>                                    │
                                          ──► 200 { access_token, id_token, refresh_token}│
                                                                                          │
                                                                                  ✓ 完成 ─┘
```

---

## 2. 三个必带 flag 的意义

| flag                              | 作用                                                                 |
|-----------------------------------|----------------------------------------------------------------------|
| `codex_cli_simplified_flow=true`  | 告诉服务端这是 codex CLI 流: 自动处理 workspace/org/project, workspace/select 之后直接给 `oauth2/auth?login_verifier=...` |
| `id_token_add_organizations=true` | id_token 里塞 org 信息, 是 simplified flow 的前置条件                |
| `prompt=login`                    | codex CLI 客户端的固定值, 缺了 simplified flow 也不会触发            |

**少任何一个**, workspace/select 200 就会回到 `page=sign_in_with_chatgpt_codex_org`,
然后 `organization/select` 必然撞 `400 duplicate "Organization already has a default project."`
(因为 add_phone 阶段服务端已经替我们建好了 default project), 流程死掉。

---

## 3. workspace/select 三种返回 → 三条路径

```
                 POST /api/accounts/workspace/select
                          │
       ┌──────────────────┼──────────────────┐
       │                  │                  │
   ┌───▼───┐        ┌─────▼─────┐      ┌─────▼─────┐
   │ 3xx    │        │ 200       │      │ 400       │
   │ Location│        │ JSON      │      │ duplicate │
   └───┬───┘        └─────┬─────┘      └─────┬─────┘
       │                  │                  │
       │              page = ?                │
       │           ┌──────┼──────┐            │
       │           │      │      │            │
       │      external_url  codex_org(多org)  │
       │           │      │                   │
       │           │  follow ws_next         │
       │           │  (oauth2/auth?           │
       │           │   login_verifier=...)    │
       │           │                          │
       │      POST organization/select        │
       │      {"org_id":..., "project_id":...} │
       │       │       │                      │
       │       │   ┌───┴───┐                  │
       │       │   │       │                  │
       │       │  200/3xx  400 duplicate      │
       │       │   │       │                  │
       │       └───┴───────┴──────────────────┤
       │                                      │
       └──────────────────► 兜底: advance     │
                            (重新 GET         │
                             authorize_url)   │
                                              │
                            ──────────────────┘
                                  │
                            提取 code → /oauth/token
```

实现见 `_oauth_submit_workspace_and_org` (chatgpt_register.py)。
**核心规则: ws_next 是规范 oauth 链路时一定优先 follow,advance 只兜底。**

---

## 4. 历史踩坑 / 反例

### 4.1 `prompt=login` 的双刃剑

`/oauth/authorize` 必须带 `prompt=login` 才触发 simplified flow。
但 advance 回退路径(重新 GET authorize_url)如果还带 `prompt=login`,
服务端会**强制重走登录**,302 到 `/api/accounts/login?login_challenge=...` → 200 `/log-in`。

→ 修复策略: **不要让 advance 跑在 ws_next 之前**。ws_next (含 `login_verifier`) 是已认证态的产物,
   先用它拿 code; advance 只在 ws_next 不存在或失败时才跑。

### 4.2 `organization/select` 的 `duplicate` 不是参数错误

```json
HTTP 400
{
  "error": {
    "message": "Organization already has a default project.",
    "type": "invalid_request_error",
    "code": "duplicate"
  }
}
```

意思是 **"这一步我已经替你做过了"**(add_phone validate 阶段创建账户时服务端就建过 default project)。
**绝不要去掉 `project_id` 重试**——那只会换来:

```json
HTTP 400 { "error": { "message": "Missing required parameter: 'project_id'." } }
```

→ 正确做法: 把 duplicate 当幂等成功,继续 advance 推进。
   更彻底的做法: 别让 organization/select 跑——只在 `page=sign_in_with_chatgpt_codex_org`
   **且** `len(orgs) > 1` 时才发(单 org 单 default project 的新号一定撞 duplicate)。

### 4.3 follow_for_code 不能 GET localhost

303 的 Location 通常是 `http://localhost:1455/auth/callback?code=ac_xxx&...`。
如果客户端在代理后面(`http_proxy=http://192.168.x.x:7890`),GET localhost 会被代理转发,
**必然 30s 超时**。

→ 修复: 见到 Location 是 `http://localhost:*` / `https://localhost:*` 时:
   - 先用 `_extract_code_from_url` / regex `[?&]code=([^&\s'\"]+)` 抓 code
   - 抓不到也直接 return None,不要再 GET

### 4.4 oai-client-auth-session cookie 必须刷新

`workspace/select` 之前必须 GET 一次 `/sign-in-with-chatgpt/codex/consent`,
让服务端 set-cookie 把"add_phone 之后的真实 workspaces"刷进 jar。
否则解出的 `workspace_id` 是陈旧的(注册前的空 workspaces 列表),会得 400。

实现见 `_oauth_submit_workspace_and_org` 开头的 `session.get(consent_url, ...)`。

---

## 5. 关键代码位点 (chatgpt_register.py)

| 步骤                          | 函数 / 位置                                  |
|-------------------------------|---------------------------------------------|
| authorize_params 构造         | `perform_codex_oauth_login_http` 开头        |
| GET /oauth/authorize          | `_bootstrap_oauth_session`                   |
| POST /authorize/continue      | `_post_authorize_continue`                   |
| POST /password/verify         | 主流程内联 `/api/accounts/password/verify`    |
| email-otp 轮询 + validate     | `_poll_email_otp` / `_submit_email_otp`      |
| add_phone (send/validate)     | `_handle_add_phone`                          |
| workspace/select + advance    | `_oauth_submit_workspace_and_org`            |
| 跟 302/303 链                 | `_oauth_follow_for_code`                     |
| allow_redirects=True 兜底     | `_oauth_allow_redirect_extract_code`         |
| advance (重 GET authorize_url)| `_advance_via_authorize` (内嵌)              |
| code → token 交换             | 主流程末尾 `POST /oauth/token`                |
| oai-client-auth-session 解码  | `_decode_oauth_session_cookie`               |

---

## 6. 调试时该看什么日志

成功时长这样:

```
[OAuth] OTP 验证通过 page=sign_in_with_chatgpt_codex_consent next=.../codex/consent
[OAuth] 5/7 跟随 continue_url 提取 code
[OAuth] follow[1] 200 .../codex/consent
[OAuth] 6/7 执行 workspace/org 选择
[OAuth] 选用 workspace_id=<uuid>
[OAuth] workspace/select -> 200
[OAuth] workspace/select page=external_url next=.../api/oauth/oauth2/auth?...login_verifier=...
[OAuth] ws_next 是规范 oauth 链路, 优先 follow: ...
[OAuth] follow[1] 302 .../api/oauth/oauth2/auth?...
[OAuth] follow[1] -> Location=.../api/accounts/consent?consent_challenge=...
[OAuth] follow[2] 302 .../api/accounts/consent?...
[OAuth] follow[2] -> Location=.../api/oauth/oauth2/auth?consent_verifier=...
[OAuth] follow[3] 303 .../api/oauth/oauth2/auth?consent_verifier=...
[OAuth] follow[3] -> Location=http://localhost:1455/auth/callback?code=ac_...
[OAuth] 7/7 POST /oauth/token
[OAuth] /oauth/token -> 200
```

如果出现下面任一信号,对照第 4 节排查:

| 信号                                           | 对应坑                              |
|------------------------------------------------|-------------------------------------|
| `workspace/select page=sign_in_with_chatgpt_codex_org` | 第 4 节 4.1: 缺 simplified flow flag |
| `organization/select 错误: ... duplicate`      | 第 4 节 4.2: 别 retry, 走 advance   |
| `follow[N] 请求异常: curl: (28) Connection timed out` (~30s) | 第 4 节 4.3: localhost 被代理拦了    |
| `session 中没有 workspace 信息`                | 第 4 节 4.4: 没预热 consent URL      |
| `/oauth/token -> 4xx` 且 code 看起来正常       | code_verifier 与 code_challenge 不匹配 (PKCE 状态丢了) |

---

## 7. 抓 HAR 验证流程

如果服务端再次改动协议,首选验证手段是抓一份浏览器 HAR:

1. Chrome DevTools → Network → 勾 Preserve log → 清空
2. 真人在 Codex CLI 触发的 chat-gpt 登录页走完整流程到 `localhost:1455/auth/callback?code=`
3. 右键 → Save all as HAR with content
4. 用 `jq` 抽 auth 相关请求:

```bash
jq -r '.log.entries[] | select(.request.url|test(
    "auth.openai.com/api/accounts/(workspace/select|organization/select|consent|email-otp/validate|password/verify)"
    + "|auth.openai.com/api/oauth/oauth2/auth"
    + "|auth.openai.com/sign-in-with-chatgpt/codex/(consent|organization)"
    + "|localhost:1455/auth/callback")) |
    "\(.startedDateTime) \(.request.method) \(.response.status) \(.request.url[0:160])"' \
  path/to/file.har
```

参考 HAR (历史成功案例): 见之前在 chatgpt_register.py:2545+ 注释里提到的
`chromewebdata1.har` 的 idx=149 → idx=207 段 (password/verify → callback?code=).
