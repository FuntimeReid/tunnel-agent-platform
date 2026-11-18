# 算法组

| 目录 | 内容 |
| --- | --- |
| [ragflow/](ragflow/) | 共享测试源码整理自 RAGFlow v0.27.2，包含库与文档、检索、解析、索引及已有改动。上游提交为 `a024bea0cd93f39e6652a42bf84dd20c55bc560b`，许可证见 [LICENSE](ragflow/LICENSE)。 |
| [ingest_gateway/](ingest_gateway/) | 入库 API、文件分类与解析路线、后台任务、重复检查、进度和重试。 |
| [retrieval/](retrieval/) | 检索 API、方向与知识库目录、账号与审核、模型密钥管理，以及选用的 Agentic RAG 镜像源码。 |
| [ocr/](ocr/) | PaddleOCR-VL、PP-StructureV3、结构化切块、表格与公式清理。 |
| [demo/](demo/) | 用于验证接口的中期演示页。 |
| [evaluation/](evaluation/) | 三个档位的批量调用脚本及示例题。 |

日常接口联调从 [入库](ingest_gateway/README.md) 和 [检索后端](retrieval/README.md) 开始。运行环境提供模型、服务密钥、知识库和数据库。仓库内的配置示例用本机地址。

基础 RAGFlow 供两组查看和修改现有代码。当前专用检索镜像由官方 `infiniflow/ragflow:v0.27.2` 加 [三份证据相关源码](retrieval/image/patches/) 构建，使用已选用的 `opt-r2-focused-documents` 版本。两处代码按各自部署方式维护。

基础 Docker Compose 的示例位于 [ragflow/docker/.env.example](ragflow/docker/.env.example)。进入 `ragflow/docker`，将它复制为 `.env`，填写本机配置，再用 `docker compose -f docker-compose.yml up -d` 启动基础栈。详见 [RAGFlow 中文说明](ragflow/README_zh.md) 和 [Docker 说明](ragflow/docker/README.md)。
