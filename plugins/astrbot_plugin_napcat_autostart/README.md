# NapCat 自启动（astrbot_plugin_napcat_autostart）

AstrBot 加载完成后，自动把 NapCat 拉起来，让机器人 QQ 上线 —— 不用每天手点启动器。

## 它做什么

用 `@filter.on_astrbot_loaded()` 钩子，等 AstrBot 加载完、延迟 `start_delay_seconds` 秒后：

1. 如果 `kill_qq_before_start=true`，先杀掉正在跑的 `QQ.exe`
2. 用 `NapCatWinBootMain.exe` 注入启动 QQ：
   `NapCatWinBootMain.exe "<qq_path>" "<napcat_dir>\NapCatWinBootHook.dll" -q <qq_number>`
3. 启动前会设置五个环境变量（**缺一个 NapCat 后端就不会加载**）：
   `NAPCAT_PATCH_PACKAGE` / `NAPCAT_LOAD_PATH` / `NAPCAT_INJECT_PATH` / `NAPCAT_LAUNCHER_PATH` / `NAPCAT_MAIN_PATH`

## 命令

`/napcat`：看状态（NapCat 是否在跑、QQ 是否在跑、上次启动结果），也可手动拉起。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| enabled | true | 总开关 |
| napcat_dir | `<NapCat 安装目录>` | NapCat 目录 |
| qq_number | `123456789` | 机器人 QQ |
| qq_path | `C:\Program Files\QQ.exe` | QQ 主程序 |
| kill_qq_before_start | true | 启动前先杀 QQ |
| start_delay_seconds | 6 | 延迟多久再拉 |
| extra_args | "" | 追加给 NapCatWinBootMain 的参数 |

## 已知边界

- 仅 Windows：依赖 `NapCatWinBootMain.exe` + `NapCatWinBootHook.dll`
- 首次登录要扫码；已有快登 token 时 `-q` 能自动登录
- 启动前必须没有 QQ 在跑，否则会把 NapCat 注入到你当前登录的号上
- NapCat 崩了不会重启（只负责开机那一次）
