# Token 电表（astrbot_plugin_token_meter）

把每次模型调用的用量、前缀缓存命中和请求结构记下来，按天落盘，再按单价估个花费。

## 它做什么

- `on_llm_request`：记下这一轮的 system_prompt 长度、contexts 条数、工具数量
- `on_llm_response`：读 `LLMResponse.usage`（`input_other` / `input_cached` / `output`），
  跟请求对上号，写一行 JSONL
- 顺手按配置里的单价（元/百万 token）估算「未命中 / 命中 / 输出」三段花费

## 怎么看

- 命令 `/tokens`：今天的汇总（调用次数、未命中、命中、命中率、输出、估算花费）+ 最近 7 天
- 接口（`/api/v1/plugins/extensions/astrbot_plugin_token_meter/` 下）：`state`（today / days / recent）、`reset`（清空今天）

## 数据

`data/usage-YYYYMMDD.jsonl`，一行一次调用：

```json
{"ts": 1793..., "date": "2026-11-05", "time": "21:03:12", "umo": "qq:FriendMessage:123456789",
 "model": "deepseek-flash", "in_other": 3866, "in_cached": 184704, "out": 512,
 "sp_chars": 7797, "prompt_chars": 412, "ctx_msgs": 68, "ctx_chars": 29011, "tools": 34}
```

- `in_other` = 没命中缓存的输入 token，`in_cached` = 命中缓存的输入 token
- 命中率 = `in_cached / (in_cached + in_other)`

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| price_in_miss / price_in_hit / price_out | 见配置 | 单价（元 / 百万 token） |
| keep_days | 30 | 保留多少天 |
| show_recent | 10 | `/tokens` 里列几次最近调用 |

## 已知边界

- 只有 provider 真的返回 `usage` 时才有数（DeepSeek 是返回的）
- 估算花费按配置单价算，官方调价要自己改
- 只读：不改任何请求，出问题最多是少记一条
