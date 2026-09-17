# Home AI OS

[项目主页](https://github.com/MR-MaoJiu/home-ai-os) · [参与开发](CONTRIBUTING.md) · [许可证](LICENSE)

以家庭服务器为中心的私人 AI 系统。项目已建立可运行开发实现，但**尚未达到完整 V1 或家庭生产可用的验收标准**。功能状态见 [验收记录](docs/验收记录.md)，不得将 Provider 配置文件视为真实接入成功。

## 当前可运行闭环

- 一次性配对 → P-256 设备签名 → 短期会话与刷新轮换。
- 用户数据同步 → 加密持久化 → 版本冲突 → 共享/撤回 → 删除墓碑。
- 记忆候选 → 用户确认 → 规范记录 → 授权范围内检索。
- 持久化任务 → OPA → 本地 llama.cpp → 受控工具提议 → 执行/审批/失败状态。
- Outbox → 真实 NATS JetStream 发布。
- SwiftUI 五页客户端及日历、提醒、联系人、睡眠、选定照片、文件导入入口。

## 项目边界与完成状态

本仓库提供家庭服务端、iOS 客户端、网页管理后台和独立 Provider 适配器。Home AI Connect 的账号、套餐、订单、运营后台和中继计量属于独立闭源项目，不在本仓库；家庭私人数据不上传到平台数据库。

“源码已实现”“真实服务集成通过”“iPhone 真机通过”“家庭部署通过”是四个不同阶段。下面列出的待办是实际缺口，不会通过返回固定成功值或示例数据掩盖。

| 模块 | 已实现 | 仍未完成或待验收 |
|---|---|---|
| 身份与管理后台 | 本机初始化、设备签名、短期令牌、撤销；网页密码/TOTP、CSRF、重新认证 | 完整安装向导体验、家庭多成员实用验收 |
| 数据 | 信封加密、版本冲突、共享/撤权、删除闭包、墓碑 | 分页快照一致性、后台增量同步、批次确认 |
| 记忆 | 候选确认、规范账本、pgvector、版本检查、自动与手动重建 | 冲突事实裁决、Mem0/Graphiti 真实集成及故障演练 |
| Agent | 持久化多步骤工作流、结果引用、OPA、逐步审批、取消、进程互斥、重启恢复与人工核对 | 模型自主多轮规划、费用预算、复杂条件及补偿 |
| 隐私 | 云能力限制、公开资料最小调用、披露记录 | 完整 NER、本地复核、占位符往返还原；私人内容上云保持拒绝 |
| 自动化 | 多步骤 Cron 工作流、幂等提交、Outbox/JetStream 发布 | 事件消费者、Skill 条件与补偿 |
| 插件 | 显式映射、凭据隔离、停用、配置回滚 | sandboxd、gVisor、网络沙箱、签名/SBOM、完整卸载验证 |
| 模型 | llama.cpp 本地生成；OpenAI 兼容协议 | MLX/vLLM 独立真实验证、云账户集成、Reranker |
| 文档/语音/家居/邮件 | 对应适配器代码、受控调用路径 | Docling、FunASR、whisper.cpp、CosyVoice、HA、邮件真实闭环 |
| iOS | 五个页面、配对、数据授权导入、录音入口、证书校验 | APNs、后台队列、App Intent、任务事件流、真机验收 |
| 远程 | 主动 frp 隧道、实例签名、租约、TLS 透传；已有真实连通/撤销记录 | 家庭域名 ACME 自动申请续期、长期断网与配额故障演练 |
| 运维 | 独立迁移账号、加密备份、隔离库恢复与删除日志重放 | 每日备份调度、异机/密钥恢复、RPO/RTO、生产隔离验收 |

## 系统架构

核心采用 Python 模块化单体。HTTP API、任务 worker、记忆 worker 是不同进程，共享核心契约和数据库；逻辑模块不是几十个独立微服务。推理服务和第三方 Provider 在独立进程/环境运行，不能获得 Core 数据库凭据。

```mermaid
flowchart TB
    IOS[iOS 18+ SwiftUI] -->|HTTPS / 设备签名| API[Core API]
    WEB[React 管理后台 /admin] -->|同源 Cookie / CSRF| API
    API --> AUTH[身份 / 授权 / OPA]
    API --> DATA[数据与规范记忆账本]
    API --> TASK[持久化任务 / 审批]
    DATA --> PG[(PostgreSQL + pgvector)]
    TASK --> PG
    WORKER[Core Worker] --> TASK
    WORKER --> OUTBOX[事务 Outbox]
    OUTBOX --> NATS[NATS JetStream]
    MW[Memory Worker] --> DATA
    MW --> EMBED[本地 Embedding]
    MW --> DERIVED[Mem0 / Graphiti 派生索引]
    WORKER --> POLICY[每次调用策略检查]
    POLICY --> REG[Capability / Provider Registry]
    REG --> LOCAL[本地模型与业务 Provider]
    REG --> PRIVACY[云隐私出站限制 / 披露]
    PRIVACY --> CLOUD[OpenAI 兼容云服务]
```

### 工程目录与职责

| 路径 | 职责 |
|---|---|
| `server/homeai/api.py` | 应用装配、设备会话、数据/任务/Provider API |
| `server/homeai/browser_auth.py` | 浏览器密码、TOTP、Cookie、CSRF 与重新认证 |
| `server/homeai/db.py`、`server/alembic/` | 数据模型、主体作用域、数据库版本与 RLS |
| `server/homeai/data.py`、`deletion.py` | 数据版本、访问权限、来源删除闭包 |
| `server/homeai/memory.py`、`vector_index.py` | 候选确认、规范内容检索、向量对账与重建 |
| `server/homeai/derived_memory.py` | 外部记忆投影的清除、重建、检查点与失败状态 |
| `server/homeai/runtime.py`、`task_control.py`、`worker.py` | 多步骤执行、审批恢复、人工核对、定时调度、事务事件发布 |
| `server/homeai/policy.py`、`privacy.py` | 核心风险等级、OPA、云出站限制 |
| `server/homeai/providers.py` | 端点/能力映射、凭据注入、实际 HTTP/MCP 调用 |
| `server/homeai/remote.py`、`remote_agent.py` | 独立服务器身份、绑定、短租约、frpc 生命周期 |
| `providers/homeai_providers/` | 第三方 SDK 桥；独立安装依赖 |
| `admin-web/` | 管理后台源码；构建结果由 Core 同源部署 |
| `ios/` | SwiftUI、Keychain、设备签名和系统数据连接器 |
| `contracts/` | 由服务端生成的 OpenAPI 和 JSON Schema |
| `deploy/` | 开发基础服务、OPA 策略和镜像构建配置 |
| `scripts/` | 初始化、迁移、模型登记、备份恢复、契约导出 |
| `server/tests/` | 单元与真实服务集成测试；不作为业务运行数据 |

### 数据归属与安全边界

- `Principal` 是成员身份；家庭管理者能管理基础设施，不能因此读取其他成年成员私有记录。
- `Record` 保存规范数据，`Revision` 记录版本，`Grant` 表达显式共享。应用层授权与 PostgreSQL `FORCE ROW LEVEL SECURITY` 同时生效。
- 私人正文、候选、任务参数/结果和 Secret 使用随机数据密钥加密，再由主密钥包裹；数据库记录通过主体及用途绑定 AAD。
- **pgvector 中用于计算距离的向量不是应用层密文**，向量也可能泄露语义。生产必须使用磁盘加密、数据库访问隔离与备份加密；不能宣称数据库所有列都是加密正文。
- 本地主密钥仍须由操作者安全保管、解锁；它不能随备份归档一起存放。当前不是自动 TPM 解锁方案。
- Provider 输出、MCP 描述、附件和模型回答均不具有授权能力。风险等级由 Core 决定，R4 操作不开放。

## 主要业务流程

### 1. 安装与首次身份建立

1. 启动 PostgreSQL、OPA、NATS，使用迁移账号执行迁移。
2. 本机创建主密钥和 HTTPS 证书，启动 Core；从终端创建家庭管理员并领取短期票据。
3. 网页账户通过 `web-setup` 票据绑定该管理员，再设置密码和 TOTP。没有票据不能匿名抢占管理员。
4. iPhone 使用独立配对票据和可信证书指纹配对，上传设备公钥，取得短期访问凭据。
5. 手机后续请求签名绑定时间、随机数、方法、路径、请求体摘要和令牌摘要；刷新凭据轮换，撤销设备后原凭据失效。

网页与 iPhone 使用两套会话机制，但业务授权复用同一个主体。平台账号不等于家庭管理员账号。

### 2. 数据导入、共享与删除

```mermaid
sequenceDiagram
    participant C as iOS/管理后台
    participant A as Core API
    participant D as PostgreSQL
    participant W as Memory Worker
    C->>A: 提交来源ID、来源版本和正文
    A->>A: 身份、授权、密级和版本检查
    A->>D: 同一事务写规范记录、审计、Outbox
    D-->>C: 返回记录ID/版本或409冲突
    W->>D: 读取已授权规范事实与版本
    W->>W: 本地生成向量/重建派生投影
    C->>A: 删除来源
    A->>A: 先持久化独立删除日志
    A->>D: 墓碑、派生闭包删除、清理结果缓存
    W->>W: 清理索引；外部投影清除后重建
```

同一来源同一版本且内容相同是幂等写入；同版本不同内容、旧版本覆盖和已删除来源自动复活均拒绝。共享撤回立即影响 Core 查询。删除来源时递归撤下引用该来源的事实与候选，恢复旧备份时先重放独立删除日志，再允许访问。

### 3. 记忆确认与检索

候选并非有效事实：来源必须存在且属于调用者，确认时再次检查删除状态；用户确认后才写入 `memory.fact`。Mem0/Graphiti 不拥有规范事实的唯一副本。

向量 worker 检查 Provider 身份、版本、模型、端点、输入前缀与记录版本。配置变化或手动清除向量后自动补建，每次最多生成 50 条；事务锁防止同一主体被两个 worker 同时重建。模型输出必须是有限数值向量。查询向量生成后在 pgvector 计算距离，结果再次回到规范账本授权读取；过期版本不返回。

外部记忆消费者按主体与 Provider 建立检查点：发生记录变更时清除该主体投影，再从账本重建。中途失败保留 `FAILED` 和错误类型，后续循环重新清除再重试；未同步的投影拒绝查询。当前采用保守全量重建，适合验证一致性，尚不是大规模增量图同步实现。停用 Provider 后不再向其发送请求，外部残留数据须在恢复连接后清除，不能声称离线第三方存储已删除。

| 接口 | 用途 |
|---|---|
| `GET /api/v1/memory/index` | 当前调用者的可索引数、就绪数、待处理数及 Provider |
| `POST /api/v1/memory/index/rebuild` | 清除自己的向量投影，交给 worker 重建，不删除规范数据 |
| `GET /api/v1/memory/derived` | 外部记忆 Provider 的同步状态、尝试次数和错误类型 |
| `GET /api/v1/memory/search?q=…` | 返回 `pgvector_exact` 或明确的 `authorized_literal` 降级模式 |

### 4. 任务、审批与异常

当前任务按 `RECEIVED → EXECUTING → SUCCEEDED/FAILED` 执行；高风险工具进入 `AWAITING_APPROVAL → APPROVED`，参数摘要、主体和有效期必须与审批一致。取消标志、设备撤销和引用记录权限在调用返回时再次检查。

任务提交幂等键绑定请求摘要，相同键不同请求返回冲突。外部副作用在超时或进程中断后可能进入 `NEEDS_RECONCILIATION`，不会盲目重放。已支持显式声明的多步骤工作流，`max_steps` 在提交时限制步骤数。每次 worker 执行一个未完成步骤，成功后持久化结果；下一步骤可引用前序结果。任务连接级 PostgreSQL 锁跨事务保持，多个 worker 不会同时执行同一任务；进程退出后锁由数据库释放。启动时不再批量修改所有执行中任务。

恢复时跳过已成功步骤；内部事务操作使用 Invocation ID 作为来源幂等标识；只读网络操作在有限预算内重试；执行中断的外部副作用进入 `NEEDS_RECONCILIATION`，即使任务已过期也不能误报为确定失败。网络调用受单步超时与任务总截止时间限制。

**显式多步骤工作流不等于模型自主多轮 Agent 已完成。** 当前自然语言工具规划仍是单轮，费用预算、条件分支和补偿仍待开发。

```json
{
  "idempotency_key": "my-workflow-20260917-001",
  "max_steps": 2,
  "timeout_seconds": 600,
  "step_timeout_seconds": 120,
  "max_read_retries": 1,
  "steps": [
    {"capability": "reminder.create@v1", "arguments": {"title": "准备出行资料"}},
    {"capability": "reminder.create@v1", "arguments": {"title": "检查前一事项", "linked_record": {"$step": 0, "path": ["record_id"]}}}
  ]
}
```

将上述结构提交到已认证的 `POST /api/v1/tasks`。`$step` 从 0 起，只能指向本任务已完成步骤；`path` 只接受对象键和数组下标，不允许代码或表达式。`model.generate@v1` 的显式步骤可使用 `message` 和 `context` 参数，`context` 可引用前序结果，仍经秘密检测且多步骤工作流只开放本地模式。

通过 `GET /api/v1/tasks/{id}/steps` 或管理后台查看步骤。未知外部结果需先在外部系统核对，再调用 `POST /api/v1/tasks/{id}/reconcile`，提交 `COMPLETED`、`NOT_EXECUTED` 或 `ABORT` 及核对依据。人工确认结果明确标记为 `user_reconciliation`，不伪装成 Provider 自动确认。确认未执行后，R3 操作必须重新审批；过期和已取消任务不能重新执行。浏览器核对操作要求近期密码与 TOTP 验证。

Cron 自动化将整份 Skill 提交为一个工作流，也走相同任务入口和逐步审批策略。Outbox 与业务写入同事务，NATS 发布使用事件 ID 去重；这不意味着下游所有消费者或外部服务具有恰好一次执行保证。

### 5. 模型、插件与云调用

调用路径是 `Task → Policy → Registry → Provider`。Manifest 声明版本、适配器、能力、端点和允许主机；秘密由 Core 按主体与 Provider 对应关系注入，不返回管理页面，也不拼进模型上下文。

本地模型失败不会自动换成云模型。当前云端只允许指定公开资料总结/翻译路径，受限制指令、密级、显式云策略和披露审计共同约束。完整私人数据脱敏还没完成，不能为了“成功回答”放宽。

生产 Provider 启用目前主动拒绝，因为操作系统级沙箱未验收。开发环境的主机白名单属于应用层防护，不等同于 gVisor、网络命名空间或禁止插件访问宿主的完整隔离。

### 6. 可选远程访问

```mermaid
flowchart LR
    PHONE[远程 iPhone] -->|家庭域名:8443 / HTTPS| RELAY[HAProxy TCP / frps]
    HOME[家庭 frpc] -->|主动出站:7443 / TLS| RELAY
    RELAY -->|隧道传输加密流量| HOME
    HOME --> HTTPS[家庭 HTTPS Core]
    HOME -->|实例签名 / 短租约| PLATFORM[独立 Home AI Connect]
```

家庭路由器无需开放入站端口。注册、邮箱验证、人工收款、权益开通、子域名选择发生在平台；家庭后台确认绑定后才建立实例身份。已配对 iPhone 额外检查服务器身份，不仅看域名证书。

家庭 HTTPS 在家庭端终止，平台不保存家庭正文和模型密钥；但平台掌握域名和连接元数据，不能宣传为绝对无法访问任何信息。停用、到期、配额耗尽应断开远程连接，本地服务继续工作。平台提供的是子域名使用权，不是可独立转移的注册域名。

## 本机开发

要求 Python 3.12+、Docker、Xcode、XcodeGen。本机验证使用 Python 3.14；GitHub CI 使用 Python 3.12。iOS 已通过模拟器编译和证书校验测试，iOS 18 真机仍待验收。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python scripts/dev_env.py
docker compose --env-file .env.local -f deploy/compose.dev.yml up -d
.venv/bin/python scripts/migrate.py --runtime
.venv/bin/homeai init-key
.venv/bin/python scripts/tls.py
```

`.env.local` 的 `HOMEAI_DATABASE_URL` 应指向 `homeai_runtime`；迁移工具使用独立迁移账号，API 使用无 BYPASSRLS 权限的账号。主密钥只在首次初始化创建，禁止覆盖。首次初始化执行：

```sh
.venv/bin/homeai bootstrap --name 家庭管理员
```

配对码仅有效 5 分钟，不应复制到日志、Git 或聊天中。后续可以用 `homeai pair --user <用户ID>` 重新签发。开发证书指纹位于 `state/tls/fingerprint.txt`。

启动本地模型、API 和 worker（分别在三个终端运行）：

```sh
llama-server -m state/models/Qwen3-0.6B-Q8_0.gguf --host 127.0.0.1 --port 58080 -c 4096 --jinja --reasoning-budget 0
.venv/bin/python scripts/register_local_model.py
.venv/bin/uvicorn homeai.api:create_app --factory --host 127.0.0.1 --port 58443 --ssl-keyfile state/tls/server.key --ssl-certfile state/tls/server.crt
.venv/bin/python -m homeai.worker
```

API 地址为 `https://localhost:58443`，默认不开放局域网与公网。iOS 模拟器可以连接此地址；真机接入需要配置 LAN/VPN 地址及相应证书。

```sh
xcodegen generate --spec ios/project.yml
open ios/HomeAI.xcodeproj
```

首次进入“设置”填写地址、从本机可信渠道获取的证书指纹和配对码。Secure Enclave 真机私钥不导出；模拟器使用明确分支下的测试私钥。

## 测试

```sh
.venv/bin/pytest -q
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/migrate.py --test
HOMEAI_INTEGRATION=1 HOMEAI_MODEL_TEST=1 .venv/bin/pytest -q
```

集成测试固定使用 `homeai_test`，不写业务库。没有真实服务时集成测试明确跳过，不以模拟测试替代真实验收。

## Provider

`providers/manifests/` 为端点配置示例，注册后默认停用。模型名称和地址必须与实际服务一致。独立 SDK 适配器通过 `HOMEAI_ADAPTER` 选择，并强制校验 `PROVIDER_SERVICE_TOKEN`。各适配器使用独立环境，不把 Docling、语音或图数据库依赖安装到核心服务环境。

当前生产启用接口主动阻止未完成沙箱验收的 Provider。所有依赖尚未按生产 OCI Digest 和签名完成锁定，因此不能将开发 Compose 作为生产部署配置使用。

## 备份

`scripts/backup.py` 生成认证加密归档，并支持密文完整性验证。备份密钥应是独立的 32 字节随机文件，权限 0600，不随备份一起存储。`scripts/restore.py` 只允许恢复到全新隔离库，并强制重放独立删除日志。本机小样本演练已通过；异机恢复、备份期间附件一致性和 RPO/RTO 仍需验收。

## 网页管理与远程访问

登录式本地管理后台已实现，包含密码＋TOTP、敏感操作重新验证、模型配置、成员设备、数据、审批、自动化、备份列表、远程绑定与审计。备份恢复和服务器主密钥仍由本机管理。官方 Home AI Connect 是独立的闭源托管连接服务，可选用于远程访问；本地 AI、资料与家庭服务不依赖其账号或付费状态，也允许使用自建远程连接。

### 启用管理后台

```sh
cd admin-web
npm ci
npm run build
cd ..
.venv/bin/python scripts/migrate.py --runtime
.venv/bin/homeai web-setup --user <家庭管理员用户ID>
```

重启 API 后打开 `/admin/`。在“首次部署”入口输入本机短期凭据，设置用户名、密码并绑定验证器。初始化凭据沿用 CLI 输出中的 `pairing_token` 字段，5 分钟有效；它不是长期密码。忘记密码或丢失验证器时，在本机执行 `homeai web-recover --user <用户ID>`，再通过相同初始化页面重置；旧网页会话失效，手机设备身份保持独立。

浏览器会话使用 Secure/HttpOnly/host-only Cookie 与 CSRF 校验。敏感设置需要最近 5 分钟的密码与 TOTP 验证。生产须使用 HTTPS；仅本机开发 Origin 允许通过开发代理访问。

### 可选远程连接

在家庭后台生成连接申请码，在所选平台选择子域名，再将短期绑定码粘贴回家庭后台。家庭端持有独立实例私钥，不持有平台 Cloudflare 凭据。

安装并验证官方 frpc 二进制后，启动：

```sh
HOMEAI_FRPC_PATH=/absolute/path/to/frpc .venv/bin/python -m homeai.remote_agent
```

平台不可用或停用远程服务时，本地 AI 继续运行。客户端状态分别标明绑定、租约和进程状态；它们不等于真实外网已连通。家庭域名的受信任证书自动申请/续期仍在完善，当前不能将固定证书的透传验收描述为完整证书交付。

### 启动真实本地 Embedding

本机验证使用 llama.cpp 与 `ggml-org/embeddinggemma-300M-GGUF` 的固定版本。它是独立的检索模型，不拿生成模型的随意输出充当向量。模型权重约 334 MB，不进入 Git；使用前遵守模型仓库的许可。

```sh
curl -fL 'https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/0f741b5a6585bd53aeb15cd1372c56f2a0f65e12/embeddinggemma-300M-Q8_0.gguf' -o state/models/embeddinggemma-300M-Q8_0.gguf
shasum -a 256 state/models/embeddinggemma-300M-Q8_0.gguf
```

校验值应为 `b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63`，不匹配时停止。分别在终端运行：

```sh
llama-server -m state/models/embeddinggemma-300M-Q8_0.gguf --host 127.0.0.1 --port 58081 --embedding --pooling mean -c 2048
.venv/bin/python scripts/register_local_embedding.py
.venv/bin/python -m homeai.memory_worker
```

登记脚本先调用真实 `/embeddings` 验证输出，再写 Provider 配置。查询与文档使用模型所需的不同前缀；切换前缀也会使旧索引失效。管理后台“数据与记忆”可查看当前账户的就绪数，并提交重建。

独立测试库准备完成后执行：

```sh
HOMEAI_INTEGRATION=1 HOMEAI_MODEL_TEST=1 HOMEAI_EMBEDDING_TEST=1 .venv/bin/pytest -q
```

`test_real_embedding.py` 通过真实 HTTP 调用 Embedding 和 OPA，连接 PostgreSQL，覆盖向量维度、语义检索、跨成员隔离、事实更新、版本切换、手动重建与删除。其他单元测试可能使用测试替身，它们不算作 Provider 真实接入证据。没有对应服务时不设置这些开关，不得把跳过测试称为通过。

参考：[模型固定版本](https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/tree/0f741b5a6585bd53aeb15cd1372c56f2a0f65e12)、[llama.cpp Embedding 说明](https://github.com/ggml-org/llama.cpp/blob/master/examples/embedding/README.md)。

### 派生向量索引

配置本地 `model.embed@v1` Provider 后，单独运行 `python -m homeai.memory_worker`，避免索引阻塞任务执行。PostgreSQL 使用精确向量检索，结果回到规范账本读取；缺少 Embedding Provider 或索引不可用时明确降级为授权范围内文字检索。Mem0/Graphiti 已接入规范账本驱动的清除、重建和失败重试消费者，但 SDK 与真实模型、Neo4j 的集成验收仍未通过；不能将消费者源码视为两个 Provider 已交付。

## 许可与出处

自有代码采用 **Home AI OS Attribution License 1.0**（`LicenseRef-Home-AI-OS-Attribution-1.0`）。允许个人使用、商用、修改、闭源衍生与自行托管。对外发布的衍生产品或托管服务必须保留版权声明，并在关于页、文档或 CLI 关于信息中显示“基于 Home AI OS”及本项目链接。

这是自定义宽松许可，不是标准 MIT，也不宣称获得 OSI 认证。完整权利和条件以 [LICENSE](LICENSE) 为准；第三方组件适用各自许可。
