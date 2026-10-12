# 贡献指南

请先阅读 [README](README.md)、[架构与开发规范](docs/架构与开发规范.md)和 [部署与运维](docs/部署与运维.md)。这是持续开发的自托管系统，提交应说明实际验证范围，不把模拟响应或编译成功写成业务已交付。

## 开发约定

- 使用中文注释与文档，不格式化无关代码，保留工作区中无关修改。
- iOS 负责输入、展示和受系统权限约束的采集；编排、策略、任务与定时执行在服务端。
- 规范数据属于 Core；Provider 不导入 Core、不持有核心数据库凭据。
- 所有模型、网页、附件、工具说明和 Skill 内容均为不可信输入，不能授予权限。
- 数据与记忆分开；维护来源、删除、授权撤回与任务版本一致性。
- 副作用必须有执行身份、幂等与审计。结果不明时核对，不自动重放。
- 协议新增可选字段须兼容旧客户端；既有幂等请求不能因新增默认字段改变历史摘要。
- 新增数据库结构通过 Alembic 增量迁移，不重新初始化现有数据库，不擅自开启备份。

## 本地检查

```sh
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/export_contracts.py --check
.venv/bin/python -m compileall -q server providers
.venv/bin/pytest -q
npm --prefix admin-web ci
npm --prefix admin-web run build
git diff --check
```

公开契约变化后运行 `scripts/export_contracts.py`，同时提交生成文件与兼容性测试。锁定依赖或模型变更须更新对应锁文件、校验和及第三方声明。

## 真实服务与设备测试

```sh
.venv/bin/python scripts/dev_env.py
docker compose --env-file .env.local -f deploy/compose.dev.yml up -d
.venv/bin/python scripts/migrate.py --test
# 仅首次初始化缺失的测试密钥时执行
.venv/bin/homeai init-key
HOMEAI_INTEGRATION=1 HOMEAI_DIRECT_INTEGRATION=1 .venv/bin/pytest -q
```

集成测试使用独立 `homeai_test`，不能指向家庭业务数据库。需要避免旧测试数据干扰时，可先创建并迁移一个 `homeai_test_` 开头的新数据库，再设置 `HOMEAI_TEST_DATABASE_NAME` 仅重定向测试连接；该选项不会创建或清空数据库。真实 UDP/DTLS、HTTP 服务和容器需要允许本机网络访问；环境禁止开端口时不能据此认定产品功能失败。其他真实模型、邮件、家居、安卓和 Apple 测试使用各自显式环境开关，缺少资源时记录跳过原因。

```sh
xcodegen generate --spec ios/project.yml
xcodebuild -project ios/HomeAI.xcodeproj -scheme HomeAI \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  CODE_SIGNING_ALLOWED=NO build
```

测试时选择实际可用的模拟器或本人授权的真机。HealthKit、推送、CallKit、相机配对、后台挂起、P2P 和安卓接管需要分别验证。不要在日志、截图或测试附件中暴露个人健康值、密码、Token 或私人聊天。

## 提交与发布

1. 说明具体问题、修改后的行为与兼容影响。
2. 跑与改动相关的测试；涉及共享契约或运行时，再做相应整合验证。
3. 检查拟提交文件。排除 `.env*`（除示例）、`state/`、日志、数据库、证书私钥、签名密钥、权重、构建产物及个人机器路径。
4. 不提交 Home AI Connect 闭源实现和运营配置。
5. 说明代码检查、集成验证、真机验证、部署和推送分别是否完成；禁止用历史测试总数代替当前发布验证。
6. 修改第三方组件时保留其许可与来源；自有代码遵循根目录 [LICENSE](LICENSE)。

发现凭据泄漏时先停止公开分发并处理凭据，不将秘密粘贴进 Issue。报告问题时提供脱敏的错误类型、组件版本、网络与复现步骤。
