# 文档入库服务

服务接收文件，先按文件内容识别格式，再结合文件类别选择分块模板。PDF 交给 RAGFlow 和 MinerU 解析，保留页码。新版 Office 文档先经 MinerU 转 Markdown，Markdown 和文本直接提交给 RAGFlow。

上传后返回登记记录。后台完成解析并更新进度。服务在同一库中按文件 SHA-256 判断重复，失败记录可以重试。

## 启动

1. 准备一个 RAGFlow 服务账号和它使用的 MinerU 模型。在 MySQL 创建独立的登记数据库和账号，执行 [schema.sql](deploy/schema.sql)。
2. 把 [ingest.example.yaml](config/ingest.example.yaml) 复制为 `config/ingest.yaml`，把 [ragflow-account.example.json](config/ragflow-account.example.json) 复制为 `config/ragflow-account.json`。填写本地 RAGFlow、MinerU、MySQL 的连接信息。文件路径相对于 YAML 所在目录，也可填写绝对路径。
3. 在配置指定的私有文件中填写 RAGFlow API Key 和 MySQL 密码。运行时可通过 `INGEST_CONFIG` 指定另一份 YAML。
4. 从仓库根目录启动。入库服务用于后端内部调用，系统组从 [管理接口](../retrieval/management/openapi.json) 处理登录、上传和审核。

```bash
python -m pip install -r algorithm/ingest_gateway/deploy/requirements.lock
python -m uvicorn --app-dir algorithm/ingest_gateway app.main:app --host 127.0.0.1 --port 30269
```

| 内部接口 | 作用 |
| --- | --- |
| `GET /ingest/health` | 检查服务和依赖 |
| `GET /ingest/libraries`、`POST /ingest/libraries` | 查看和创建登记库 |
| `POST /ingest/documents` | 上传，表单字段为 `library_id`、`file`、`category` |
| `GET /ingest/documents/{record_id}` | 查询处理进度和结果 |
| `POST /ingest/documents/{record_id}/retry` | 重试失败记录 |

[ragflow-patch/](ragflow-patch/) 保存原入库部署中让 Python 执行器和新版切块服务识别 `skip_chunk_image` 的补丁。应用到原版对应文件前，先在目标源码目录执行 `git apply --check`。共享工作树可能已经包含同样的逻辑。
