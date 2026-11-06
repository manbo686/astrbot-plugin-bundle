# -*- coding: utf-8 -*-
"""Token 电表：把每次模型调用的用量、缓存命中、请求结构记下来。

数据来源：
- `astrbot/core/provider/entities.py:323` 的 `TokenUsage`
  （input_other / input_cached / output），挂在 `LLMResponse.usage`。
- 请求侧由 `@filter.on_llm_request()` 先记下 system_prompt / contexts / 工具数量，
  响应时再跟 usage 对上号（同一个 umo 的请求是串行的，用一个栈就够了）。

落盘：`data/usage-YYYYMMDD.jsonl`，一行一次调用。
命令：`/tokens`（今天的汇总 + 最近 7 天）
HTTP：`state`（today / days / recent）、`reset`（清空今天的记录文件）
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
CONFIG_PATH = PLUGIN_DIR / "config.json"
PLUGIN_NAME = "astrbot_plugin_token_meter"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 估算价格（元 / 百万 token），按 DeepSeek 官方价目可自行调整
    "price_in_miss": 2.0,
    "price_in_hit": 0.5,
    "price_out": 8.0,
    "keep_days": 30,
    "show_recent": 10,
}


def _load_json(path: Path, dflt):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8-sig") as f:
                return json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[token_meter] 读取 {path.name} 失败: {e}")
    return dflt


def load_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    raw = _load_json(CONFIG_PATH, {})
    if isinstance(raw, dict):
        for k, v in raw.items():
            cfg[k] = v
    return cfg


def _today() -> str:
    return date.today().isoformat()


def _file_for(day: str) -> Path:
    return DATA_DIR / f"usage-{day.replace('-', '')}.jsonl"


def _fmt_tokens(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(int(n))


def _read_day(day: str) -> list[dict[str, Any]]:
    p = _file_for(day)
    out: list[dict[str, Any]] = []
    try:
        if not p.exists():
            return out
        with p.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[token_meter] 读取 {p.name} 失败: {e}")
    return out


@register(
    PLUGIN_NAME,
    "DSH",
    "记录每次模型调用的 token 用量与缓存命中，按天落盘，/tokens 看账",
    "1.0.0",
)
class TokenMeter(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._cfg_obj = config if isinstance(config, dict) else None
        self.conf = load_config()
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 工具

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled", True))

    def _save(self, rec: dict[str, Any]) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with _file_for(rec["date"]).open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[token_meter] 写记录失败: {e}")

    def _aggregate(self, recs: list[dict[str, Any]]) -> dict[str, Any]:
        inp_other = sum(int(r.get("in_other") or 0) for r in recs)
        inp_cached = sum(int(r.get("in_cached") or 0) for r in recs)
        out = sum(int(r.get("out") or 0) for r in recs)
        total_in = inp_other + inp_cached
        cost = (
            inp_other * float(self.conf.get("price_in_miss", 2.0)) / 1e6
            + inp_cached * float(self.conf.get("price_in_hit", 0.5)) / 1e6
            + out * float(self.conf.get("price_out", 8.0)) / 1e6
        )
        return {
            "calls": len(recs),
            "in_other": inp_other,
            "in_cached": inp_cached,
            "out": out,
            "in_total": total_in,
            "hit_ratio": round(inp_cached / total_in, 4) if total_in else 0.0,
            "cost": round(cost, 4),
        }

    def summary(self) -> dict[str, Any]:
        today = _today()
        recs = _read_day(today)
        days: list[dict[str, Any]] = []
        for i in range(7):
            d = (date.today() - timedelta(days=i)).isoformat()
            r = recs if d == today else _read_day(d)
            if not r and d != today:
                continue
            days.append({"date": d, **self._aggregate(r)})
        recent = list(reversed(recs[-int(self.conf.get("show_recent", 10)):]))
        return {"today": self._aggregate(recs), "days": days, "recent": recent}

    # ------------------------------------------------------------------ 钩子

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        if not self.enabled:
            return
        try:
            contexts = getattr(req, "contexts", None) or []
            ctx_chars = 0
            for m in contexts:
                try:
                    c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
                except Exception:  # noqa: BLE001
                    c = ""
                ctx_chars += len(str(c or ""))
            tools = getattr(req, "func_tool", None)
            tool_names: list[str] = []
            try:
                for t in getattr(tools, "tools", []) or []:
                    nm = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                    if nm:
                        tool_names.append(str(nm))
            except Exception:  # noqa: BLE001
                tool_names = []
            umo = getattr(event, "unified_msg_origin", "") or ""
            meta = {
                "umo": umo,
                "model": str(getattr(req, "model", "") or ""),
                "sp_chars": len(str(getattr(req, "system_prompt", "") or "")),
                "prompt_chars": len(str(getattr(req, "prompt", "") or "")),
                "ctx_msgs": len(contexts),
                "ctx_chars": ctx_chars,
                "tools": len(tool_names),
                "tool_names": tool_names,
            }
            self._pending[umo] = meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[token_meter] 请求记录失败: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: Any):
        if not self.enabled:
            return
        try:
            usage = getattr(resp, "usage", None)
            umo = getattr(event, "unified_msg_origin", "") or ""
            meta = self._pending.pop(umo, None) or {}
            rec: dict[str, Any] = {
                "ts": int(time.time()),
                "date": _today(),
                "time": datetime.now().strftime("%H:%M:%S"),
                "umo": umo,
                "model": meta.get("model", ""),
                "in_other": int(getattr(usage, "input_other", 0) or 0) if usage else 0,
                "in_cached": int(getattr(usage, "input_cached", 0) or 0) if usage else 0,
                "out": int(getattr(usage, "output", 0) or 0) if usage else 0,
                "sp_chars": meta.get("sp_chars", 0),
                "prompt_chars": meta.get("prompt_chars", 0),
                "ctx_msgs": meta.get("ctx_msgs", 0),
                "ctx_chars": meta.get("ctx_chars", 0),
                "tools": meta.get("tools", 0),
            }
            if usage is None:
                logger.info("[token_meter] 这次响应没有 usage，跳过记账")
                return
            self._save(rec)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[token_meter] 响应记账失败: {e}")

    # ------------------------------------------------------------------ 命令

    @filter.command("tokens", alias={"token", "电表"})
    async def tokens_cmd(self, event: AstrMessageEvent):
        if not self.enabled:
            yield event.plain_result("电表没开。")
            return
        s = self.summary()
        t = s["today"]
        lines = [
            f"今天：{t['calls']} 次调用",
            f"输入 {_fmt_tokens(t['in_total'])}（缓存命中 {_fmt_tokens(t['in_cached'])}，"
            f"{t['hit_ratio'] * 100:.1f}%）｜输出 {_fmt_tokens(t['out'])}",
            f"估算成本 ¥{t['cost']:.3f}（价目见配置 price_in_miss/hit/out）",
        ]
        if len(s["days"]) > 1:
            lines.append("最近 7 天：")
            for d in s["days"]:
                lines.append(
                    f"  {d['date'][5:]}  {d['calls']:>3} 次  {_fmt_tokens(d['in_total']):>7}  "
                    f"命中 {d['hit_ratio'] * 100:>5.1f}%  ¥{d['cost']:.3f}"
                )
        if s["recent"]:
            r = s["recent"][0]
            lines.append(
                f"最近一次：{r['time']} in {_fmt_tokens(r['in_total'])} / out {_fmt_tokens(r['out'])}，"
                f"sys {r['sp_chars']} 字、上下文 {r['ctx_msgs']} 条、工具 {r['tools']} 个"
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ HTTP

    async def api_state(self) -> dict:
        return self.summary()

    async def api_reset(self) -> dict:
        p = _file_for(_today())
        try:
            if p.exists():
                p.unlink()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    async def initialize(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        for route, handler, desc in (
            ("state", self.api_state, "今天的用量与最近 7 天"),
            ("reset", self.api_reset, "清空今天的记录"),
        ):
            try:
                self.context.register_web_api(
                    f"{PLUGIN_NAME}/{route}", handler, ["GET"], desc
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[token_meter] 注册 Web API {route} 失败: {e}")
        logger.info(f"[token_meter] 电表已启动（数据目录 {DATA_DIR}）")
