# AstrBot 插件合集（自用）

给 AstrBot 写的一批自动化插件：让 QQ 里的机器人自己在 B 站活动、按兴趣点挑视频、维护心情状态、在合适的时刻醒来主动说话。

这些插件都是给一只具体的机器人写的，抽出来后可以给别人用，但**默认值全是空的**，装好后需要在 `config.json` 里填自己的会话、QQ、路径。

仓库：<https://github.com/manbo686/astrbot-plugin-bundle> ｜ 下载：<https://github.com/manbo686/astrbot-plugin-bundle/releases>

## 包含哪些

| 目录 | 名称 | 版本 | 做什么 |
|---|---|---|---|
| `astrbot_plugin_bili_activity` | B站活动 | 2.4.4 | 在 B 站刷视频、读字幕/弹幕、抽帧、听音轨，会发评论和回复，带表情与颜文字 |
| `astrbot_plugin_bili_interest` | 兴趣点引擎 | 1.2.0 | 每天生成一批兴趣点，把视频额度按权重分到分区，另发探索位、热门位与 DeepSeek 位；带面板 |
| `astrbot_plugin_exec_guard` | 执行权限守卫 | 1.0.0 | 只在指定会话放行本机执行/文件/远程控制类工具，其它会话自动移除 |
| `astrbot_plugin_idle_compact` | 闲置边界压缩 | 1.0.0 | 只在会话闲置时压缩历史，先归档再写回，不打断前缀缓存；被压掉的原文可检索 |
| `astrbot_plugin_mood` | 心情引擎 | 2.0.0 | 六维心情状态机 + 昼夜节律与疲劳 + 29 个情绪词 + 每轮至多三行语气注入 + 每日日记 + 面板 |
| `astrbot_plugin_napcat_autostart` | NapCat 自启动 | 1.0.0 | AstrBot 启动后自动拉起 NapCat，让机器人 QQ 上线 |
| `astrbot_plugin_proactive_chat` | 主动聊天 | 1.0.0 | 上一次对话结束后不定时主动找人说话，可按心情与兴趣点选题 |
| `astrbot_plugin_screen_control` | 屏幕控制 | 1.0.0 | 只关显示器不睡眠，关屏期间顶住系统待机，进程与网络照常 |
| `astrbot_plugin_token_meter` | Token 电表 | 1.0.0 | 记录每次模型调用的 token、缓存命中与请求结构，按天落盘并估算花费 |
| `astrbot_plugin_wake_plan` | 醒来时刻表 | 1.0.3 | 把定时任务统一挂到「醒来点」上：按时间窗排若干次醒来，每次派发不同的事 |

## 安装

三种方式，选一种：

1. **下载 zip 安装**：到 [Releases](https://github.com/manbo686/astrbot-plugin-bundle/releases) 下载对应插件，AstrBot WebUI →「插件」→「安装」→ 上传 zip（zip 根目录就是插件名）
2. **手动解压**：把插件目录整个放到 `data/plugins/` 下，重启或重载插件
3. **从仓库拷**：`cp -r plugins/<name> <AstrBot>/data/plugins/`

## 配置

每个插件目录里有 `config.example.json`，复制成 `config.json` 再按需改：

```bash
cd data/plugins/astrbot_plugin_bili_activity
cp config.example.json config.json
```

也可以装好后在面板里改，改完记得重载插件。

**要填的几处**：
- `target_session` / `owner_umo` / `sessions`：会话标识，格式 `平台:消息类型:ID`，例如 `qq:FriendMessage:123456789`
- `owner_qq` / `qq_number`：QQ 号
- 各种路径：视频笔记目录、NapCat 安装目录、ffmpeg 等

## 环境变量（可选）

| 变量 | 用在 | 说明 |
|---|---|---|
| `BILI_WATCH_DIR` | B站活动 | cookie.json、mentions.json、pending_events.jsonl 所在目录；不填用插件自己的 data/watch |
| `FFMPEG_PATH` | B站活动 | ffmpeg.exe 路径；不填先找 AstrBot venv 同目录，再找插件目录 |
| `ASR_PYTHON` | B站活动 | 本地转写用的 python.exe（装了 faster-whisper / SenseVoice） |
| `ASR_AUDIO_PROBE` | B站活动 | 抽音轨特征的 audio_probe.py 路径 |
| `ASR_SENSEVOICE_DIR` | B站活动 | SenseVoice 模型目录 |

## 依赖

- AstrBot **>= 4.0.0**
- Python 3.12（跟 AstrBot 自带的一致）
- 第三方库：`requests`、`Pillow`、`psutil`（B站活动抽帧与进程检测用）
- 外部程序：`ffmpeg`（B站活动抽帧、听音轨用）；不在 PATH 里就用环境变量 `FFMPEG_PATH` 指定
- 平台限制见下方「平台」一节

## 可选的外部脚本

有几个能力由本机的小脚本提供，**不在这个仓库里**，不装也能用：

| 脚本 | 放在哪 | 作用 | 没有时 |
|---|---|---|---|
| `bili_digest.py` | `$BILI_WATCH_DIR`（默认插件自己的 `data/watch`） | `bili_digest` 工具的实现：一次拿齐字幕、弹幕热区、热评 | 调这个工具会回「本机没找到 bili_digest.py」，改用 `bili_watch` + `bili_subtitles` 自己拼 |
| `post_comment.py` | 任意位置，自己定时跑 | 往插件 `data/pending_events.jsonl` 投递「评论已发出」事件，供插件做统计 | 评论照发，只是统计里少一条 |
| `check_idle.py` | 任意位置 | 醒来前判断主人是否在线，决定这次要不要主动关心 | `wake_plan` 的提示词里提到它，不用就在 note 里删掉那行 |

写自己的脚本时，`pending_events.jsonl` 每行一个 JSON，字段至少要有 `kind` 与 `bv`；`kind` 支持 `comment`、`reply`、`share`。

## 插件之间怎么联动

多数插件单独也能跑，装了邻居就多一层味道。**所有跨插件调用都包在 `try/except` 里**，对方没装、崩了或被卸载，这边只会退化成默认行为，不会连带报错。

| 谁 | 看谁 | 拿什么 | 对方不在时 |
|---|---|---|---|
| 醒来时刻表 `wake_plan` | 心情引擎 `mood` | 当前心情标签与六维数值 | 提示词里不出现心情行 |
| 醒来时刻表 `wake_plan` | 兴趣点引擎 `bili_interest` | 今天剩余额度、分区预算 | 退回「这次看 1–4 个视频」的写法 |
| 醒来时刻表 `wake_plan` | B站活动 `bili_activity` | 每天第一次醒来补一次登录 | 登录交给 B站活动自己的定时器 |
| B站活动 `bili_activity` | 心情引擎 `mood` | 心情类目，用来挑表情 | 用默认表情池 |
| B站活动 `bili_activity` | 兴趣点引擎 `bili_interest` | 下一个该去的分区 | 只按指定 BV 或热门刷 |
| 兴趣点引擎 `bili_interest` | 心情引擎 `mood` | 心情快照，影响当天清单的语感 | 用中性提示词 |
| 主动聊天 `proactive_chat` | 心情引擎 + 兴趣点引擎 | 六维数值、今天的分区预算 | 只按概率与时间窗触发 |
| 心情引擎 `mood` | LivingMemory（第三方） | 把每日日记写成一条长期记忆 | 日记只留在插件里 |

## 平台

- 全部插件在 **Windows** 上开发与测试
- `screen_control`、`napcat_autostart` 用了 Win32 API，只能在 Windows 跑
- `bili_activity` 的抽帧/听音轨依赖外部 `ffmpeg`，其它系统把路径填进 `FFMPEG_PATH` 一般也能用，但没测过

## 不包含什么

这个仓库**只有插件代码**，没有：

- 任何聊天记录、记忆库、日记、归档（`data/` 一律不在仓库里）
- `config.json`（只给模板），没有 cookie、QQ 号、路径、密码
- 别人写的插件（下面单独列了上游链接）

用到的第三方插件（请去上游装，不要从本仓库拿）：

| 插件 | 上游 |
|---|---|
| Bilibili | <https://github.com/Soulter/astrbot_plugin_bilibili> |
| biliVideo 视频总结 | <https://github.com/anka-afk/astrbot_plugin_biliVideo> |
| LivingMemory | <https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory> |
| 表情包管理器 | <https://github.com/anka-afk/astrbot_plugin_meme_manager> |
| 群聊唤醒窗口 | <https://github.com/PingPangJiao/astrbot_plugin_group_window> |
| 情绪表情包 | <https://github.com/PingPangJiao/astrbot_plugin_mood_meme> |
| 启动时打开 WebUI | <https://github.com/PingPangJiao/astrbot_plugin_open_webui> |
| 话题接入 | <https://github.com/PingPangJiao/astrbot_plugin_topic_bridge> |

## 免责

自用工具，按自己需要改的，接口和配置随时可能变；拿去用请先小范围试，尤其是会自动发评论、自动执行命令的那几个。

## 许可

AGPL-3.0，见 [LICENSE](LICENSE)。

简单说：可以自由使用、修改、再分发；但**改了要开源**，如果把它做成网络服务给别人用，也要把改后的源码提供出来。商用同样受此约束。
