"""NapCat 自启动插件。

AstrBot 加载完成后，自动拉起 NapCat（NapCatWinBootMain.exe）让机器人 QQ 上线。

注意事项：
- NapCat 是通过「注入 + 启动 QQ」的方式工作的，所以启动前 QQ 客户端必须是关闭状态；
  否则会把 NapCat 注入到你当前登录的 QQ 账号上。
- 若 config.json 里把 kill_qq_before_start 设为 true，插件会先杀掉 QQ.exe 再启动。
- 首次登录需要扫码（或已有快登 token 时用 -q 自动登录）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PLUGIN_DIR / "config.json"

CREATE_NEW_CONSOLE = 0x00000010 if sys.platform == "win32" else 0
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

DEFAULT_CONFIG: dict = {
    "enabled": True,
    "napcat_dir": "",
    "qq_number": "",
    "qq_path": "",
    "kill_qq_before_start": False,
    "start_delay_seconds": 6,
    "extra_args": "",
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[napcat_autostart] 读取 config.json 失败，使用默认值: {e}")
    return cfg


def _process_running(names: tuple[str, ...]) -> bool:
    targets = {n.lower() for n in names}
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name in targets:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _detect_qq_path() -> str:
    """从注册表 UninstallString 推导 QQ.exe 路径，失败则返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        ]
        for root, sub in keys:
            try:
                with winreg.OpenKey(root, sub) as k:
                    uninst, _ = winreg.QueryValueEx(k, "UninstallString")
                if not uninst:
                    continue
                candidate = Path(str(uninst).strip('"')).parent / "QQ.exe"
                if candidate.exists():
                    return str(candidate)
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    for fallback in (r"C:\Program Files\QQ.exe", r"C:\Program Files (x86)\QQ.exe"):
        if Path(fallback).exists():
            return fallback
    return ""


def _kill_qq(wait_seconds: int = 6) -> bool:
    if not _process_running(("QQ.exe",)):
        return True
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "QQ.exe"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[napcat_autostart] 结束 QQ 进程失败: {e}")
        return False
    for _ in range(wait_seconds):
        if not _process_running(("QQ.exe",)):
            return True
        time.sleep(1)
    return not _process_running(("QQ.exe",))


def start_napcat(cfg: dict) -> tuple[bool, str]:
    """同步启动 NapCat，返回 (是否已发出启动, 说明)。"""
    raw_dir = str(cfg.get("napcat_dir") or "").strip()
    if not raw_dir:
        return False, "没配 napcat_dir（NapCat 安装目录），跳过自启动"
    napcat_dir = Path(raw_dir).expanduser()
    boot_exe = napcat_dir / "NapCatWinBootMain.exe"
    hook_dll = napcat_dir / "NapCatWinBootHook.dll"
    if not boot_exe.exists() or not hook_dll.exists():
        return False, f"NapCat 目录不完整：{napcat_dir}"

    if _process_running(("NapCatWinBootMain.exe",)):
        return False, "NapCat 已在运行，跳过"

    qq_path = str(cfg.get("qq_path") or "").strip() or _detect_qq_path()
    if not qq_path or not Path(qq_path).exists():
        return False, f"找不到 QQ.exe（配置 qq_path 或检查 QQ 安装）: {qq_path!r}"

    if _process_running(("QQ.exe",)):
        if not cfg.get("kill_qq_before_start"):
            return False, (
                "QQ 客户端正在运行，NapCat 无法注入。"
                "请先退出 QQ，或把 config.json 的 kill_qq_before_start 设为 true"
            )
        if not _kill_qq():
            return False, "尝试结束 QQ 进程失败（可能权限不足）"

    args = [str(boot_exe), qq_path, str(hook_dll)]
    qq_number = str(cfg.get("qq_number") or "").strip()
    if qq_number:
        args += ["-q", qq_number]
    extra = str(cfg.get("extra_args") or "").strip()
    if extra:
        args += extra.split()

    # launcher.bat 在调用 NapCatWinBootMain 前会设置这些环境变量，
    # 缺了它们 NapCat 后端不会加载（不打印日志、不生成二维码、不连 OneBot）。
    env = os.environ.copy()
    env.update(
        {
            "NAPCAT_PATCH_PACKAGE": str(napcat_dir / "qqnt.json"),
            "NAPCAT_LOAD_PATH": str(napcat_dir / "loadNapCat.js"),
            "NAPCAT_INJECT_PATH": str(hook_dll),
            "NAPCAT_LAUNCHER_PATH": str(boot_exe),
            "NAPCAT_MAIN_PATH": str(napcat_dir / "napcat.mjs"),
        }
    )

    try:
        log_dir = PLUGIN_DIR.parents[1] / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        console_log = log_dir / "napcat_console.log"
        log_fh = open(console_log, "ab", buffering=0)  # noqa: SIM115
        proc = subprocess.Popen(  # noqa: S603
            args,
            cwd=str(napcat_dir),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
        log_fh.close()
        logger.info(f"[napcat_autostart] NapCat 输出重定向到 {console_log}")
    except Exception as e:  # noqa: BLE001
        return False, f"启动 NapCat 失败: {e}"

    # 等几秒确认没有秒退（注入失败、权限不足、QQ 路径错误都会立刻退出）
    for _ in range(6):
        if proc.poll() is not None:
            return (
                False,
                f"NapCat 启动后立即退出（退出码 {proc.returncode}）。"
                "常见原因：需要管理员权限、QQ 版本不匹配、或注入失败",
            )
        time.sleep(1)

    logger.info(
        f"[napcat_autostart] 已启动 NapCat 进程 pid={proc.pid} qq={qq_number or '(扫码登录)'}"
    )
    return True, f"NapCat 已启动 pid={proc.pid}"


@register("astrbot_plugin_napcat_autostart", "DSH", "AstrBot 启动后自动拉起 NapCat", "1.0.0")
class Main(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.cfg = load_config()

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self, *args, **kwargs) -> None:
        cfg = load_config()
        if not cfg.get("enabled", True):
            logger.info("[napcat_autostart] 已禁用（config.json: enabled=false）")
            return
        delay = cfg.get("start_delay_seconds", 6) or 0
        try:
            delay = max(0, int(delay))
        except (TypeError, ValueError):
            delay = 6
        if delay:
            await asyncio.sleep(delay)
        ok, msg = await asyncio.to_thread(start_napcat, cfg)
        if ok:
            logger.info(f"[napcat_autostart] {msg}")
        else:
            logger.warning(f"[napcat_autostart] 未启动：{msg}")

    @filter.command("napcat")
    async def napcat_cmd(self, event: AstrMessageEvent) -> None:
        """用法：/napcat status | /napcat start | /napcat stop"""
        raw = (event.message_str or "").strip()
        parts = raw.split()
        action = parts[1].lower() if len(parts) > 1 else "status"

        if action in {"status", "状态"}:
            running = _process_running(("NapCatWinBootMain.exe",))
            qq_running = _process_running(("QQ.exe",))
            yield event.plain_result(
                f"NapCat 进程：{'运行中' if running else '未运行'}\n"
                f"QQ 客户端：{'运行中' if qq_running else '未运行'}"
            )
            return

        if action in {"start", "启动"}:
            ok, msg = await asyncio.to_thread(start_napcat, load_config())
            yield event.plain_result(("✅ " if ok else "⚠️ ") + msg)
            return

        if action in {"stop", "停止"}:
            if not _process_running(("QQ.exe",)) and not _process_running(("NapCatWinBootMain.exe",)):
                yield event.plain_result("NapCat / QQ 本来就没在运行")
                return
            _kill_qq()
            yield event.plain_result("已结束 QQ 进程（NapCat 随之退出）")
            return

        yield event.plain_result("用法：/napcat status | start | stop")
