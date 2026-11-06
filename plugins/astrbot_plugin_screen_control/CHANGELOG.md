# 更新日志

## v1.0.0
- 首版：`/息屏` `/亮屏` `/屏幕` 三个命令 + `screen_control` LLM 工具
- 用 `SendMessageTimeoutW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER)` 只关显示器
- 关屏后可用 `mouse_event` 补一次输入唤醒（`wake_fallback`）
- 防待机：`prevent_standby=true` 时用 `PowerCreateRequest(PowerRequestSystemRequired)`
  加一个每 20 秒 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` 的常驻线程，
  先把系统待机顶住再灭屏，亮屏/卸载时释放
