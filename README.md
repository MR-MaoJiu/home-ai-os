# Home AI OS

Home AI OS 是运行在家庭服务器上的私人 AI 系统，提供原生 iOS 客户端与网页管理后台。会话、资料、记忆、任务和自动化由家庭服务器持有；本地使用不需要平台注册或付费。

项目采用**服务端编排、客户端交互**的架构：iOS 发送用户消息并展示进度、回答和必要确认，服务端负责多轮上下文、模型选择、工具调用、隐私检查和结果保存。联网搜索由 AI 在对话中按需执行，客户端不选择搜索工具或拼装 Agent 工作流。

当前为持续开发版本；真实接入和验收范围见 [验收记录](docs/验收记录.md)，不将未具备环境的功能标记为完整生产交付。

## 产品组成

| 组成 | 职责 |
|---|---|
| iOS | 对话与历史、语音输入、自动化、个人数据与共享、系统权限和扫码配对 |
| 家庭管理后台 | 成员与设备、模型和 Provider、本人及已共享数据、记忆、任务、自动化、远程连接与运维 |
| Core API | 身份认证、会话接口、数据权限、同步、任务提交、审批与审计 |
| Core Worker / Agent | 持久化执行、多轮规划、工具调用、重试、预算、取消及自动化触发 |
| Memory Worker | 从规范记忆账本生成向量和可重建的派生索引 |
| Provider | 模型、搜索、文档、语音、家居、邮件与受控 MCP 的能力实现 |
| Home AI Connect | 独立闭源协调平台，提供远程开通、设备授权和连接协商，不转发家庭业务正文 |

iOS 最低支持 iOS 18，保留**对话、记忆、自动化、数据、设置**五个主页面。没有独立的“活动”Tab，也没有对话右上角的独立搜索入口。

## 系统架构

```mermaid
flowchart TB
    IOS[iOS：输入与展示] -->|会话和用户操作| API[Core API]
    WEB[家庭管理后台] -->|同源登录与 CSRF| API
    API --> AUTH[身份、权限、策略与审计]
    API --> CHAT[会话服务]
    CHAT --> DB[(PostgreSQL / 加密会话与规范账本)]
    CHAT --> TASK[持久化任务]
    WORKER[Core Worker] --> TASK
    TASK --> AGENT[服务端 Agent：上下文与多轮规划]
    AGENT --> POLICY[权限、隐私与必要确认]
    POLICY --> CAP[能力注册与 Provider]
    CAP --> MODEL[本地模型 / 可配置云模型]
    CAP --> TOOLS[搜索、资料、记忆、语音、家居、邮件]
    TOOLS -->|真实工具结果| AGENT
    AGENT -->|最终回答与来源| DB
    DB -->|历史、进度与结果| API
    DB --> OUTBOX[事务 Outbox / NATS JetStream]
    OUTBOX --> MEMORY[记忆与事件消费者]
```

服务端是 Python 模块化单体，API、任务 Worker 和记忆 Worker 独立运行。PostgreSQL 保存规范数据，pgvector、Mem0 与 Graphiti 提供检索或派生索引，不能取代规范账本成为唯一数据副本。Provider 不持有 Core 数据库凭据。

## 对话与业务流程

### 1. 配对和打开应用

家庭管理员在“成员与设备”创建成员并生成五分钟有效的二维码。iOS 扫码后自动取得地址与稳定服务器身份：未启用远程时使用家庭 HTTPS 地址，已启用远程时可在外网首次配对。

打开应用后，客户端从服务端读取该成员的会话列表和历史，恢复上次选择的会话。聊天记录不依赖页面内存；关闭页面或重启应用不会删除服务端历史。不同成员的会话相互隔离。

### 2. 发送消息和多轮上下文

1. iOS 将消息和发送标识提交到会话接口，不提交模型角色、工具名称或执行计划。
2. Core 校验身份与会话所有权，把消息和任务在同一事务中保存。同一发送标识重复提交不会重复创建任务。
3. Agent 从服务端读取近期历史，重新校验旧回答引用资料的权限，再组织本轮上下文。
4. 服务端调用模型，由模型提出结构化工具建议；核心重新检查契约、权限、隐私和预算后执行。
5. 工具结果回到 Agent，必要时继续调用工具，最终将回答与真实来源保存到服务端。
6. iOS 读取进度和结果；断线期间服务端任务继续，重连后读取原任务，不重放有副作用的操作。

完整历史保存在服务器，但模型上下文有长度和轮次预算，并非无限上下文。已撤回授权或已删除来源的旧答案会被隐藏，不继续送入模型。旧版本没有会话标识的单轮任务，迁移为独立历史对话，不虚构它们之间的多轮关系。

### 3. 自动搜索与操作确认

公开联网搜索在对话中按需自动执行，通过自托管 SearXNG 获取结果，AI 整合后提供真实来源链接。搜索不再单独要求审批；查询仍经过隐私检查，不能借自动搜索将秘密或未获授权的私人来源发送出去。

邮件发送、受控设备操作等需要确认的行为，会在对话中显示具体参数。用户确认或拒绝后，由服务端继续处理。确认绑定执行身份、参数摘要和有效期，客户端不能把任意操作伪装成已批准。禁止的高风险能力不会因用户确认而自动开放。

语音录制和系统权限由 iOS 处理；音频交给服务器转写。快捷提醒通过语义输入接口交给服务器创建任务和会话记录，不在客户端编排工具。

### 4. 资料、成员共享与记忆

日历、提醒、联系人、睡眠、照片和位置等数据，由用户在 iOS 授权后按需上传。文件解析、索引和后续处理在服务端执行。

资料归属于手机配对的成员。管理后台只显示当前账号的数据和成员明确共享给它的数据；家庭管理员不会自动获得成年成员的私人内容。图片、文件等资料在数据详情逐条共享和撤回。健康、位置在 iOS 设置中按类型和接收成员开启持续共享：服务端同时授权已有资料和后续上传、更新的资料，关闭时撤回该类型的全部授权。此设置不扩大 iOS 系统采集权限，不代表后台持续定位；秘密资料不能共享。

原始资料不会自动成为已确认记忆。用户基于自己的来源资料提交候选，确认后写入记忆账本。iOS 的“记忆”Tab 展示当前成员已确认记忆及待确认候选，“数据”Tab 不混入记忆；家庭后台也将数据和记忆拆成独立页面。共享健康或位置资料不会自动共享从中提取的记忆。两者都在家庭服务器持久保存，使用不同业务类型与处理流程；管理端仅能按已有授权筛选成员。向量、Mem0 和 Graphiti 索引可重建，停用 Provider 不删除规范记忆。

### 5. 任务与自动化

任务是一次执行的持久记录，例如回答消息、解析文件或创建提醒。对话内展示相关进度和必要确认；管理端“我的任务与审批”用于核查当前账号的执行情况，并非全家私人任务的总览。

自动化是“什么时候、做什么”的规则。定时或数据事件触发后，服务器创建任务，再经过同一套策略与执行流程。自动化页面管理当前成员的规则；停用规则不等于取消已经生成的任务。

Provider 显示服务可达，只说明健康检查通过。实际执行还取决于启用状态、能力契约、账户权限、模型或凭据配置，以及隐私策略。

## 远程连接

```mermaid
flowchart LR
    PHONE[iOS] -. 授权与签名信令 .-> CONNECT[Home AI Connect / 兼容平台]
    HOME[家庭服务器] -. 授权与签名信令 .-> CONNECT
    PHONE <-->|加密 P2P 数据通道| HOME
```

家庭端主动连接协调平台，不要求路由器映射家庭服务端口。客户端与家庭端通过 WebRTC 数据通道直接传输业务数据，稳定公钥签名绑定协商描述和 DTLS 身份；每个请求仍经过设备认证与数据权限检查。无法直连时明确失败，不回退 frp、TURN 或业务 HTTP 中继。

家庭后台默认使用 `https://homeai-connect.pintheworld.cn`，也可使用兼容协调协议的平台。读取套餐与客服信息后生成申请码，客服确认开通，家庭端自动领取并保存连接配置，无需来回复制绑定码。

开通后自动采用平台下发的 STUN，无需选择配置来源。家庭端可以选填自定义地址；平台可以追加备用节点。STUN 只用于发现网络地址，不承载家庭业务正文。内置服务正常与公网可达是不同状态，以实际探测结果为准。

有效直连不会因空闲或满五分钟而无条件关闭；单次请求仍有超时，设备撤销和授权失效继续生效。iOS 被系统挂起或网络变化时可能需要重新打洞，不承诺后台永久在线。

## 安全与数据边界

- 用户、设备、服务与 Provider 分开认证；应用权限与 PostgreSQL RLS 双重隔离。
- 浏览器使用 Secure、HttpOnly、SameSite Cookie 与 CSRF 校验；iOS 使用设备签名和 Keychain。
- 会话、资料、凭据与记忆内容在家庭端加密保存；密钥不进入模型上下文或普通日志。
- 外部网页、模型输出和工具结果均不可信，不能改变权限或批准操作。
- 云模型调用经过隐私网关，不能在本地模型失败后自动把私人内容转发到云端。
- 写入与 Outbox 同事务，消费者去重；外部副作用结果不明时保留待核对状态。
- 删除、撤权及恢复备份后的删除记录重放继续作用于旧答案和检索结果。

## 开发与部署

需要 Python 3.12+、Docker、Node.js；iOS 另需 Xcode 与签名配置。以下为开发环境启动方式。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python scripts/dev_env.py
docker compose --env-file .env.local -f deploy/compose.dev.yml up -d
.venv/bin/python scripts/migrate.py --runtime
.venv/bin/homeai init-key
.venv/bin/python scripts/tls.py
npm --prefix admin-web ci
npm --prefix admin-web run build
```

`init-key` 只在首次安装执行，保留已有主密钥与 `.env.local`。从旧版本升级时先备份，再运行数据库迁移及历史导入：

```sh
.venv/bin/python scripts/migrate.py --runtime
.venv/bin/python scripts/migrate_chat_history.py
```

分别启动 API 和后台服务：

```sh
.venv/bin/uvicorn homeai.api:create_app --factory \
  --host 0.0.0.0 --port 58443 \
  --ssl-keyfile state/tls/server.key --ssl-certfile state/tls/server.crt
.venv/bin/python -m homeai.worker
.venv/bin/python -m homeai.memory_worker
# 使用家居事件订阅时另行运行
.venv/bin/python -m homeai.home_observer
```

首次初始化管理员：

```sh
.venv/bin/homeai bootstrap --name 家庭管理员
.venv/bin/homeai web-setup --user <用户ID>
```

访问 `https://<家庭服务器地址>:58443/admin/`，配置家庭地址、模型和成员后生成二维码。开发证书默认仅适用于本机；部署时配置实际入口与证书，不关闭证书验证。打开 `ios/HomeAI.xcodeproj` 设置签名后运行。

模型权重不随仓库分发。Provider 启动与注册脚本位于 `scripts/`，固定版本配置位于 `providers/manifests/`。加密备份、主密钥和恢复演练需按实际部署环境配置。

## 工程目录

| 目录 | 内容 |
|---|---|
| `server/homeai/` | 会话、身份、账本、Agent、任务、工具与运维模块 |
| `server/alembic/` | 数据库迁移与 RLS 策略 |
| `ios/HomeAI/` | SwiftUI 交互、会话展示、设备权限和传输层 |
| `admin-web/` | React 家庭管理后台 |
| `providers/` | 独立 Provider 适配及固定版本清单 |
| `contracts/` | OpenAPI 与公共 JSON Schema |
| `deploy/`、`scripts/` | 部署、启动、迁移、备份与验证工具 |
| `server/tests/`、`ios/HomeAITests/` | 权限、协议、工作流与客户端测试 |

Home AI Connect 在独立闭源项目维护，不放入此仓库。

## 许可证

采用 `LicenseRef-Home-AI-OS-Attribution-1.0`，允许使用、修改、商用、闭源再分发和自行托管。对外发布的衍生产品或托管服务须保留“基于 Home AI OS”及指向本项目的可点击链接。详见 [LICENSE](LICENSE) 和 [第三方许可](THIRD_PARTY_NOTICES.md)；不宣称是标准 MIT 或已通过 OSI 认证。


## 新电脑启用内置服务

Provider 数据库为空表示尚未登记服务，不需要复制其他电脑的数据库或凭据。打开“模型与凭据”即可看到内置目录：

- **联网搜索**：SearXNG，固定容器镜像。需要安装并启动 Docker。
- **本地模型**：Qwen3 0.6B Q8（下载约 0.64 GB）或 Qwen3 4B Q4（约 2.50 GB）。实际运行还需要额外内存；0.6B 适合轻量验证，复杂工具规划优先选 4B。先按 [llama.cpp 官方指南](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md) 安装 `llama-server` 并加入 PATH。模型来自 [Qwen 官方 GGUF 仓库](https://huggingface.co/Qwen/Qwen3-4B-GGUF)，下载固定提交的文件并校验 SHA256。

除了 API、任务 Worker，在项目根目录启动内置部署器：

```bash
PYTHONPATH=server .venv/bin/python scripts/run_builtin_services.py
```

然后在“模型与凭据”选择服务与模型大小，点击“启用／应用选择”。部署器负责下载和启动；界面显示等待部署、启动中、可用或具体失败原因。只有实际健康检查通过才登记为可用。部署器未运行时会明确提示，申请不会伪装成已经启动。重启机器后需要将此进程与其他服务一起启动；首次下载需要访问模型仓库和容器仓库。停用会禁止 Core 调用；SearXNG 容器可继续存在供本机使用，若不再需要可在服务器执行 `docker compose -f deploy/compose.searxng.yml stop`。

内置部署器是可信本机运维进程，只接受固定目录选项，不接受网页提交的 Shell 或任意下载 URL。API 无需获得 Docker Socket。当前生产隔离尚未验收，生产模式仍拒绝通过开发部署器启用。

## MCP 与 Skill

“模型与凭据”提供 MCP 地址和可选访问令牌，连接后读取真实工具目录。管理员核对参数契约，明确映射到 Core 能力，再保存并启用 Provider。目录指纹变化会停止调用，必须重新核对；不能把任意工具名称伪装成已有能力。现阶段支持 Streamable HTTP；stdio 服务使用 `scripts/run_mcp_stdio.py` 的固定配置桥接器，其对外协议为 Core HTTP Provider，不是 Streamable HTTP；不能把桥接地址直接填入 MCP 表单。当前网页 MCP 表单用于 Streamable HTTP 接入。工具仍由服务端 Agent 调度，并保留审批、审计和出站策略。

Skill 支持导入带 `name`、`description` YAML 头部的 `SKILL.md`。保存后默认停用；启用后作为本家庭 Agent 的处理指引，不赋予额外工具权限。当前入口执行指令型 Skill，不运行附带脚本或任意安装命令。确定性、多步骤及定时任务继续使用“自动化”的声明式工作流。Skill 可停用、删除，删除不会移除家庭资料或记忆。

## 部署凭据与动态码登录

以下三类凭据用途不同，不能互换：

| 凭据 | 来源 | 用途 |
|---|---|---|
| 数据加密主密钥 | `init-key` 生成于服务器 | 解密家庭资料；不得粘贴到网页登录或 iOS 验证器 |
| 初始化凭据 | 本机 `web-setup` / `web-recover` 输出 | 五分钟内、一次性初始化或恢复网页账号 |
| TOTP 动态码密钥 | 管理网页初始化成功后一次性展示 | 导入 iOS 验证器，离线生成每 30 秒更新的六位动态码 |

首次安装依次执行已有环境与数据库初始化步骤，再在项目根目录：

```bash
PYTHONPATH=server .venv/bin/python -m homeai.cli init-key
PYTHONPATH=server .venv/bin/python -m homeai.cli bootstrap --name 家庭管理员
# 将上一步输出的 user_id 填入以下命令，勿填写 pairing_token。
PYTHONPATH=server .venv/bin/python -m homeai.cli web-setup --user <user_id>
```

已有安装不要重复执行 `init-key` 或 `bootstrap`。`web-setup` 输出中的 `setup_ticket` 是此命令生成的网页初始化凭据，有效期 300 秒：打开家庭 HTTPS `/admin/`，选择“首次部署”，填写此凭据、用户名和备用密码。页面随后展示 TOTP 密钥，将其导入 iOS“设置 → 管理端动态码”，或标准验证器，并填入生成的动态码完成绑定。完成绑定前不能访问管理功能。凭据过期时重新运行 `web-setup`，不要删除数据库。

日常管理登录输入**用户名＋当前动态码**即可；已有客户端若同时提交密码，服务端仍校验该密码。敏感配置变更需要重新输入一个未使用的动态码，验证后有效 5 分钟。同一动态码不能重复使用；刚登录后再次验证时等待下一周期。连续错误会限流。iOS 在本机 Keychain 保存 TOTP 密钥，显示验证码前验证设备身份，离开前台隐藏；生成动态码不要求与家庭服务器连接。

丢失验证器时，只能由持有服务器本机访问权的人执行：

```bash
PYTHONPATH=server .venv/bin/python -m homeai.cli web-recover --user <user_id>
```

用新的一次性凭据完成初始化流程，重新绑定 TOTP；旧动态码密钥和旧网页会话随恢复失效。不要将初始化凭据、TOTP 密钥、主密钥、模型 API Key 或 `.env.local` 提交到 Git。
