# 检索与管理后端

## 启动

先运行 [入库服务](../ingest_gateway/) 和 RAGFlow。检索服务读同一份入库配置和登记表。

```bash
python -m pip install -r algorithm/retrieval/requirements.txt
export CATALOG_INGEST_BASE=http://127.0.0.1:30269
export RETRIEVAL_API_BASE=http://127.0.0.1:29390/api/v1
python -m uvicorn --app-dir algorithm/retrieval retrieval_service:app --host 127.0.0.1 --port 30271
```

`MANAGEMENT_DATA` 可指定账号、审核、审计和密钥保存目录。管理后端默认在 `management/private/` 保存这些文件；方向登记默认放在检索目录下的 `library_catalog.sqlite3`，也可用 `LIBRARY_CATALOG_DB` 指定。第一次添加超管时，用下面的命令交互设置密码。如果指定了 `MANAGEMENT_DATA`，给账号脚本传入同一个目录。

```bash
python algorithm/retrieval/management/manage_accounts.py add \
  --employee-id DEMO001 --name DemoAdmin --role superadmin
```

## 专用算法 API

[image/](image/) 是实际使用的派生镜像源码。构建后，把运行环境的 `service_conf.yaml` 和 `llm_factories.json` 放在挂载到容器 `/run/retrieval-config` 的私有目录里。配置应能从容器访问目标 RAGFlow 的数据库、索引、对象存储和模型。将容器加入对应网络，让宿主机检索服务访问容器的 9380 端口。

```bash
docker build -t tunnel-ragflow-evidence:0.27.2 algorithm/retrieval/image
```

网关默认使用 `standard`，也支持 `medium`、`high`。网关把问题限在指定的一个库，要求 evidence 接口只返回原文块。返回包含块 ID、文档 ID、文档名、原文、页码和位置等字段。

## 系统组接入

[OpenAPI 文件](management/openapi.json) 可导入 Apifox。下面路径均以 `/retrieve/manage/v1` 开头。

| 调用 | 作用 |
| --- | --- |
| `POST /auth/login` | 传 `identifier`（工号或手机号）和 `password`，从 `data.access_token` 取登录令牌 |
| `GET /directions`、`GET /libraries` | 选方向和知识库 |
| `POST /libraries`、`POST /libraries/{library_id}/documents` | 建库和上传。管理员与普通用户的操作进入审核流程 |
| `GET /reviews/{review_id}`、`POST /reviews/{review_id}/decision` | 查看申请和处理审核 |
| `GET /records/{record_id}` | 查询解析状态 |
| `POST /search` | 传 `library_id` 和最多 8000 字符的 `text`，可选 `mode`、`top_n` |
| `/model-keys`、`/audit` | 管理模型密钥及查看审计记录 |

登录后的请求带 `Authorization: Bearer <access_token>`。Agent 可用 [agent_handler.py](agent_handler.py) 的工具参数和调用示例，将返回的 `data.chunks` 交给后续回答流程。

旧版 `/retrieve/search` 保留原来的检索字段并要求登录。入库写操作通过上述管理接口提交审核，旧的 `/ingest/` 用作内部服务。
