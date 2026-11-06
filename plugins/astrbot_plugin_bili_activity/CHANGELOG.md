## 2.4.4 — 2026-11-06 00:35
修「搜到了但记不上分区」：B 站新版 `view` 接口 `tname` 常是空串、`tid` 换成新体系号（221 野生动物、250 出行、208 校园学习…），`TID_ZONE` 只认老号，导致「搜完再校验分区」永远不成立、每次都退回分区排行榜，抓到的视频和兴趣点无关。

### 改
- `BiliClient.search()` 保留搜索结果自带的 `typeid` / `typename`
- 新增 `zone_from_type(typename, typeid)`：先查 `TID_ZONE`，再用 `TYPE_HINTS` 把「野生动物 / 美食制作 / 人文历史 / 手工 / 校园学习…」归到我们用的分区名
- `bili_watch` 的兴趣点分支改成：关键词搜 → 用 `typename` 确认分区 → 从对得上的前 8 条里随机；一条都对不上才退回排行榜（分区对、话题随机）

# CHANGELOG

## 2.4.3 — 2026-11-06 00:27
配合兴趣点引擎 v1.2.0：`bili_watch` 不给 BV 时，若选中「兴趣点分区」，先拿该兴趣点的 `keywords` 去 B 站搜一条没看过的，搜不到才退回分区排行榜。避免按分区随机抓到跟兴趣点无关的视频。

## 2.4.2 — 2026-11-05 21:05
主人要求去掉「总闸」`comment_paused`。

### 删
- `DEFAULTS["comment_paused"]` 与 `config.json` 里的同名键
- `bili_comment`、`bili_reply` 开头那两道「评论先停了 / 回复也先停了」的挡板

### 行为变化
- 不再有一键停发开关；要临时停，直接改 `per_video_comment_limit`、冷却或把插件禁用
- 每个视频 20 条、8 秒冷却、字数与 AI 味检查照旧

## 2.4.1 — 2026-11-05 21:00
按主人要求，删掉「陌生人回复先存草稿」这套机制。

### 删
- `DEFAULTS["reply_draft_for_strangers"]` 与 `config.json` 里的同名键
- `bili_reply` 里的存草稿分支（原来非主人回复会写进 `data/reply-drafts.json` 等人审）
- `/bili drafts`、`/bili approve <id>` 两个子命令，以及 `DRAFTS_PATH` 常量

### 行为变化
- 现在回谁都是直接发（主人、陌生人一视同仁），仍然走每视频额度 `per_video_comment_limit`、8 秒冷却、字数与 AI 味检查
- `data/reply-drafts.json` 从未生成过，无历史数据需要清理

## 2.4.0 — 2026-11-05
省token第二刀：新增 `bili_digest`，一次把看视频要的东西拿齐（方案 7 第二期）。

### 新增
- **`bili_digest` 工具**：包一层 `bili_digest.py`（放在工作目录的 `bili-watch` 下）。一次调用返回：标题/UP/分区/简介、字幕（可抽样）、弹幕热区（10 秒分桶取最密几段）、热评（带 rpid）、这条视频自己说过什么；可选 `listen=N` 顺手听音轨、`want_frames=true` 顺手抽三张图
- 参数：`bv`、`subs`(默认 15)、`hot`(默认 3)、`dan`(默认 5)、`listen`(默认 0)、`want_frames`(默认 false)
- 结果走 `--out` 落盘再读 utf-8，不吃控制台编码；超过 6000 字截断并提示改用 `bili_comments` / `bili_subtitles` 补细节
- 超时 4 分钟收手；脚本缺失时自动提示退回 `bili_watch` + `bili_subtitles` + `bili_danmaku`
- 跑在 `asyncio.to_thread` 里，不卡事件循环；顺手记 2 点读取额度

### 为什么
- 原来「看一条」要串 3~5 步（`bili_watch → bili_subtitles/bili_listen → bili_danmaku → bili_comments`），每步 0.04~0.07 元
- 现在一步出齐，实测默认 1.4~2.0 秒、约 1000~1300 字；加听音轨 5~9 秒；加抽帧 15~20 秒
- 脚本那边的坑与体检都记在笔记目录的 `bili-digest-2026-11-05.md`

## 2.3.1 — 2026-11-05
省token第一刀：把工具说明压短、把一次看视频要走的步数压少。

### 改
- **工具说明 2496 字 → 1713 字**（省 783）：`bili_comment` 341→229、`bili_reply` 334→239、`bili_listen` 271→139、`bili_frames` 232→113，其余同样压到一两行。这些说明每一步请求都要重读，压一次省一路
- **`bili_watch` 加两个参数**：`subs`（默认 15 句官方字幕，0 不带，999 全要）、`hot`（默认 3 条热评，0 不带）。原来要另点 `bili_subtitles` 才看得到台词，现在打开视频就顺手带上，省一到两步
- **热评默认从 8 条降到 3 条**：要更多显式传 `hot=N`
- **报错带提示**：`bili_reply` 撞 12006 会直接说「多半是评论被删或没给 root，重新找 rpid 再把主楼 rpid 填进 root」；-400 会说「检查 rpid 是不是纯数字、正文有没有超字数或带链接」。说明压短了，细节搬到这里，只在错时付钱
- `subs` / `hot` 传空串或字符串都不会炸，内部自己转数

### 为什么
- 实测一轮醒来 9~17 步、每步 0.04~0.07 元，钱花在「步数 × 上下文」上，不在图上
- 这一步估摸一轮省 0.03~0.12 元；真数要拿同一条视频改前改后对照

## 2.3.0 — 2026-11-05
主人在晚间低谷期把发评论的牙口松开了，插件端与脚本端一起改。

### 改
- **每视频互动上限 6 → 20 条**：`per_video_comment_limit` 由 6 改成 20；回主人仍不占额度
- **两条间隔 1 分钟 → 8 秒**：`min_interval_minutes` 记 0.14（脚本按秒取整得 8 秒），`comment_cooldown_*` 与 `reply_cooldown_*` 同步收到 0.14
- **字数上限 120 → 250**：`max_chars` 改成 250；`post_comment.py` 不再写死 120，改成读 `max_chars`，默认 250
- **间隔提示改按秒**：以前 8 秒的等待会被显示成「0 分钟」，现在直接报秒数
- **任务话术同步**：主楼/回复的额度说明、字数说明跟着改口径，不再留着「每天最多 6 条、隔 25 分钟」这类过期话

### 没动
- 日上限仍然不限；同一视频重复内容照旧会被挡
- 4 字下限、带链接/QQ号/敏感词拒发这两条不动


## 2.2.0 — 2026-11-04
- 新增 `bili_listen` 工具：把视频音轨拉到本机，转成带时间戳的文字，并给情绪标签（开心/生气/难过/平静…）与事件（说话/笑声/掌声/背景音乐）
- 声音特征本地算：平均与最高音量、安静段、最响处、BPM、打击感、明亮度；给人话总结（「鼓点中等；节奏快；有安静段」）
- 「听」这条链路全在本机跑（ffmpeg 抽音轨 + 本地 `asr` 目录里的 SenseVoice int8），不花token；只把压缩后的文字返回，token 可控
- 新增 `listen.py`（按文件路径加载，跟 `frames.py` 一样独立可测）；`BiliClient` 增加 `audio_url()`，单独取音频流，比拖整条视频省流量
- 默认听 120 秒，最多 300 秒；可指定从第几秒开始，返回的时间戳是视频里的绝对秒数
- 临时 wav 用完即删，不占盘

# 更新日志 · B站活动（astrbot_plugin_bili_activity）

## v2.1.0 — 2026-11-03

新增「看一眼画面」的能力：bili_frames。

### 新增
- **bili_frames 工具**：铺底拼图 + 连拍 + 残影 + 本地 OCR，全部本机跑，不吃token
- **frames.py 独立模块**：取流、抓帧、拼图、残影、相似帧去重、OCR 都在里面，可以单独测
- **BiliClient.playurl**：取最低码率的 dash 流，抽帧不追清晰度，只追省流量

### 规格
- 长边固定 640：横屏一格 640x360、竖屏 360x640，拼图时统一成 480x480 的方格
- 铺底 4 帧（短）/ 9 帧（长），整张约 92 万像素、1200 token 上下
- 连拍每秒 10 帧、8 帧一组（0.1 秒间隔），格子 345 像素，整张约 95 万像素
- 残影把 6-8 帧按 45% 混合，看走向和轨迹
- 相似帧会自动往后挪一点重抓，避免四张一个样
- 超过 40 分钟的视频不抽（主人清单里点名的除外）；单次消耗 2 点读取额度

### 实测
- 107 秒竖屏萌宠视频：取流 + 4 帧铺底 + 8 帧连拍 + 残影 + OCR，共 9 秒
- OCR 能读出画面字幕（「我一把年纪了说点反话怎么了」这类），连拍能看出猫在走动

### v2.1.0 修掉的两个问题（2026-11-03 21:00 前后）
- **流比元数据短**：B 站给的那路流实际只有 17.5 秒，元数据却写 72 秒，按元数据取点会全落空（远程跳帧返回「No filtered frames」）。现在先探容器真实时长，再决定取点位置
- **同帧判定太狠**：连续帧太像时整格被丢掉，一条视频只剩一张。改成先往后挪点再抓，挪不动就用手里那张，不再丢格

## v2.0.0 — 2026-11-03

排班彻底改朝换代：刷视频的时间表从本插件里搬出去，交给 wake_plan 统一管。

### 变了什么
- **删掉整套自带排班**：避峰日历、活跃时间段、时间段切分（_allowed_spans / _active_window / _schedule_date）、名额计算（_count_for）、时刻表生成（_slots）、派发（_fire_activity）、排班线程（_scheduler 里的排班分支）全部移除
- **连同避峰一起删**：PEAK_SPANS_DEFAULT、CALENDAR_PATH、CALENDAR_URL、法定节假日抓取（_refresh_calendar）与节假日缓存
- **排班归 wake_plan**：一天在 12:00-14:00、18:00-24:00 两个时间窗里醒 5-6 次，每次看 1-2 个视频，单条超过 40 分钟的直接跳过（主人清单里点名的例外）
- **配置瘦身**：清掉 9 个旧键 —— schedule_enabled、active_from、active_to、offpeak_enabled、peak_spans、target_max、min_gap_minutes、catchup_minutes、activity_prompt
- **命令瘦身**：/bili run（手动排一次）与 /bili 状态里的排班显示一并去掉；status / login / emoji / drafts / approve 保留
- **提示词归位**：刷视频的规则说明不再由本插件下发，改由 wake_plan 的 note_watch 提供（含先看 @、弹幕先本地去重聚热区再读）

### 留下了什么
- 每日登录打卡（_daily_login）、@ / 回复轮询（_poll_mentions / _mention_loop）、pending 事件清理（_drain_pending）
- 14 个工具全部保留：bili_owner_list、bili_watch、bili_comments、bili_comment、bili_reply、bili_exp、bili_share_qq、bili_share、bili_mentions、bili_mention_done、bili_subtitles、bili_danmaku、bili_fav、bili_owner
- _sched 精简成只记「每日打卡日期」，不再生成时刻表；wake_plan 调用的 _sched / _daily_login 接口保持不变

### 升级须知
- 需要重载本插件与 wake_plan 才生效
- 旧配置里被删掉的键不再读取，留着也不会报错
- 备份：工作目录下的 `backup\`

## v1.3.0 及更早
- 自带排班 + 避峰 + 每日登录 + 刷视频/评论/回复/分享/收藏那套工具
