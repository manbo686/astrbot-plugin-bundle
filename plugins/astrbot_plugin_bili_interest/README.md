# 兴趣点引擎（astrbot_plugin_bili_interest）

每天给它一张「今天想看什么」的清单，再按这张清单把当天的视频额度分到 B 站分区。

## 它做什么

1. 每天 `refresh_hour`（默认 6 点）之后第一次活动时，读它的**人格 / 心情 / 日记 / 最近记录**，
   让模型生成 `interest_count`（默认 4）个兴趣点；每个兴趣点绑定一个 B 站分区，带权重和一句「为什么想看」。
2. 把当天 `daily_videos` 条额度按权重随机分配到这些分区：
   **每个分区至少 1 条，至少覆盖 `min_zones`（3）个分区、最多 `max_zones`（6）个**。
   `daily_videos = 0` 表示自动（取 wake_plan 的醒来次数 × 每次视频数，取不到就用 10）。
3. 额外发三种「固定位」，可以在配置里关掉：
   - 探索位 `explore_count`（2）：从 `explore_pool` 里随机挑分区
   - 热门位 `hot_count`（2）：全站热门榜
   - DeepSeek 位 `deepseek_max_per_day`（2）：按 `deepseek_keywords` 找相关视频
4. 面板（`pages/app/index.html`）：今日兴趣点、分区预算条（绿色=已看）、最近 7 天汇总。
5. `bili_activity` 每看完一个视频会回报分区，绿条随之增长。**额度只在当天有效，零点清零不结转。**

## 怎么用

- 面板：AstrBot → 插件 → 已安装 → 兴趣点引擎 → 页面
- 命令：`/interest` 看今日状态
- 接口（`/api/plug/astrbot_plugin_bili_interest/` 下）：

| 路由 | 方法 | 作用 |
|---|---|---|
| `state` | GET | 今日兴趣点 + 分区预算 + 已看 |
| `roll` | POST | 立刻重新生成（`force`） |
| `used` | POST | `{"zone": "音乐", "n": 1}` 记一条已看 |
| `history` | GET | 最近若干天的汇总 |

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| min_zones / max_zones | 3 / 6 | 至少/最多覆盖几个分区 |
| interest_count | 4 | 每日兴趣点个数 |
| daily_videos | 0 | 当天视频总额度，0=自动 |
| refresh_hour | 6 | 几点后可以生成新一天的清单 |
| explore_count | 2 | 探索位条数 |
| explore_pool | 见配置 | 探索位候选分区 |
| hot_enabled / hot_count | true / 2 | 热门位 |
| deepseek_enabled / deepseek_max_per_day | true / 2 | DeepSeek 位 |
| persona_id | （留空=默认人格） | 用哪份人格做素材 |
| context_chars / history_days / llm_timeout / display | — | 生成素材长度、历史保留天数、超时、面板显示选项 |

## 数据

- `data/today.json`：当天兴趣点、分区预算、已看记录
- `data/history.json`：历史每日汇总，保留 `history_days` 天

## 联动

- 读 `../astrbot_plugin_mood/data/state.json` 拿当前心情标签
- 被 `astrbot_plugin_bili_activity` 调用 `mark_used()` 记账

## 已知边界

- 兴趣点质量取决于模型；同一分区可能被多个兴趣点占用
- 只统计「预算内」的已看；它临时去看美食/影视等预算外分区，不会记账
- 生成失败时会退回上一份清单，不会清空
