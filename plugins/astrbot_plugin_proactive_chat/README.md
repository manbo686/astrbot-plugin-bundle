# 主动聊天（astrbot_plugin_proactive_chat）

主人半天不说话时，让它自己想个话题来找你 —— 不是定时群发，而是按心情挑一个合适的由头。

## 它做什么

1. 记录每个会话里「主人最后一次说话」的时间
2. 闲置超过 `idle_min_minutes` 后，在 `idle_min_minutes ~ idle_max_minutes` 之间随机挑一个时刻
3. 到这个时刻按 `probability` 掷一次骰子，命中才用 cron 的一次性任务把它唤起
4. 唤起时按心情加权选题（关心 / 撒娇 / 好奇 / 分享 / 请求 / 记忆疑问），
   并参考当前心情、今日兴趣点、B 站活动
5. 说完会推心情（engagement / affinity），也会写进 LivingMemory

## 约束（防止变成刷屏）

- `daily_max`：一天最多主动几次
- `min_gap_minutes`：两次之间至少隔多久
- `quiet_hours`：这几个小时不主动（默认深夜不发）

## 命令

`/proactive`：看今天主动了几次、下一次大概什么时候、当前是否在静默时段。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| sessions | `["qq:FriendMessage:123456789"]` | 对哪些会话生效 |
| idle_min_minutes / idle_max_minutes | 45 / 180 | 闲置多久之后考虑 |
| daily_max | 3 | 每天上限 |
| quiet_hours | 见配置 | 静默时段 |
| probability | 0.35 | 掷骰子命中率 |
| min_gap_minutes | 90 | 两次最小间隔 |

## 数据

- `data/state.json`：每个会话的最后发言时间、今日次数、下次计划

## 联动

| 看谁 | 拿什么 | 对方不在时 |
|---|---|---|
| 心情引擎 `astrbot_plugin_mood` | 六维数值，决定这次的话题偏轻松还是偏沉 | 只按概率与时间窗触发 |
| 兴趣点引擎 `astrbot_plugin_bili_interest` | 今天的分区预算，当话题素材 | 只用内置话题池 |

两个都通过 `get_registered_star` 取，取不到就跳过这一层，不影响主动聊天本身。

## 已知边界

- 依赖 cron 的一次性任务，AstrBot 没跑就不会触发
- 主人回一句之后计时重来，不会在对话中插话
