# 屏幕控制（astrbot_plugin_screen_control）

给机器人一个「关屏幕 / 亮屏幕」的开关。**只关显示器**，并且关屏期间**顶住系统待机**，CPU、网络、QQ、AstrBot 全部照常运行。

## 怎么用

在 QQ 里直接说就行（要管理员/主人）：

| 说法 | 效果 |
|---|---|
| `/息屏`、`/熄屏`、`/关屏` | 关闭显示器 |
| `/亮屏`、`/开屏` | 点亮显示器 |
| `/屏幕` | 看插件状态和用了几次 |
| 「帮我把屏幕关了」 | 它调 `screen_control` 工具关屏 |

关屏会**延迟 1.5 秒**执行，让它把回复先发出去，然后屏幕才黑。

## 亮屏是怎么做到的

发 `SC_MONITORPOWER = -1` 唤醒显示器；个别显示器不认这条指令，所以另外补一记**零位移鼠标事件**（不会移动光标）。如果还是没亮，说明显示器自己进了深度节能，动一下鼠标或按任意键即可。

## 安全边界

- 只关显示器；关屏期间会持有 `ES_SYSTEM_REQUIRED`（常驻线程每 20 秒续一次），**不会**进入 Modern Standby 低功耗待机，亮屏或插件卸载时自动释放。
- 默认**仅管理员**可用：`admin_only: true` 时只认 AstrBot `admins_id` 里的账号。
- 想放行别人，把 QQ 号填进 `allowed_user_ids`。
- 改屏幕不会写任何系统设置，也不会影响正在跑的任务。

## 配置（WebUI 插件配置页）

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `admin_only` | true | 仅管理员 |
| `allowed_user_ids` | [] | 额外放行的 QQ 号 |
| `delay_seconds` | 1.5 | 关屏延迟 |
| `wake_fallback` | true | 亮屏时补零位移鼠标事件 |
| `prevent_standby` | true | 关屏期间防止系统待机（Modern Standby 机型必开） |
| `llm_tool_enabled` | true | 允许自然语言调用 |
| `reply_off` / `reply_on` / `reply_denied` / `reply_unsupported` | — | 四种回复文案 |

## 平台

只在 **Windows** 上生效；其他平台会回复「这台机器不是 Windows」。

## 数据

`data/stats.json` 记录关屏/亮屏次数、最后一次操作时间与操作者 QQ。

