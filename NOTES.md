# chatgpt_register 项目笔记

## 一、整体流程

```
DuckMail 临时邮箱 / 用户指定邮箱 ──┐
                                   ├──▶ 注册 ChatGPT 账号 ──▶ OAuth 拿 Codex Token ──▶ 落盘 + 上传 CPA
随机姓名/生日/密码 ────────────────┘
```

### 阶段 0：准备

| 步 | 动作 | 接口 |
|---|---|---|
| 0.1 | 生成随机邮箱前缀 + 强密码 | 本地 |
| 0.2 | DuckMail 创建邮箱账户 | `POST api.duckmail.sbs/accounts` |
| 0.3 | 取邮件读取 token | `POST api.duckmail.sbs/token` |
| 0.4 | 随机 Chrome 指纹 (UA + sec-ch-ua + TLS impersonate) | 本地 |

> 指定邮箱模式跳过 0.2 / 0.3，OTP 走手动输入。

### 阶段 1：ChatGPT 注册（chatgpt.com → auth.openai.com）

| 步 | 动作 | 接口 | 备注 |
|---|---|---|---|
| 1 | 访问首页拿 cookies | `GET chatgpt.com/` | 获得 `oai-did` 等 |
| 2 | 取 CSRF token | `GET /api/auth/csrf` | |
| 3 | 提交 signin 拿 authorize URL | `POST /api/auth/signin/openai` | 带 csrf + email |
| 4 | 跟随 authorize | `GET auth.openai.com/api/accounts/authorize` | 服务端按 email 是否存在分流：<br>• 不存在 → `/create-account/password`（走 5）<br>• 已注册 → `/email-verification`<br>• 已完成 → 直接 `/callback` |
| 5 | **创建账号** | `POST /api/accounts/user/register` | ⚠️ 必须带 `openai-sentinel-token`（flow=`username_password_create`） |
| 6 | 触发发 OTP 邮件 | `GET /api/accounts/email-otp/send` | |
| 7 | 拉验证码 | DuckMail 自动 / 用户手动输入 | 正则 `\b(\d{6})\b` |
| 8 | 提交 OTP | `POST /api/accounts/email-otp/validate` | |
| 9 | 提交姓名/生日 | `POST /api/accounts/create_account` | |
| 10 | 跟随 callback 完成登录 | `GET continue_url` | |

### 阶段 2：Codex OAuth（拿 Token 才有 auth file）

走 **Authorization Code + PKCE**：

| 步 | 动作 | 接口 |
|---|---|---|
| 1 | 生成 PKCE | 本地 |
| 2 | 启动 OAuth | `GET /oauth/authorize` |
| 3 | 提交邮箱 | `POST /api/accounts/authorize/continue` ← sentinel `authorize_continue` |
| 4 | 提交密码 | `POST /api/accounts/password/verify` ← sentinel `password_verify` |
| 5 | 可能再要 OTP | `POST /api/accounts/email-otp/validate` |
| 6 | 自动选 workspace | `POST /api/accounts/workspace/select` |
| 7 | 自动选 organization | `POST /api/accounts/organization/select` |
| 8 | 跟随 302 抓回调 URL 里的 `?code=` | 内部跳转 |
| 9 | **换 token** | `POST /oauth/token` |

返回 `{access_token, refresh_token, id_token}`。

### 阶段 3：保存 Token

| 文件 | 内容 |
|---|---|
| `codex_tokens/<email>.json` | Codex CLI 用的完整凭证 |
| `registered_accounts.txt` | `email----密码----邮箱密码----oauth=ok/fail` |
| CPA 面板（可选） | `POST {UPLOAD_API_URL}` 上传 JSON |

---

## 二、关键反爬绕过

1. **TLS 指纹**：`curl_cffi` 的 `impersonate=chrome131/142...` 让 JA3 指纹和真 Chrome 一致
2. **Sentinel PoW**：`SentinelTokenGenerator` 用 FNV-1a 32 位哈希暴力寻 nonce，得到 `openai-sentinel-token`
3. **PKCE**：防止 code 被中间人窃取
4. **cookies 链**：全局 session 维护 `oai-did` / `login_session` / `oai-client-auth-session` / `cf_clearance`

---

## 三、当前问题：阶段 1 步 5 register 返回 400

### 报错

```
[POST] https://auth.openai.com/api/accounts/user/register
[Status] 400
[Response] {"error":{"message":"Failed to create account. Please try again.",
            "type":"invalid_request_error","param":null,"code":null}}
```

通用兜底文案，不告诉真实原因。

### 已做的尝试

- ✅ 加上了 `openai-sentinel-token` header（flow=`username_password_create`）
- ❓ 仍待验证是否能通过

### 与 GptCrate (`/home/dev/repos/public-repos/GptCrate`) 的对比

| 维度 | chatgpt_register.py | GptCrate |
|---|---|---|
| **Sentinel PoW** | 完整本地计算（FNV-1a 暴力 nonce + 19 字段 base64） | **完全不算 PoW**：`{"p":"","t":"","c":<server返回>,"id":did,"flow":...}` |
| **register 前预热** | ❌ 直接 POST `/user/register` | ✅ 先 POST `/authorize/continue` with `screen_hint=signup` 把 username 写入 session cookie |
| **register sentinel 的 flow** | `username_password_create`（与浏览器一致） | 复用 `authorize_continue` |
| **TLS impersonate** | `chrome120/123/.../142` 随机 | 固定 `safari` |

### 根因分析（按可能性）

#### ① Sentinel 的 base64 配置 schema 已过期（最可能）

浏览器抓包的真实 sentinel token 解码后是 **26 字段**，且：
- 第 0 字段是数字 `20`（脚本写的是 `"1920x1080"` 字符串）
- 第 2 字段是 `4294967296`（脚本写的是 `4294705152`）
- 多了 outerWidth、bluetooth、reactListening 等检测项

`SentinelTokenGenerator._get_config()` (chatgpt_register.py:255) 用的是旧版 19 字段 schema，服务端解码校验时失败。

#### ② 缺少 `/authorize/continue` 预热

GptCrate 在 `/user/register` 之前显式调用：
```python
POST /api/accounts/authorize/continue
body: {"username":{"value":"<email>","kind":"email"},"screen_hint":"signup"}
```

把 username 写入 `oai-client-auth-session` cookie。从抓包验证：浏览器的 register 请求 cookie 里 `oai-client-auth-session` 解码后包含 `"username":{"value":"...","kind":"email"}`，证明这一步是必须的。

新版 OpenAI 可能不再通过 `authorize` URL 的 `login_hint` 自动写入 session。

#### ③ DuckMail 域名被风控（次要）

`@duckmail.sbs` 是小众一次性邮箱，OpenAI 可能加入了 disposable email 黑名单。可用真实邮箱（如指定邮箱模式）排除。

---

## 四、修复方案优先级

| 优先级 | 改动 | 工作量 | 收益 |
|---|---|---|---|
| 🔥 高 | 加 `/authorize/continue` 预热 + sentinel(`authorize_continue`) | 小 | 大 |
| 🔥 高 | 绕过本地 PoW（仿 GptCrate 的 `p="" + c=server_token` 形式，仅当 `proofofwork.required=false` 时可用） | 小 | 大 |
| 🟡 中 | 更新 `_get_config()` 字段 schema 到 26 字段 | 大（需逆向当前 sentinel SDK） | 大 |
| 🟢 低 | TLS 改 `impersonate="safari"` 做对照 | 极小 | 小 |
| 🟢 低 | 替换 DuckMail 为更稳的临时邮箱（如 luckmail / hotmail） | 中 | 中 |

### 建议下一步

按 ① + ② 顺序合并改一次：

1. 在 `register()` 之前加 `_authorize_continue_signup(email)`，body=`{"username":{"value":email,"kind":"email"},"screen_hint":"signup"}`，header 带 sentinel(`authorize_continue`)
2. 修改 `build_sentinel_token`，加一个分支：当 `proofofwork.required` 为 false 时，直接返回 `{"p":"","t":"","c":<token>,"id":did,"flow":...}`，跳过 `_get_config` 计算

如果改完仍 400，再做 ③ 的 schema 升级。

---

## 五、文件结构

```
chatgpt_register/
├── chatgpt_register.py    兼容 wrapper
├── src/chatgpt_register/  主实现 package
├── config.json            兼容旧配置路径（也可用 var/config.json）
├── codex_tokens/          兼容旧 token 目录（也可用 var/codex_tokens/）
├── codex_tokens_bak/      历史备份
├── registered_accounts.txt  兼容旧注册结果摘要
└── var/                   新运行态默认目录
```

## 六、运行方式

```bash
# 默认 DuckMail 临时邮箱模式
python3 chatgpt_register.py
# 邮箱来源: [1] DuckMail 临时邮箱(默认)  [2] 指定自有邮箱: 1
# 注册账号数量: 10
# 并发数: 3

# 指定邮箱模式（OTP 手动输入，强制单账号）
python3 chatgpt_register.py
# 邮箱来源: ...: 2
# 请输入邮箱地址: foo@example.com
```
