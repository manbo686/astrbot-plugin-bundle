# 更新日志

## v1.0.0
- map-reduce 分块摘要：按轮分块、逐块压缩、再合并
- 只在闲置边界触发，写回前复核 `updated_at` 与条数
- 归档：压掉的原文落 `archives/<cid>/<时间戳>.json`
- `recall_chat` LLM 工具 + `/idlecompact recall` 搜索归档
- `dry_run`、`only_conversation_ids`、`debug` 三个调试开关
