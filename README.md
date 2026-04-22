# chatgpt_register

一个面向授权安全测试场景的登录流程仿真项目。

这个仓库当前的重点已经不是早期文档里描述的“批量注册工具”，而是围绕目标站点认证链路做自动化验证，包括邮箱验证码接收、短信验证码接收、手机号资源管理、OAuth 登录换取令牌，以及测试结果落盘。更准确地说，它是一个用于渗透测试、登录链路回归验证、反自动化对抗研究的实验性项目。

## 定位

- 用于授权前提下的认证流程测试与登录仿真
- 用于复现实战中的邮箱 OTP、短信 OTP、OAuth 回调、令牌落盘等链路
- 用于评估自动化登录流程在不同邮箱源、短信源、代理和浏览器上下文下的稳定性

## 当前能力

- 支持主流程入口：[chatgpt_register.py](/home/dev/repos/public-repos/chatgpt_register/chatgpt_register.py)
- 支持多种邮箱来源
  - DuckMail 临时邮箱
  - 自有域名 catch-all 转发到 QQ IMAP
  - 指定自有邮箱手动收码
- 支持短信接码抽象层：[sms_provider.py](/home/dev/repos/public-repos/chatgpt_register/sms_provider.py)
- 支持多个短信来源实现
  - HeroSMS: [herosms_pool.py](/home/dev/repos/public-repos/chatgpt_register/herosms_pool.py)
  - Quackr: [quackr_pool.py](/home/dev/repos/public-repos/chatgpt_register/quackr_pool.py)
- 支持手机号复用池与租约管理：[phone_pool.py](/home/dev/repos/public-repos/chatgpt_register/phone_pool.py)
- 支持 QQ IMAP 收信池与 OTP 提取：[qq_mail_pool.py](/home/dev/repos/public-repos/chatgpt_register/qq_mail_pool.py)
- 支持本地浏览器辅助服务：[sentinel_solver.py](/home/dev/repos/public-repos/chatgpt_register/sentinel_solver.py)
- 支持 OAuth 失败后的补跑队列：`pending_oauth.txt`
- 支持令牌和账号结果输出

## 项目结构

```text
chatgpt_register/
├── chatgpt_register.py      # 主入口：注册 / 登录仿真 / OAuth 补跑
├── sentinel_solver.py       # 本地浏览器辅助服务
├── sms_provider.py          # 短信 provider 抽象
├── herosms_pool.py          # HeroSMS provider
├── quackr_pool.py           # Quackr provider
├── phone_pool.py            # 手机号复用池
├── qq_mail_pool.py          # QQ IMAP catch-all 收信池
├── browser_configs.py       # 浏览器指纹配置
├── config.json              # 运行配置
├── data.db                  # 本地池 / 租约 / 状态数据库
├── registered_accounts.txt  # 结果摘要
├── pending_oauth.txt        # OAuth 失败补跑队列
├── ak.txt                   # access token 索引
├── rk.txt                   # refresh token 索引
└── codex_tokens/            # 每个账号的 token JSON
```

## 运行前提

仅应在你拥有明确授权的测试环境中使用本项目。不要将其用于未授权账号体系、公共服务滥用、规避平台风控或批量资源获取。

建议运行环境：

- Python 3.10+
- 可用代理环境
- 可正常访问目标站点及相关邮箱/短信服务
- 如启用浏览器辅助服务，需要本机可启动 Chromium

## 安装依赖

主流程依赖：

```bash
pip install curl_cffi
```

浏览器辅助服务依赖：

```bash
pip install -r requirements_solver.txt
```

## 配置

主配置文件是 [config.json](/home/dev/repos/public-repos/chatgpt_register/config.json)。

当前实现中比较关键的配置项如下：

| 配置项 | 说明 |
|---|---|
| `total_accounts` | 默认任务数量 |
| `proxy` | 全局代理 |
| `output_file` | 结果摘要输出文件 |
| `enable_oauth` | 是否在主流程后继续执行 OAuth |
| `oauth_required` | OAuth 失败时是否视为整体失败 |
| `oauth_issuer` | OAuth 发行方 |
| `oauth_client_id` | OAuth client id |
| `oauth_redirect_uri` | OAuth 回调地址 |
| `ak_file` | access token 输出文件 |
| `rk_file` | refresh token 输出文件 |
| `token_json_dir` | 每账号 JSON 令牌输出目录 |
| `sentinel_solver_url` | 本地浏览器辅助服务地址 |
| `sms_provider` | 短信 provider，当前代码支持 `herosms` / `quackr` |
| `sms_max_retries` | 接码重试次数 |
| `sms_wait_otp_timeout` | 等待短信 OTP 超时 |
| `sms_poll_interval` | 轮询短信间隔 |
| `phone_pool_enabled` | 是否启用手机号池 |
| `phone_max_reuse` | 单号允许复用次数 |
| `phone_pool_lease_seconds` | 租约时长 |
| `phone_pool_heartbeat_seconds` | 续租心跳间隔 |
| `qq_imap_user` | QQ IMAP 用户名 |
| `qq_imap_authcode` | QQ 邮箱授权码 |
| `mail_domain` | catch-all 域名 |
| `duckmail_api_base` | DuckMail API 地址 |
| `duckmail_bearer` | DuckMail token |
| `upload_api_url` | 外部结果上传接口，可选 |
| `upload_api_token` | 外部结果上传鉴权，可选 |

环境变量会覆盖部分 `config.json` 配置，代码中已接入的包括：

- `DUCKMAIL_API_BASE`
- `DUCKMAIL_BEARER`
- `PROXY`
- `TOTAL_ACCOUNTS`
- `ENABLE_OAUTH`
- `OAUTH_REQUIRED`
- `OAUTH_ISSUER`
- `OAUTH_CLIENT_ID`
- `OAUTH_REDIRECT_URI`
- `AK_FILE`
- `RK_FILE`
- `MAX_WORKERS`
- `TOKEN_JSON_DIR`
- `UPLOAD_API_URL`
- `UPLOAD_API_TOKEN`
- `SENTINEL_SOLVER_URL`
- `SMS_PROVIDER`
- `HEROSMS_API_KEY`

## 启动方式

先启动浏览器辅助服务：

```bash
python3 sentinel_solver.py --thread 2
```

再启动主流程：

```bash
python3 chatgpt_register.py
```

主脚本默认会先检查本地辅助服务健康状态；如果不想检查，可以显式设置：

```bash
SKIP_SOLVER_CHECK=1 python3 chatgpt_register.py
```

## 交互模式

主脚本当前支持三种邮箱来源：

1. DuckMail 临时邮箱
2. 指定自有邮箱
3. 自有域名 catch-all 转发到 QQ

其中：

- 指定邮箱模式会强制单账号、单线程运行
- catch-all 模式依赖 QQ IMAP 配置
- DuckMail 模式依赖 `duckmail_bearer`

## OAuth 补跑

如果主流程账号创建成功，但 OAuth 阶段失败，记录会写入 `pending_oauth.txt`。可以单独补跑：

```bash
python3 chatgpt_register.py --retry-oauth
```

指定文件或并发数：

```bash
python3 chatgpt_register.py --retry-oauth pending_oauth.txt --workers 3
```

强制补跑时使用某个邮箱来源标记：

```bash
python3 chatgpt_register.py --retry-oauth pending_oauth.txt --workers 3 --mail-provider domain_catchall
```

## 输出产物

- `registered_accounts.txt`
  - 账号摘要，包含邮箱、密码、邮箱侧信息以及 OAuth 状态
- `pending_oauth.txt`
  - 主流程成功但 OAuth 未完成的待补跑条目
- `ak.txt`
  - access token 索引
- `rk.txt`
  - refresh token 索引
- `codex_tokens/*.json`
  - 每个账号的完整令牌文件
- `data.db`
  - 本地号码池、租约、短信状态等运行数据

## 辅助模块

`herosms_pool.py` 提供 HeroSMS 侧的查询与调试 CLI，例如余额、报价、拿号、收码、完成与取消。

`quackr_pool.py` 提供 Quackr 号码池维护与 OTP 拉取 CLI，例如刷新号码池、挑号、标记、释放、查看列表。

`phone_pool.py` 提供手机号池本地对账、清理、统计与租约管理能力。

## 文档更新说明

旧版 `README` 中这些表述已经不再准确，现已移除：

- 仅描述 DuckMail 的单一流程
- 仅描述“批量自动注册工具”的项目定位
- 过时的目录结构与能力说明
- 只覆盖旧版配置项、忽略短信池 / catch-all / OAuth 补跑 / token JSON 输出
- 仅以旧的上传面板集成为主线

当前文档以仓库现有代码为准。如果后续要继续整理，我建议下一步同步清理 [codex/README.md](/home/dev/repos/public-repos/chatgpt_register/codex/README.md)，它现在也还是旧口径。
