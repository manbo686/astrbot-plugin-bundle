# B站活动 · astrbot_plugin_bili_activity

> 让它在 B 站过日子：自己挑视频看、读字幕、听音轨、回评论，把值得的说给主人听。
>
> 版本 `v2.4.2` · 作者 DSH · 需要 AstrBot `>= 4.0.0`

---

## 它做什么

它不是「定时发评论的机器人」。

它给机器人的是**在 B 站生活的十个动作**：打开一个视频、把字幕和评论读进来、听一段声音、看几帧画面、
回一句话、把喜欢的东西收藏、把好玩的讲给主人听。

**排班不归它管。** 2.0 起「它什么时候醒、醒来做什么」交给 `wake_plan`；
这个插件只负责三件常驻小事（每日打卡、抓 @、清事件队列）和全部内容动作。

三条设计原则：

1. **只给能力，不给日程**——什么时候看什么由上层策略决定，插件不做自己的定时器排班。
2. **每个动作都有刹车**——额度、冷却、字数、AI 味、分享欲，一道道闸门。
3. **说过什么能查**——评论记账、状态、@ 回执都落盘，随时 `/bili` 能看。

---

## 一、它能做的 17 件事（LLM 工具）

### A. 看：把视频读进上下文

| 工具 | 用途 | 备注 |
|---|---|---|
| `bili_owner_list` | 主人留的「下次先看」清单 | 不占今日额度，看完自动划掉 |
| `bili_watch` | 打开视频：标题 / UP / 简介 / 热评，顺手带几行字幕 | 不给 `bv` 就按兴趣分区挑一个 |
| `bili_digest` | **一次拿齐**看视频所需的全部材料 | 字幕 + 弹幕热区 + 热评 + 自己说过什么 |
| `bili_comments` | 读评论（`hot` / `new`） | 用来决定回哪条 |
| `bili_subtitles` | 官方字幕，带时间戳 | 只有官方提供了字幕的视频才有 |
| `bili_danmaku` | 弹幕热区（哪几秒最热闹） | 找「值得看的那一下」 |
| `bili_frames` | 抽帧：铺底拼图 + 连拍 + 残影 + OCR | **本机跑**，不花token |
| `bili_listen` | 听音轨：转写 + 情绪 / 节拍 / 音量 | **本机 SenseVoice**，最长 300 秒 |
| `bili_exp` | 查等级经验，顺手打一次观看心跳 | 让「看过」这件事真的记账 |

`bili_digest` 是最常用的那一个：默认 **1.4–2.0 秒 / 1000–1300 字**；
加 `listen` 多 5–9 秒，加抽帧多 15–20 秒；超过 6000 字自动截断。

### B. 说：把想法留在评论区

| 工具 | 用途 | 刹车 |
|---|---|---|
| `bili_comment` | 发主楼评论 | 4–250 字；`to_owner` 开头 @主人且不占额度 |
| `bili_reply` | 回某个人的评论 | 4–250 字；回谁都可以，仍受每视频额度与冷却约束 |
| `bili_share` | 把好视频分享给主人 | 每天 ≤3 次，且要过「分享欲」阈值 |
| `bili_share_qq` | 直接发到 QQ | 同上，走另一条通道 |
| `bili_fav` | 收藏视频 | 理由必须 ≥6 个字，每天 ≤3 条 |

评论内容会先过 **AI 味评分**（`ai_flavor_threshold`，默认 6）：太像「AI 在营业」的会被拦下重写。

### C. 关系：认得主人，接得住 @

| 工具 | 用途 |
|---|---|
| `bili_owner` | 报出主人在 B 站的账号（UID + 昵称），评论里标【主人】 |
| `bili_mentions` | 看主人有没有 @ 它、回了它 |
| `bili_mention_done` | 处理完一条 @ 后记账，避免重复 |

---

## 二、刹车：它不能乱来

| 层 | 配置项 | 线上值 | 作用 |
|---|---|---|---|
| 每视频互动数 | `per_video_comment_limit` | **20** | 同一个视频下最多互动多少条 |
| 间隔与冷却 | `min_interval_minutes` / `comment_cooldown_*` / `reply_cooldown_*` | **0.14 分 ≈ 8 秒** | 连续动作之间喘口气 |
| 字数 | `min_chars` / `max_chars` | **4 / 250** | 不许「+1」，也不许写小作文 |
| 读取额度 | `read_hourly_limit` / `read_daily_limit` | **240 / 1200** | 一小时、一天最多读多少次，防刷 |
| 跳过率 | `skip_chance` | **0.3** | 三成的视频看完就走，不必每条都说话 |
| AI 味 | `ai_flavor_threshold` | **6** | 评分 ≥6 直接拒（`main.py:1109`） |
| 分享欲 | `share_min_urge` | **0.5** | `valence×0.45 + affinity×0.45 + (100−stress)×0.10`（`main.py:760`） |
| 收藏 | `fav_daily_limit` | **3** | 外加理由 ≥6 字 |
| 主人清单 | `owner_queue_max` | **30** | 主人留的待看最多堆 30 个 |

---

## 三、后台常驻在做什么

| 循环 | 做什么 | 位置 |
|---|---|---|
| 每日打卡 | 访问 B 站首页 + 领每日登录奖励，记 `schedule.json` 的 `login_date` | `_daily_login`（`main.py:1640`） |
| @ 轮询 | 抓 `mentions.json` 里主人的 @ / 回复，转成待办 | `_mention_loop` |
| 清事件队列 | 每 60 秒清一次外部脚本投递的 `pending_events.jsonl` | `_scheduler` |
| **不排班** | 什么时候醒来、醒来做什么 → 交给 `wake_plan` | — |

启动日志：`[bili_activity] 已启动（每日打卡 + @ 轮询 + 清 pending）；排班由 wake_plan 负责`

---

## 四、和别的插件怎么联动

| 对方 | 怎么联动 |
|---|---|
| `astrbot_plugin_mood`（心情引擎） | `_mood_category()` 决定表情池；分享欲用 valence / affinity / stress 现算 |
| `astrbot_plugin_bili_interest`（兴趣点引擎） | 按分区预算挑视频；发完 / 看完回报 `used`，额度不结转 |
| `wake_plan` | 负责「什么时候醒」；本插件不建定时任务 |
| `bili-watch/post_comment.py`（外部脚本） | 往 `pending_events.jsonl` 投递事件，由本插件消费 |
| `data/watch/mentions.json`（可用 `BILI_WATCH_DIR` 改） | 主人 @ / 回复的待办来源 |

---

## 五、命令

```
/bili                 # 状态：今日条数、累计评论/回复/失败、今日分区、主人信息
/bili login           # 立刻打卡（清 login_date 重跑）
/bili emoji           # 当前心情对应的表情池 + 随机颜文字
```

> 旧文档里的 `/bili plan`、`/bili run` 已废弃——排班交给 `wake_plan`。

---

## 六、数据落盘（`data/`）

| 文件 | 内容 |
|---|---|
| `comment-log.json` | 今天发了多少条、发在哪些 BV |
| `stats.json` | 累计评论 / 回复 / 失败 |
| `schedule.json` | 打卡日期等 |
| `fav-log.json` / `share-log.json` | 收藏、分享的每日计数 |
| `owner_queue.json` | 主人留的待看清单 |
| `seen_mentions.json` | 已处理的 @ 回执 |
| `traffic.json` | 读取额度（小时 / 日） |
| `pending_events.jsonl` | 外部脚本投递的事件队列 |
| `digest-BV*.txt` | `bili_digest` 的缓存材料 |

---

## 七、版本沿革

| 版本 | 变化 |
|---|---|
| 2.0 | 排班交给 `wake_plan`，插件只留打卡 / 抓 @ / 发内容 |
| 2.2.0 | 新增 `bili_listen`（ffmpeg 抽音轨 + 本机 SenseVoice） |
| 2.3.0 | 每视频互动 6 → **20** 条，间隔 25 分钟 → **8 秒**，字数 120 → **250** |
| 2.3.1 | 工具说明从 2,496 字压到 1,713 字；`bili_watch` 增加 `subs` / `hot` 参数 |
| 2.4.0 | 新增 `bili_digest`：一次拿齐，默认 1.4–2.0 秒 / 1000–1300 字 |
| 2.4.1 | 删掉「陌生人回复先存草稿」机制（`reply_draft_for_strangers`、`/bili drafts`、`/bili approve`） |
| 2.4.2 | 删掉「总闸」`comment_paused`，不再有一键停发开关 |

---

## 八、已知边界

1. **分区识别靠不住**：B 站新接口的 `tname` 常返回空串，只能靠 `TID_ZONE` 的 tid 映射兜底，命中率有限（例如 Arcaea 联动没被记进「音乐」）。
2. **字幕只有官方的**：UP 没传字幕就是没有，不做事后 ASR（要听内容用 `bili_listen`）。
3. **「已看额度」依赖兴趣点插件**：本插件只负责回报，额度怎么算在 `astrbot_plugin_bili_interest`。
4. **抽帧 / 听音吃本机 CPU**：`bili_digest` 加 `listen` 多 5–9 秒、加抽帧多 15–20 秒。
5. **旧版本遗留**：`data/peak_calendar.json` 是早期排班留下的文件，当前代码不再读取，删不删都行。

## 配置

插件不带默认配置文件也能跑：代码里有一套默认值，缺项时自动使用。

想改配置有两种方式：

1. 面板 → 插件 → 该插件 → 设置（推荐，改完即时生效）
2. 复制 `config.example.json` 为插件目录下的 `config.json`，按需修改

`config.example.json` 里空字符串或 `0` 的字段是「跟机器有关」的值（会话、QQ 号、
本机路径），需要填成你自己的；其余是功能参数，可以先不动。
### 主要配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `target_session` | 空 | 收报告的会话（留空就用主人私聊） |
| `owner_qq` / `owner_mid` / `owner_name` | `0` / `0` / 空 | 认出「谁是主人」：QQ 号、B 站 UID、B 站昵称 |
| `owner_umo` | 空 | 分享和报告发到这个会话 |
| `cookie_path` | 空 | 默认找 `BILI_WATCH_DIR/cookie.json` |
| `auto_login` | `true` | 每天自动打卡领经验 |
| `mention_poll` | `true` | 轮询主人的 @ 和回复 |
| `per_video_comment_limit` | `20` | 同一个视频下最多回几条 |
| `min_interval_minutes` | `0.14` | 两条动作之间的最小间隔（分钟） |
| `comment_cooldown_min/max` | `0.14` | 评论前的随机冷却区间 |
| `reply_cooldown_min/max` | `0.14` | 回复前的随机冷却区间 |
| `min_chars` / `max_chars` | `4` / `250` | 正文长度 |
| `allow_emoji` / `allow_kaomoji` | `true` | 允许带表情 / 颜文字 |
| `ai_flavor_threshold` | `6` | 「AI 味」评分阈值，≥ 这个分就不发 |
| `skip_chance` | `0.3` | 看到不感兴趣的内容时跳过的概率 |
| `hot_comment_count` | `8` | `bili_watch` 默认带几条热评 |
| `daily_share_limit` | `3` | 每天最多主动分享几次 |
| `share_via` | `comment` | 分享方式：在评论区 @ 主人 |
| `share_min_urge` | `0.5` | 分享冲动阈值 |
| `owner_queue_max` | `30` | 主人待看清单上限 |
| `fav_daily_limit` / `fav_folder_id` | `3` / `0` | 每天最多收藏几个、收藏到哪个夹 |
| `read_hourly_limit` / `read_daily_limit` | `240` / `1200` | 每小时 / 每天能读多少次接口（防刷） |

其余键见 `config.example.json`，一般不用动。
