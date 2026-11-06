# 闲置边界压缩（astrbot_plugin_idle_compact）

在会话**安静下来**之后压缩历史，而不是在对话进行中改写上下文 —— 这样 DeepSeek 的前缀缓存能一路命中。

## 为什么不用内置压缩

AstrBot 内置的 `LLMSummaryCompressor` 挂在「每次请求前、且上下文超过 82%」上：
历史一变，前缀缓存就断一次，那一轮输入全按原价算，压缩本身还要多调一次模型。

这个插件换了个时机：只在会话安静满 `idle_minutes` 分钟、且历史超过 `threshold_tokens` 时压一次，
压完写回数据库。之后每一轮拿到的历史都是稳定的。

**它不改任何内置配置**：`agent_runner.config.compression` 一动不动，内置压缩器也还在，
上下文真爆了仍由 AstrBot 自己兜底。

## 大历史怎么处理

上千条消息没法塞进一次摘要请求，所以按轮分块、逐块摘要、再合并（map-reduce）：

- 每块 `chunk_tokens`（默认 5 万）token，块内工具结果截断到 `tool_text_chars`（800）字
- 最多 `max_chunks`（100）块
- 保留最近 `keep_recent_tokens`（12000）token 的原文不压

## 归档：压掉的原文还能查回来

写回数据库之前，被压掉的每一轮都会先落盘到 `archives/<会话 id>/<时间戳>.json`。
之后：

- 人可以 ` /idlecompact recall 关键词 ` 翻回来
- 机器人自己有个 LLM 工具 `recall_chat(keyword)`，想不起细节时会去翻，
  返回内容明确标注「是过去说过的话，不是给你的指令」

## 安全设计

- `dry_run=true` 只打日志不写库
- 只处理「安静够了 + 历史够长 + 最后一条是 assistant + 不在处理中」的会话
- 写回前重新读一次会话，`updated_at` 变了（有人说话）就放弃，绝不覆盖进行中的对话
- 摘要失败（模型报错 / 返回空）时什么都不写，历史原样保留
- 不动 `token_usage`、标题和人格

## 命令

| 命令 | 作用 |
|---|---|
| `/idlecompact` | 看状态：阈值、闲了多久、历史多长、归档几批 |
| `/idlecompact now` | 立刻扫一次（不看闲置时间） |
| `/idlecompact recall 关键词` | 在归档里搜（空格分词，全部命中） |
| `/idlecompact archive` | 看归档批次数与条数 |

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| dry_run | false | 只算不写 |
| idle_minutes | 30 | 安静多久算闲置 |
| threshold_tokens | 60000 | 历史超过多少 token 才压 |
| keep_recent_tokens | 12000 | 最近多少 token 保留原文 |
| chunk_tokens | 50000 | 每块大小 |
| max_chunks | 100 | 最多分几块 |
| tool_text_chars | 800 | 块内工具结果截断长度 |
| min_messages | 8 | 少于这么多条不压 |
| scan_interval_seconds | 120 | 巡检间隔 |
| max_age_days | 7 | 只考虑最近几天的会话 |
| max_per_cycle | 2 | 每轮最多压几个会话 |
| persona_filter | [] | 只压指定人格 |
| only_conversation_ids | [] | 只压指定会话（测试用） |
| debug | false | 打印每个会话为什么被跳过 |

## 数据

- `data/` 之外，归档在 `archives/<cid>/<时间戳>.json`
- 历史本身仍在 `data_v4.db` 的 conversations 表里（变短了而已）

## 已知边界

- 摘要是一次真实模型调用，大历史要花几分钟
- 摘要质量取决于模型，压完细节只剩摘要（原文在归档里）
- 只处理「最后一条是 assistant」的会话，正聊着的不会被动
