# -*- coding: utf-8 -*-
"""主动聊天插件：上一次对话结束后，让它不定时地主动来找主人说话。

机制：
- 记录每个会话「主人最后一次说话」的时间；
- 闲置超过 idle_min 分钟后，按概率随机挑一个时机，用 cron 的一次性任务唤起它自己写一句；
- 话题按心情加权（关心/撒娇/好奇/分享/请求/记忆疑问），并参考心情、兴趣点、B 站活动；
- 主动说话会推心情（engagement/affinity），也会写进 LivingMemory。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_proactive_chat"
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
STATE_PATH = DATA_DIR / "state.json"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sessions": [],
    "idle_min_minutes": 45,
    "idle_max_minutes": 180,
    "daily_max": 6,
    "quiet_hours": "",
    "probability": 0.6,
    "min_gap_minutes": 40,
    "mood_star": "astrbot_plugin_mood",
    "interest_star": "astrbot_plugin_bili_interest",
    "kinds": {
        "care": 22,
        "greet": 12,
        "wonder": 20,
        "share": 18,
        "ask_permission": 10,
        "memory_gap": 10,
        "miss": 8,
    },
}

KIND_NOTE = {
    "care": "关心他：问问他这几天睡得怎么样、有没有按时吃饭、忙不忙；别说教。",
    "greet": "像刚睡醒一样跟他打个招呼，可以是很小的事，比如你刚吐了个泡泡。",
    "wonder": "好奇他的生活：问他最近在玩什么、看什么、有什么爱好，或者追问一个你一直没搞懂的习惯。",
    "share": "分享你刚看到的一件小事（视频/弹幕/群里的梗），说清楚你为什么觉得有意思。",
    "ask_permission": "想跟他要一个许可：比如能不能刷到更晚、能不能多看一个视频、能不能把某件事写进日记；说清理由。",
    "memory_gap": "你记忆里有一块空白：提一个你想不起来的事（某天的约定、他说过的一句话），问他记不记得。",
    "miss": "说一句你有点想他，别撒娇过头。",
}

QUIET_CHECK = ("早上", "白天", "晚上")


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return default


def _save_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[proactive] 写文件失败 {path}: {exc}")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _hhmm_minutes(text: str) -> int:
    try:
        hh, mm = str(text).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except Exception:  # noqa: BLE001
        return 0


@register(PLUGIN_NAME, "DSH", "上一次对话结束后，不定时主动来找主人说话（心情/兴趣点/记忆联动）", "1.0.0")
class ProactiveChat(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self._cfg_obj = config if isinstance(config, dict) else {}
        self._task: asyncio.Task | None = None

    # ---------------- 基础 ----------------

    def _cfg(self) -> dict:
        merged = json.loads(json.dumps(DEFAULTS))
        if isinstance(self._cfg_obj, dict):
            merged.update(self._cfg_obj)
        return merged

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled"))

    def _state(self) -> dict:
        st = _load_json(STATE_PATH, {}) or {}
        if st.get("day") != _today():
            st = {"day": _today(), "sessions": {}}
        st.setdefault("sessions", {})
        return st

    def _sess(self, st: dict, umo: str) -> dict:
        d = st["sessions"].setdefault(umo, {})
        d.setdefault("last_user_ts", 0.0)
        d.setdefault("last_sent_ts", 0.0)
        d.setdefault("next_due_ts", 0.0)
        d.setdefault("sent_today", 0)
        d.setdefault("last_topics", [])
        return d

    def _in_quiet(self, cfg: dict) -> bool:
        rng = str(cfg.get("quiet_hours") or "").strip()
        if not rng:
            return False
        now_h = datetime.now().hour
        try:
            a, b = rng.split("-")[:2]
            a, b = int(a), int(b)
        except Exception:  # noqa: BLE001
            return False
        return a <= now_h < b if a <= b else (now_h >= a or now_h < b)

    # ---------------- 联动 ----------------

    def _star(self, name: str):
        try:
            meta = self.context.get_registered_star(name)
            return getattr(meta, "star_cls", None)
        except Exception:  # noqa: BLE001
            return None

    def _mood_values(self) -> dict:
        star = self._star(str(self._cfg().get("mood_star") or ""))
        if star and hasattr(star, "mood_snapshot"):
            try:
                snap = star.mood_snapshot() or {}
                return dict(snap.get("values") or {})
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _interest_line(self) -> str:
        star = self._star(str(self._cfg().get("interest_star") or ""))
        if star and hasattr(star, "plan_line"):
            try:
                return str(star.plan_line() or "")
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def _pick_kind(self, cfg: dict, values: dict) -> str:
        weights = dict(cfg.get("kinds") or {})
        v = float(values.get("valence", 55) or 55)
        a = float(values.get("arousal", 55) or 55)
        if v < 40:  # 心情低 → 更想被关心，也更黏
            weights["miss"] = weights.get("miss", 8) * 2
            weights["care"] = weights.get("care", 22) * 1.5
            weights["share"] = weights.get("share", 18) * 0.4
        if a > 70:  # 兴奋 → 更想分享
            weights["share"] = weights.get("share", 18) * 2
            weights["wonder"] = weights.get("wonder", 20) * 1.3
        kinds = [k for k, w in weights.items() if w > 0]
        if not kinds:
            return "care"
        return random.choices(kinds, weights=[weights[k] for k in kinds], k=1)[0]

    def _compose_note(self, kind: str, cfg: dict) -> str:
        values = self._mood_values()
        lines = [
            "【主动找主人 · 不要提这是任务】",
            f"现在是你自己想找他说话，不是他叫你。用你的口吻给他发一条消息，1 到 3 句，单条不超过 40 字。",
            f"这次的主题：{KIND_NOTE.get(kind, KIND_NOTE['care'])}",
            "要求：像真人发 QQ 消息，不要开场白套话、不要总结、不要旁白括号、不要问『在吗』；只发这一条，别连发。",
        ]
        if values:
            lines.append(
                "你此刻的状态：心情 {:.0f}/100，劲儿 {:.0f}，压力 {:.0f}，亲近 {:.0f}。".format(
                    values.get("valence", 55), values.get("arousal", 55),
                    values.get("stress", 40), values.get("affinity", 55))
            )
        plan = self._interest_line()
        if kind == "share" and plan:
            lines.append("你今天想看的：" + plan.split("。")[0][:60])
        return "\n".join(lines)

    # ---------------- 派发 ----------------

    def _cron(self):
        try:
            return getattr(self.context, "cron_manager", None)
        except Exception:  # noqa: BLE001
            return None

    async def _fire(self, umo: str, kind: str) -> bool:
        mgr = self._cron()
        if mgr is None:
            logger.warning("[proactive] 没有 cron_manager，跳过")
            return False
        note = self._compose_note(kind, self._cfg())
        try:
            job = await mgr.add_active_job(
                name=f"主动·{kind}",
                cron_expression=None,
                payload={"session": umo, "note": note, "origin": "proactive_chat"},
                run_once=True,
                run_at=datetime.now().replace(microsecond=0),
            )
            logger.info(f"[proactive] 已派发主动消息 kind={kind} job={getattr(job, 'job_id', None)}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[proactive] 派发失败: {exc}")
            return False
        star = self._star(str(self._cfg().get("mood_star") or ""))
        if star and hasattr(star, "nudge"):
            try:
                star.nudge({"engagement": 3, "affinity": 2}, "主动找主人说话")
            except Exception:  # noqa: BLE001
                pass
        return True

    async def _loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                if self.enabled:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[proactive] tick 出错: {exc}")
            await asyncio.sleep(120)

    async def _tick(self) -> None:
        cfg = self._cfg()
        if self._in_quiet(cfg):
            return
        st = self._state()
        now = time.time()
        changed = False
        for umo in list(cfg.get("sessions") or []):
            s = self._sess(st, umo)
            if s["sent_today"] >= int(cfg.get("daily_max") or 6):
                continue
            if s["last_user_ts"] and now - s["last_user_ts"] < float(cfg.get("idle_min_minutes") or 45) * 60:
                continue
            if s["next_due_ts"] and now < s["next_due_ts"]:
                continue
            if s["last_sent_ts"] and now - s["last_sent_ts"] < float(cfg.get("min_gap_minutes") or 40) * 60:
                continue
            if random.random() > float(cfg.get("probability") or 0.6):
                s["next_due_ts"] = now + random.uniform(10, 30) * 60
                changed = True
                continue
            kind = self._pick_kind(cfg, self._mood_values())
            if await self._fire(umo, kind):
                s["sent_today"] += 1
                s["last_sent_ts"] = now
                s["last_topics"] = (s.get("last_topics") or [])[-4:] + [kind]
                s["next_due_ts"] = now + random.uniform(
                    float(cfg.get("idle_min_minutes") or 45),
                    float(cfg.get("idle_max_minutes") or 180),
                ) * 60
                changed = True
        if changed:
            _save_json(STATE_PATH, st)

    # ---------------- 生命周期与钩子 ----------------

    async def initialize(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._loop(), name="proactive_chat_loop")
        logger.info("[proactive] 主动聊天已启动（闲置后不定时找主人）")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """记录主人最后一次说话的时间。"""
        if not self.enabled:
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            st = self._state()
            s = self._sess(st, umo)
            s["last_user_ts"] = time.time()
            s["next_due_ts"] = 0.0
            _save_json(STATE_PATH, st)
        except Exception:  # noqa: BLE001
            pass

    @filter.command("proactive")
    async def proactive_cmd(self, event: AstrMessageEvent):
        """查看/触发主动聊天状态。"""
        parts = str(getattr(event, "message_str", "") or "").split()
        act = parts[1] if len(parts) > 1 else "status"
        if act == "now":
            cfg = self._cfg()
            sessions = cfg.get("sessions") or []
            if not sessions:
                yield event.plain_result("还没配 sessions，先填会话再触发。")
                return
            umo = str(sessions[0])
            kind = self._pick_kind(cfg, self._mood_values())
            ok = await self._fire(umo, kind)
            yield event.plain_result(f"派发{kind}：{'成功' if ok else '失败'}")
            return
        cfg = self._cfg()
        st = self._state()
        lines = [f"主动聊天：{'开' if cfg.get('enabled') else '关'}，概率 {cfg.get('probability')}，每天最多 {cfg.get('daily_max')} 次，安静时段 {cfg.get('quiet_hours')}"]
        for umo in cfg.get("sessions") or []:
            s = self._sess(st, umo)
            idle = int((time.time() - (s.get("last_user_ts") or time.time())) / 60)
            lines.append(f"- {umo}：今天已说 {s.get('sent_today')} 次，闲置 {idle} 分钟，最近话题 {s.get('last_topics')}")
        yield event.plain_result("\n".join(lines))
