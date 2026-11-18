# 检索测试脚本

[run_questions.py](run_questions.py) 调用专用 evidence 接口，支持 `standard`、`medium`、`high` 和返回原文块或带引用回答。[questions.example.json](questions.example.json) 展示题目格式。

先跑 `--dry-run` 查看会发出哪些请求。去掉该选项后，按脚本提示输入目标 RAGFlow 的授权值，格式为 `Bearer <API Key>`。

```bash
python algorithm/evaluation/run_questions.py \
  --cases algorithm/evaluation/questions.example.json \
  --dataset-id 00000000000000000000000000000000 \
  --out runtime/evaluation --output chunks --dry-run
```

实际测试时换成目标知识库的 ID，并使用独立结果目录。
