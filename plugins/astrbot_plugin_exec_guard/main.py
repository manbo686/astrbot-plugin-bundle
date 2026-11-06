"""执行权限守卫：只在白名单会话里放行本机执行/文件/远程控制类工具。"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DEFAULT_CONFIG = {
    "enabled": True,
    "allow_admin": True,
    "allow_patterns": ["FriendMessage:123456789"],
    "blocked_tools": [
        "astrbot_execute_python",
        "astrbot_execute_ipython",
        "astrbot_execute_shell",
        "astrbot_shell_session",
        "astrbot_file_read_tool",
        "astrbot_file_write_tool",
        "astrbot_file_edit_tool",
        "astrbot_grep_tool",
        "astrbot_upload_file",
        "astrbot_download_file",
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
        "astrbot_run_browser_skill",
        "astrbot_cua_screenshot",
        "astrbot_cua_mouse_click",
        "astrbot_cua_keyboard_type",
        "screen_control",
    ],
}


@register(
    "astrbot_plugin_exec_guard",
    "DSH",
    "只在指定会话放行本机执行类工具，群聊里自动移除",
    "1.0.0",
)
class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._cfg = dict(DEFAULT_CONFIG)
        if isinstance(config, dict):
            for key, value in config.items():
                self._cfg[key] = value

    # ---------- 内部 ----------

    @property
    def enabled(self) -> bool:
        return self._cfg.get("enabled", True) is not False

    @property
    def allow_patterns(self) -> list[str]:
        patterns = self._cfg.get("allow_patterns") or []
        return [str(p) for p in patterns if str(p).strip()]

    @property
    def blocked_tools(self) -> list[str]:
        tools = self._cfg.get("blocked_tools") or []
        return [str(t) for t in tools if str(t).strip()]

    @property
    def allow_admin(self) -> bool:
        return self._cfg.get("allow_admin", True) is not False

    def is_allowed(self, umo: str, role: str = "") -> bool:
        """会话是否允许动手（白名单会话，或管理员本人发言）。"""
        if role == "admin" and self.allow_admin:
            return True
        if not umo:
            return False
        return any(pattern in umo for pattern in self.allow_patterns)

    # ---------- 核心 ----------

    @filter.on_llm_request()
    async def guard_tools(self, event: AstrMessageEvent, req):
        if not self.enabled:
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        role = getattr(event, "role", "") or ""
        if self.is_allowed(umo, role):
            return

        tool_set = getattr(req, "func_tool", None)
        if not tool_set:
            return

        removed = []
        for name in self.blocked_tools:
            if tool_set.get_tool(name):
                tool_set.remove_tool(name)
                removed.append(name)

        if removed:
            logger.info(
                f"[exec_guard] 会话 {umo} 不在白名单，已移除 {len(removed)} 个本机工具: "
                + ", ".join(removed)
            )

    @filter.command("execguard")
    async def execguard(self, event: AstrMessageEvent):
        """查看当前执行权限配置。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        allowed = self.is_allowed(umo, getattr(event, "role", "") or "")
        yield event.plain_result(
            f"执行权限守卫：{'开启' if self.enabled else '关闭'}\n"
            f"当前会话：{umo}\n"
            f"是否允许动手：{'是' if allowed else '否（已移除执行/文件类工具）'}\n"
            f"白名单：{', '.join(self.allow_patterns) or '（空）'}\n"
            f"受限工具：{len(self.blocked_tools)} 个"
        )