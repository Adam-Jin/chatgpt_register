# chatgpt_register

一个面向授权安全测试场景的登录流程仿真项目。

这个仓库当前的重点已经不是早期文档里描述的“批量注册工具”，而是围绕目标站点认证链路做自动化验证，包括邮箱验证码接收、短信验证码接收、手机号资源管理、OAuth 登录换取令牌，以及测试结果落盘。更准确地说，它是一个用于渗透测试、登录链路回归验证、反自动化对抗研究的实验性项目。

## 定位

- 用于授权前提下的认证流程测试与登录仿真
- 用于复现实战中的邮箱 OTP、短信 OTP、OAuth 回调、令牌落盘等链路
- 用于评估自动化登录流程在不同邮箱源、短信源、代理和浏览器上下文下的稳定性

## 当前能力

- 支持主流程入口：[chatgpt_register.py](/home/dev/repos/public-repos/chatgpt_register/chatgpt_register.py)（兼容 wrapper，实际实现见 [register.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/register.py)）
- 支持多种邮箱来源
  - DuckMail 临时邮箱
  - 自有域名 catch-all 转发到 QQ IMAP
  - 指定自有邮箱手动收码
- 支持短信接码抽象层：[sms_provider.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/sms_provider.py)
- 支持多个短信来源实现
  - HeroSMS: [herosms_pool.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/herosms_pool.py)
  - Quackr: [quackr_pool.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/quackr_pool.py)
- 支持手机号复用池与租约管理：[phone_pool.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/phone_pool.py)
- 支持 IMAP 收信池与 OTP 提取（兼容 QQ 和其它标准 IMAP SSL 邮箱）：[qq_mail_pool.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/qq_mail_pool.py)
- 支持本地浏览器辅助服务：[sentinel_solver.py](/home/dev/repos/public-repos/chatgpt_register/src/chatgpt_register/sentinel_solver.py)
- 支持 OAuth 失败后的补跑队列：`pending_oauth.txt`
- 支持令牌和账号结果输出

## 项目结构

```text
chatgpt_register/
├── chatgpt_register.py      # 主入口：注册 / 登录仿真 / OAuth 补跑
├── pyproject.toml           # Python package 元数据
├── src/
│   └── chatgpt_register/
│       ├── register.py      # 主流程实现
│       ├── paths.py         # config / data / output 路径解析
│       ├── sms_provider.py  # 短信 provider 抽象
│       ├── phone_pool.py    # 手机号复用池实现
│       ├── sentinel_solver.py
│       ├── monitor/
│       └── codex/
├── tests/
├── docs/
├── config.json              # 兼容旧路径；可迁移到 var/
└── var/                     # 新运行态目录（默认写入位置）
```

## 运行前提

仅应在你拥有明确授权的测试环境中使用本项目。不要将其用于未授权账号体系、公共服务滥用、规避平台风控或批量资源获取。

建议运行环境：

- Python 3.10+
- 可用代理环境
- 可正常访问目标站点及相关邮箱/短信服务
- 如启用浏览器辅助服务，需要本机可启动 Chromium

## 安装依赖

项目依赖统一由 `pyproject.toml` 管理：

```bash
pip install -e .
```

开发时推荐使用 editable install：

```bash
python -m unittest discover -s tests
```

## 配置

主配置文件默认仍兼容读取仓库根目录的 [config.json](/home/dev/repos/public-repos/chatgpt_register/config.json)。如果设置 `CHATGPT_REGISTER_CONFIG`，会优先使用该文件；如果设置 `CHATGPT_REGISTER_DATA_DIR`，相对运行产物会解析到这个目录。

没有旧根目录文件时，新运行态默认写入 `var/`：

- `var/config.json`
- `var/data.db`
- `var/registered_accounts.txt`
- `var/pending_oauth.txt`
- `var/codex_tokens/`

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
| `default_email_source` | 默认注册来源，对旧的 `domain_catchall` 兼容入口也会映射到这里 |
| `email_sources` | 注册来源列表；决定“注册用哪个邮箱地址”以及“OTP 默认走哪个收件箱” |
| `imap_profiles` | IMAP 收件箱列表；只描述收件箱连接参数，不直接作为注册来源展示 |
| `mail_imap_host` | IMAP 服务器地址，默认兼容 `qq_imap_host` |
| `mail_imap_port` | IMAP 端口，默认兼容 `qq_imap_port` |
| `mail_imap_user` | IMAP 用户名，默认兼容 `qq_imap_user` |
| `mail_imap_password` | IMAP 密码或授权码，默认兼容 `qq_imap_authcode` |
| `mail_imap_folder` | 监听的 IMAP 文件夹，默认兼容 `qq_imap_folder` |
| `qq_imap_user` | 旧版配置键，兼容保留 |
| `qq_imap_authcode` | 旧版配置键，兼容保留 |
| `mail_domain` | 旧版 catch-all 域名配置，兼容保留 |
| `duckmail_api_base` | DuckMail API 地址 |
| `duckmail_bearer` | DuckMail token |
| `upload_api_url` | 外部结果上传接口，可选 |
| `upload_api_token` | 外部结果上传鉴权，可选 |

推荐把邮箱配置拆成两层：

- `imap_profiles`
  - 只负责“怎么收件”，例如 `QQ`、`2925`、其它 IMAP 收件箱
- `email_sources`
  - 只负责“注册时邮箱地址怎么来”
  - `forward_domain`: 例如 `*@pandalabs.asia -> qq-main`
  - `imap_mailbox`: 默认直接使用真实邮箱；也可配 `address_mode: suffix_alias`，基于主账号派生类似 `2925` 的子邮箱

交互启动时会展示 `DuckMail / 指定单个邮箱 / email_sources`，不再直接把 IMAP profile 当成“邮箱来源”。

`forward_domain` / `imap_mailbox` 当前都只会 `SELECT` 并监听一个 IMAP 文件夹，也就是对应 profile 的 `folder`（或旧键 `qq_imap_folder` / `mail_imap_folder`）指定的那个文件夹；不会同时扫描垃圾箱、广告邮件、其它文件夹。

命令行仍支持旧的 `--mail-provider domain_catchall`、`--mail-provider imap:<profile_key>` 兼容入口，内部会自动映射到新的 `email_sources`。新的推荐写法是 `--mail-provider source:<source_key>`。

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
sentinel-solver --thread 2
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

主脚本当前支持四类注册来源：

1. DuckMail 临时邮箱
2. 指定自有邮箱
3. `forward_domain`，例如 `*@pandalabs.asia -> Primary Inbox`
4. `imap_mailbox`，例如直接使用 `2925` 邮箱本身注册，或启用 `suffix_alias` 后派生 `2925` 子邮箱

其中：

- 指定邮箱模式会强制单账号、单线程运行
- 固定地址的 `imap_mailbox` 会强制单账号、单线程运行
- `imap_mailbox + address_mode: suffix_alias` 可以像 `forward_domain` 一样批量生成不同地址
- `forward_domain` / `imap_mailbox` 都依赖 `imap_profiles + email_sources`
- DuckMail 模式依赖 `duckmail_bearer`

## OAuth 补跑

如果主流程账号创建成功，但 OAuth 阶段失败，记录会写入 `pending_oauth.txt`。可以单独补跑：

```bash
python3 chatgpt_register.py --retry-oauth
```

直接执行时默认并发为 `1`。如果是在交互终端里运行且未显式传 `--workers`，程序会先提示一次并发数。

指定文件或并发数：

```bash
python3 chatgpt_register.py --retry-oauth pending_oauth.txt --workers 3
```

强制补跑时使用某个邮箱来源标记：

```bash
python3 chatgpt_register.py --retry-oauth pending_oauth.txt --workers 3 --mail-provider source:pandalabs
```

## 输出产物

- `registered_accounts.txt` 或 `var/registered_accounts.txt`
  - 账号摘要，包含邮箱、密码、邮箱侧信息以及 OAuth 状态
- `pending_oauth.txt` 或 `var/pending_oauth.txt`
  - 主流程成功但 OAuth 未完成的待补跑条目
- `codex_tokens/*.json` 或 `var/codex_tokens/*.json`
  - 每个账号的完整令牌文件
- `data.db` 或 `var/data.db`
  - 本地号码池、租约、短信状态等运行数据

已有根目录运行产物会继续被优先使用，避免升级后读不到旧数据。要强制使用新目录，可以设置 `CHATGPT_REGISTER_DATA_DIR=/path/to/state`。

## 辅助模块

`src/chatgpt_register/herosms_pool.py` 提供 HeroSMS 侧的查询与调试 CLI，例如余额、报价、拿号、收码、完成与取消。

`src/chatgpt_register/quackr_pool.py` 提供 Quackr 号码池维护与 OTP 拉取 CLI，例如刷新号码池、挑号、标记、释放、查看列表。

`phone-pool` 提供手机号池本地对账、清理、统计与租约管理能力，实际实现位于 `src/chatgpt_register/phone_pool.py`。

## 文档更新说明

旧版 `README` 中这些表述已经不再准确，现已移除：

- 仅描述 DuckMail 的单一流程
- 仅描述“批量自动注册工具”的项目定位
- 过时的目录结构与能力说明
- 只覆盖旧版配置项、忽略短信池 / catch-all / OAuth 补跑 / token JSON 输出
- 仅以旧的上传面板集成为主线

当前文档以仓库现有代码为准。如果后续要继续整理，我建议下一步同步清理 [codex/README.md](/home/dev/repos/public-repos/chatgpt_register/codex/README.md)，它现在也还是旧口径。
