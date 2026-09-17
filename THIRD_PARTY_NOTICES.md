# 第三方组件

Home AI OS 自有代码适用根目录 LICENSE。第三方 SDK、服务、模型和容器分别适用其自身许可证，根目录许可不覆盖它们。

本仓库不包含 `.venv`、node_modules、模型权重或第三方项目源码。核心依赖通过包管理器安装；精确版本记录于 requirements.lock。部署者分发包含第三方组件的镜像或模型时，仍需保留对应的版权、许可和模型使用条件。

主要上游：FastAPI、SQLAlchemy、Alembic、PostgreSQL、pgvector、NATS、OPA、llama.cpp。可选适配：Mem0、Graphiti、Docling、FunASR、whisper.cpp、CosyVoice、Home Assistant、MCP、MLX、vLLM、SearXNG。

可选适配器的存在不表示上游所有版本、模型或部署组合已经验证，也不意味着上游对本项目背书。参考链接见 docs/来源与兼容性.md。
