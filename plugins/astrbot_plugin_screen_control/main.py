# -*- coding: utf-8 -*-
"""屏幕控制插件（Android/Linux 上无效果，仅 Windows 生效）。

给机器人一个「关屏幕」的开关：
- 指令：/息屏、/亮屏（别名若干）
- LLM 工具：screen_control(action="off"|"on")，可以对它说「帮我把屏幕关了」

只关显示器，不睡眠、不休眠，进程与网络照常运行。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.json"
DATA_DIR = PLUGIN_DIR / "data"
STATS_PATH = DATA_DIR / "stats.json"
PLUGIN_NAME = "astrbot_plugin_screen_control"

DEFAULTS: dict = {
    "enabled": True,
    "admin_only": True,
    "allowed_user_ids": [],
    "delay_seconds": 1.5,
    "llm_tool_enabled": True,
    "wake_fallback": True,
    "prevent_standby": True,
    "reply_off": "好～屏幕关上了，电脑没睡，我还在线。",
    "reply_on": "屏幕点亮啦～",
    "reply_denied": "这个只能主人让我做哦。",
    "reply_unsupported": "这台机器不是 Windows，我关不了屏幕。",
}

# ---------- Windows 显示器电源 ----------
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
SMTO_ABORTIFHUNG = 0x0002

MONITOR_ON = -1
MONITOR_STANDBY = 1
MONITOR_OFF = 2

_user32 = None
if sys.platform == "win32":
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        _user32.SendMessageTimeoutW.restype = ctypes.c_size_t
    except Exception as exc:  # pragma: no cover
        logger.error(f"[screen] 初始化 user32 失败: {exc}")
        _user32 = None


def set_monitor_power(mode: int) -> bool:
    """mode: MONITOR_OFF(2) / MONITOR_ON(-1)。返回是否调用成功。"""
    if _user32 is None:
        return False
    result = ctypes.c_size_t(0)
    try:
        ok = _user32.SendMessageTimeoutW(
            wintypes.HWND(HWND_BROADCAST),
            wintypes.UINT(WM_SYSCOMMAND),
            ctypes.c_size_t(SC_MONITORPOWER),
            ctypes.c_ssize_t(mode),
            wintypes.UINT(SMTO_ABORTIFHUNG),
            wintypes.UINT(2000),
            ctypes.byref(result),
        )
        return bool(ok)
    except Exception as exc:  # pragma: no cover
        logger.error(f"[screen] 调用 SendMessageTimeoutW 失败: {exc}")
        return False


def nudge_input() -> bool:
    """发一个零位移的鼠标事件，避免某些显示器/唤醒策略不认 SC_MONITORPOWER。"""
    if _user32 is None:
        return False
    try:
        _user32.mouse_event(0x0001, 0, 0, 0, 0)  # MOUSEEVENTF_MOVE，dx=dy=0
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning(f"[screen] 模拟输入唤醒失败: {exc}")
        return False


# ---------- 关屏期间防待机 ----------
# 这台机器是 Modern Standby（S0 低电量待机）机型：屏幕一灭，Windows 很快就把整机
# 切到低功耗空闲，进程被节流——表现就是「关屏久了像掉线/不回消息」。
# SetThreadExecutionState(ES_SYSTEM_REQUIRED) 会持续重置系统空闲计时器，让系统在
# 关屏期间保持在工作状态；必须从常驻线程调用，线程退出即失效。
ES_SYSTEM_REQUIRED = 0x00000001
ES_CONTINUOUS = 0x80000000

# PowerSetRequest 的 System Required 请求（与播放器防止锁屏后断播是同一机制）
POWER_REQUEST_SYSTEM_REQUIRED = 1
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1


class _ReasonContext(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("Flags", wintypes.DWORD),
        ("SimpleReasonString", wintypes.LPWSTR),
    ]


_kernel32 = None
if sys.platform == "win32":
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.SetThreadExecutionState.argtypes = [wintypes.ULONG]
        _kernel32.SetThreadExecutionState.restype = wintypes.ULONG
        _kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
        _kernel32.PowerCreateRequest.restype = wintypes.HANDLE
        _kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
        _kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    except Exception as exc:  # pragma: no cover
        logger.error(f"[screen] 初始化 kernel32 失败: {exc}")
        _kernel32 = None


class KeepAwake:
    """在关屏期间顶住系统待机（Modern Standby / 空闲睡眠）。

    两道保险：
    1. PowerSetRequest(SystemRequired)：进程级「系统必须可用」请求，
       与音乐/视频播放器防止锁屏后断播是同一个机制；
    2. 常驻线程里每 20 秒续一次 SetThreadExecutionState(ES_SYSTEM_REQUIRED)，
       重置系统空闲计时器，进程/线程退出即自动失效。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._request = None

    @property
    def active(self) -> bool:
        return self._active

    def _acquire_request(self):
        if _kernel32 is None:
            return None
        try:
            reason = _ReasonContext()
            reason.Version = 0
            reason.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
            reason.SimpleReasonString = "AstrBot 关屏期间保持在线"
            handle = _kernel32.PowerCreateRequest(ctypes.byref(reason))
            if not handle:
                logger.warning("[screen] PowerCreateRequest 失败")
                return None
            if not _kernel32.PowerSetRequest(handle, POWER_REQUEST_SYSTEM_REQUIRED):
                logger.warning("[screen] PowerSetRequest 失败")
                _kernel32.CloseHandle(handle)
                return None
            return handle
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[screen] 电源请求异常: {exc}")
            return None

    def _release_request(self, handle) -> None:
        if _kernel32 is None or not handle:
            return
        try:
            _kernel32.PowerClearRequest(handle, POWER_REQUEST_SYSTEM_REQUIRED)
            _kernel32.CloseHandle(handle)
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            if _kernel32 is not None:
                try:
                    _kernel32.SetThreadExecutionState(
                        ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                    )
                except Exception:
                    pass
            self._stop.wait(20)
        if _kernel32 is not None:
            try:
                _kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            except Exception:
                pass

    def start(self) -> bool:
        if sys.platform != "win32" or _kernel32 is None:
            return False
        with self._lock:
            if self._active and self._thread and self._thread.is_alive():
                return True
            self._request = self._acquire_request()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="screen-keepawake", daemon=True
            )
            self._thread.start()
            self._active = True
        logger.info(
            "[screen] 已开启关屏防待机（系统保持可用，进程不被节流；"
            f"电源请求={'已持有' if self._request else '不可用，仅线程兜底'}）"
        )
        return True

    def stop(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._active = False
            self._stop.set()
            thread = self._thread
            self._thread = None
            handle, self._request = self._request, None
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._release_request(handle)
        logger.info("[screen] 已关闭关屏防待机")
        return True


KEEP_AWAKE = KeepAwake()


# ---------- 配置 ----------
def merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                cfg = merge(cfg, raw)
    except Exception as exc:
        logger.warning(f"[screen] 读取 config.json 失败，用默认值: {exc}")
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning(f"[screen] 写 config.json 失败: {exc}")


@register(
    PLUGIN_NAME,
    "DSH",
    "帮你关屏幕、亮屏幕：只关显示器不睡眠，关屏期间顶住系统待机，进程、网络与 QQ 照常运行。",
    "1.0.0",
)
class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._cfg_obj = config if isinstance(config, dict) else None
        self.raw_cfg = load_config()
        if isinstance(config, dict) and config:
            self.raw_cfg = merge(self.raw_cfg, config)
        self.conf = merge(DEFAULTS, self.raw_cfg)
        self._tasks: set[asyncio.Task] = set()
        self._last_action_ts = 0.0
        self._stats = self._load_stats()
        self._warned_unsupported = False

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        platform_ok = sys.platform == "win32" and _user32 is not None
        logger.info(
            f"[screen] 屏幕控制已加载（平台={sys.platform}，可用={platform_ok}，"
            f"管理员限定={self.conf.get('admin_only')}）"
        )

    async def terminate(self) -> None:
        KEEP_AWAKE.stop()
        for t in list(self._tasks):
            if not t.done():
                t.cancel()
        self._tasks.clear()

    # ---------- 统计 ----------
    def _load_stats(self) -> dict:
        try:
            if STATS_PATH.exists():
                raw = json.loads(STATS_PATH.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    return raw
        except Exception:
            pass
        return {"off": 0, "on": 0, "last_action": "", "last_ts": 0, "last_by": ""}

    def _save_stats(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATS_PATH.write_text(
                json.dumps(self._stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[screen] 写 stats.json 失败: {exc}")

    # ---------- 权限 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _uid(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _allowed(self, event: AstrMessageEvent) -> bool:
        uid = self._uid(event)
        extra = {str(x) for x in (self.conf.get("allowed_user_ids") or [])}
        if uid and uid in extra:
            return True
        if not self.conf.get("admin_only", True):
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ---------- 动作 ----------
    async def _apply(self, mode: int, delay: float, who: str, action: str) -> bool:
        """延迟 delay 秒后再关/开屏，保证回复先发出去。"""
        if delay > 0:
            await asyncio.sleep(delay)
        # 先顶住待机，再灭屏：避免"屏幕刚黑、系统就开始进 S0 低功耗"的空档
        keep = bool(self.conf.get("prevent_standby", True))
        if keep and mode == MONITOR_OFF:
            KEEP_AWAKE.start()
        ok = set_monitor_power(mode)
        if keep and mode == MONITOR_OFF and not ok:
            KEEP_AWAKE.stop()
        if keep and mode == MONITOR_ON:
            KEEP_AWAKE.stop()
        if ok and mode == MONITOR_ON and self.conf.get("wake_fallback", True):
            # 有些显示器/驱动不认 SC_MONITORPOWER=-1，补一个零位移鼠标事件
            await asyncio.sleep(0.3)
            nudge_input()
        now = time.time()
        self._last_action_ts = now
        self._stats[action] = int(self._stats.get(action, 0)) + 1
        self._stats["last_action"] = action
        self._stats["last_ts"] = int(now)
        self._stats["last_by"] = who
        self._save_stats()
        if ok:
            logger.info(f"[screen] 已执行 {action}（来自由 {who or '未知'}）")
        else:
            logger.warning(f"[screen] 执行 {action} 失败（平台不支持或 API 调用失败）")
        return ok

    def _schedule(self, mode: int, who: str, action: str) -> None:
        delay = max(0.0, float(self.conf.get("delay_seconds", 1.5) or 0))
        task = asyncio.create_task(self._apply(mode, delay, who, action))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _busy(self) -> bool:
        return (time.time() - self._last_action_ts) < 2.0

    # ---------- 指令 ----------
    @filter.command("息屏", alias={"熄屏", "关屏", "screenoff"})
    async def cmd_screen_off(self, event: AstrMessageEvent):
        """关闭显示器（电脑不睡眠，机器人继续在线）"""
        if not self.enabled:
            yield event.plain_result("屏幕控制插件是关着的哦。")
            return
        if not self._allowed(event):
            yield event.plain_result(str(self.conf.get("reply_denied", "这个只能主人让我做哦。")))
            return
        if sys.platform != "win32" or _user32 is None:
            yield event.plain_result(str(self.conf.get("reply_unsupported", "这台机器不是 Windows。")))
            return
        if self._busy():
            yield event.plain_result("刚动过屏幕，等我两秒～")
            return
        self._schedule(MONITOR_OFF, self._uid(event), "off")
        yield event.plain_result(str(self.conf.get("reply_off", "好～屏幕关上了。")))

    @filter.command("亮屏", alias={"开屏", "screenon"})
    async def cmd_screen_on(self, event: AstrMessageEvent):
        """点亮显示器"""
        if not self.enabled:
            yield event.plain_result("屏幕控制插件是关着的哦。")
            return
        if not self._allowed(event):
            yield event.plain_result(str(self.conf.get("reply_denied", "这个只能主人让我做哦。")))
            return
        if sys.platform != "win32" or _user32 is None:
            yield event.plain_result(str(self.conf.get("reply_unsupported", "这台机器不是 Windows。")))
            return
        if self._busy():
            yield event.plain_result("刚动过屏幕，等我两秒～")
            return
        self._schedule(MONITOR_ON, self._uid(event), "on")
        yield event.plain_result(str(self.conf.get("reply_on", "屏幕点亮啦～")))

    @filter.command("屏幕")
    async def cmd_screen_status(self, event: AstrMessageEvent):
        """查看屏幕控制插件的状态与统计"""
        st = self._stats
        last = "还没用过"
        if st.get("last_ts"):
            last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(st["last_ts"])))
            last += f"（{st.get('last_action', '')}，来自 {st.get('last_by') or '未知'}）"
        text = (
            f"屏幕控制：{'开' if self.enabled else '关'}｜"
            f"限量管理员：{'是' if self.conf.get('admin_only', True) else '否'}\n"
            f"关屏防待机：{'开' if KEEP_AWAKE.active else '关'}\n"
            f"关屏 {int(st.get('off', 0))} 次 / 亮屏 {int(st.get('on', 0))} 次\n"
            f"最近一次：{last}"
        )
        yield event.plain_result(text)

    # ---------- LLM 工具 ----------
    @filter.llm_tool(name="screen_control")
    async def tool_screen_control(self, event: AstrMessageEvent, action: str = "off") -> str:
        """控制电脑显示器的开关。只关屏幕，不会让电脑睡眠，也不影响任何正在运行的程序。

        Args:
            action(string): off 表示关闭显示器，on 表示点亮显示器
        """
        if not self.enabled or not self.conf.get("llm_tool_enabled", True):
            return "屏幕控制的工具没有开启。"
        if not self._allowed(event):
            return "当前用户没有权限操作主人的电脑屏幕。"
        if sys.platform != "win32" or _user32 is None:
            return "这台机器不是 Windows，无法控制屏幕。"

        act = str(action or "off").strip().lower()
        if act in ("off", "0", "false", "关", "关闭", "息屏", "熄屏"):
            mode, name = MONITOR_OFF, "off"
        elif act in ("on", "1", "true", "开", "打开", "亮屏", "点亮"):
            mode, name = MONITOR_ON, "on"
        else:
            return "参数只能是 off 或 on。"

        if self._busy():
            return "刚刚操作过屏幕，稍等两秒再说。"

        self._schedule(mode, self._uid(event), name)
        delay = float(self.conf.get("delay_seconds", 1.5) or 0)
        if name == "off":
            return f"已经把屏幕关掉了（延迟 {delay:.1f} 秒执行，电脑没有睡眠，我还在）。"
        return "已经把屏幕点亮了。"
