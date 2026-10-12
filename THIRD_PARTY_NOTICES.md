# 第三方组件

Home AI OS 自有代码适用根目录 LICENSE。第三方 SDK、服务、模型和容器分别适用其自身许可证，根目录许可不覆盖它们。

本仓库不包含 `.venv`、node_modules、模型权重或第三方项目源码。核心依赖通过包管理器安装；精确版本记录于 requirements.lock。部署者分发包含第三方组件的镜像或模型时，仍需保留对应的版权、许可和模型使用条件。

主要上游：FastAPI、SQLAlchemy、Alembic、PostgreSQL、pgvector、NATS、OPA、llama.cpp。可选适配：Mem0、Graphiti、Docling、FunASR、whisper.cpp、CosyVoice、Home Assistant、MCP、MLX、vLLM、SearXNG。

可选适配器的存在不表示上游所有版本、模型或部署组合已经验证，也不意味着上游对本项目背书。固定版本依据与参考链接见 [来源与兼容性](docs/来源与兼容性.md)。

本地向量验证使用 Google EmbeddingGemma 300M 的 GGUF 量化权重，权重未提交到本仓库。模型适用独立的 [Gemma 使用条款](https://ai.google.dev/gemma/terms)，不是本项目归属许可。来源与输入格式见 [Google 模型卡](https://huggingface.co/google/embeddinggemma-300m)；本机验证版本为 `0f741b5a6585bd53aeb15cd1372c56f2a0f65e12`，GGUF SHA256 为 `b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63`。

P2P 组件使用 aiortc 1.15.0、aioice 0.10.2、PyAV 17.1.0 和 pylibsrtp 1.0.0，其已安装包元数据标识为 BSD-3-Clause；完整版本见 `requirements-p2p.lock`。二进制包所包含的底层库仍适用各自随包许可，打包分发前需保留对应许可文件。

iOS 直连使用固定版本 WebRTC 153.0.0（stasel/WebRTC 二进制发行）。完整 BSD 许可与上游第三方声明保存在 `ios/HomeAI/Resources/WebRTC-LICENSE.txt`，随应用资源一同分发。

安卓视频的 iOS 系统画中画使用系统 AVKit／AVFoundation 与上述 WebRTC 视频帧，未新增第三方画中画 SDK。`android-input/` 中的触控桥、输入法与验证页面属于本项目第一方代码，适用根目录 LICENSE；构建它们所使用的 Android SDK、平台工具与系统 API 仍遵循各自条款。

安卓连续视频使用 [scrcpy 4.1](https://github.com/Genymobile/scrcpy/tree/v4.1) 的设备端组件，适用 Apache-2.0，由 `scripts/android_media.py install` 从官方发行版下载并校验固定 SHA256。组件及上游完整许可保存在 `state/android-media/`；打包分发时必须一并保留其 LICENSE。仅启用视频与可选系统声音采集，关闭上游控制和剪贴板通道，触控继续走本项目的成员权限与手势检查。

- 管理端二维码使用 qrcode.react 4.2.0（ISC），在浏览器本地生成，不向第三方服务发送配对信息。

## 加密备用通道

端到端加密备用通道使用 PyHPKE 0.6.4（MIT），实现 RFC 9180 HPKE。项目与许可证：<https://github.com/dajiaji/pyhpke>。iOS 使用系统 CryptoKit 的 HPKE 实现。
