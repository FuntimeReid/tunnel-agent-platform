# 接口演示

这个页面用于查看方向、知识库、入库进度和检索返回。提供同一入口转发到已运行的入库、检索网关和 RAGFlow。静态页面在 [web/](web/)。

在 Python 3.10–3.12 下，从仓库根目录启动展示服务：

```bash
python algorithm/demo/deploy/public_server.py \
  --gateway-url http://127.0.0.1:30268 \
  --ragflow-url http://127.0.0.1:8080 \
  --api-key-file /path/to/local/ragflow-key
```

页面默认在 `http://127.0.0.1:30272`。原录入演示采用指定库范围，使用本地 `public_state.json` 保存。系统组的正式页面和桌面端放在 [system/](../../system/)。
