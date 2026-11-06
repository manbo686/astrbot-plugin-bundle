# 醒来时刻表（astrbot_plugin_wake_plan）

它一天只按这一张表「醒」：所有定时任务都挂在醒来点上，而不是各自设 cron 互相打架。

## 它做什么

1. 每天按 `windows` 排一批醒来点：
   默认 **12:00–14:00 与 18:00–24:00 两个窗**，窗内随机取点，间隔不小于 `min_gap_minutes`（25 分钟），
   总数在 `wakes_min`~`wakes_max`（5–6）之间
2. 每个点用 cron 的一次性任务触发，派发任务：
   - **主任务**：刷 B 站（`watch_min`~`watch_max` 个视频）
   - 第 1 次醒来额外：查 QQ / B 站消息（当天只做一次）
   - 每次醒来：主动关心判据（由它自己判断该不该发，`care_every` 控制频率）
   - 最后一次醒来：写日记（`diary_on_last`）
3. 错过补跑：窗口外错过但还在 `catchup_minutes` 内的点会补

> 注：`main.py` 顶部的 docstring 还写着旧设计（08:00–23:00、8–12 次），
> 以 `config.json` 的 `windows` / `wakes_min` / `wakes_max` 为准 —— 现在就是 12–14 与 18–24、5–6 次。

## 命令

`/wake`：看今天的醒来表（每个点的时刻、任务、是否已执行）。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| target_session | 私聊 umo | 在哪个会话里醒 |
| windows | `["12:00-14:00","18:00-24:00"]` | 醒来窗口 |
| wakes_min / wakes_max | 5 / 6 | 每天醒几次 |
| min_gap_minutes | 25 | 两个点最小间隔 |
| watch_min / watch_max | 1 / 4 | 每次看几个视频 |
| care_every | 1 | 每几次醒来做一次主动关心 |
| diary_on_last | true | 最后一次醒来写日记 |
| catchup_minutes | 见配置 | 错过多久内还补 |
| note_watch / note_care / note_first / note_diary | 提示词 | 四类任务的指令文本 |

## 数据

- `data/plan.json`：当天的醒来表（每个点的时刻、任务、done/fired_at/skipped）

## 联动

- 排班由本插件负责，业务由 `astrbot_plugin_bili_activity`（刷视频/评论）、`astrbot_plugin_proactive_chat`（主动关心）、
  `astrbot_plugin_mood`（日记）执行
- 调度失败或后端重启不影响已定的点（cron 存在 AstrBot 的 jobs 表里）

## 已知边界

- AstrBot 没跑的时候到点不会触发，只能靠 `catchup_minutes` 补
- 一天的表只在当天有效，跨天重排
