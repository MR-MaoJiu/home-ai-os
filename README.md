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

## 本机开发

要求 Python 3.12+、Docker、Xcode、XcodeGen。当前实际验证的 Python 为 3.14，iOS 为 Xcode 27 模拟器编译；尚未完成 Python 3.12 CI 和 iOS 18 真机验证。

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

登录式本地管理后台已实现，包含密码＋TOTP、敏感操作重新验证、模型配置、成员设备、数据、审批、自动化、备份列表、远程绑定与审计。备份恢复和服务器主密钥仍由本机管理。官方 Home AI Connect 是独立的闭源托管连接服务，未来可选用于远程访问；本地 AI、资料与家庭服务不依赖其账号或付费状态，也允许使用自建远程连接。

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

### 派生向量索引

配置本地 `model.embed@v1` Provider 后，单独运行 `python -m homeai.memory_worker`，避免索引阻塞任务执行。PostgreSQL 使用精确向量检索，结果回到规范账本读取；缺少 Embedding Provider 或索引不可用时明确降级为授权范围内文字检索。Mem0/Graphiti 的完整索引同步仍未完成。

## 许可与出处

自有代码采用 **Home AI OS Attribution License 1.0**（`LicenseRef-Home-AI-OS-Attribution-1.0`）。允许个人使用、商用、修改、闭源衍生与自行托管。对外发布的衍生产品或托管服务必须保留版权声明，并在关于页、文档或 CLI 关于信息中显示“基于 Home AI OS”及本项目链接。

这是自定义宽松许可，不是标准 MIT，也不宣称获得 OSI 认证。完整权利和条件以 [LICENSE](LICENSE) 为准；第三方组件适用各自许可。
