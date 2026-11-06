# -*- coding: utf-8 -*-
"""醒来时刻表：机器人一天只按这一张表「醒」，所有定时任务都挂在醒来点上。

设计：
- 每天按 config 里的 windows 排出 wakes_min~wakes_max 个醒来点
  （当前配置：12:00–14:00 与 18:00–24:00 两个窗、5~6 次、最小间隔 25 分钟）；
- 每个醒来点派发 1 个主任务（刷 B 站 watch_min~watch_max 个），再按规则挂副任务：
  * 第 1 次醒来 → 查 QQ / B 站消息（当天只做一次）
  * 每次醒来 → 主动关心判据（自己会判断该不该发）
  * 最后一次醒来 → 写日记
- 任务内容沿用原来的提示词，本插件不重复实现业务逻辑，只负责「什么时候醒」。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_wake_plan"
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
PLAN_PATH = DATA_DIR / "plan.json"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "target_session": "",
    "windows": ["12:00-14:00", "18:00-24:00"],
    "active_from": "12:00",
    "active_to": "24:00",
    "wakes_min": 5,
    "wakes_max": 6,
    "target_min": 5,
    "target_max": 6,
    "gap_base_minutes": 70,
    "min_gap_minutes": 25,
    "catchup_minutes": 8,
    "care_every": 1,          # 每次醒来都过一遍主动关心判据（判据自己会拦）
    "diary_on_last": True,
    "first_wake_check": True,
    "watch_min": 1,
    "watch_max": 4,
    "note_watch": (
        "0) 先 bili_mentions 看一眼主人有没有回你、有没有 @ 你——有就先回他：用 bili_comments 找到那条评论的 rpid，再 bili_reply 回一句，回完 bili_mention_done 标记；和主人的互动不占额度；\n"
        "现在是你自己的时间。\n"
        "0. 先用 bili_owner_list 看看主人在 QQ 里发给你看的视频；有就优先用 bili_watch 按 BV 去看"
        "（不占今天的额度），看完照常评论/回复，看完会自动划掉；\n"
        "1. 再用 bili_watch 看 {watch_min}-{watch_max} 个视频{watch_left}——这一轮看几个由你自己定（想多看就多看，1 个也行，最多 {watch_max} 个），别为了凑数硬看"
        "（单条超过 40 分钟的直接跳过；主人清单里点名的例外，照样看）；不给 BV 就按今天的兴趣点分区挑；跳过擦边/猎奇/纯带货/纯搬运；\n"
        "2. 用 bili_comments 翻评论区——不是只看，要回：挑 1-2 条真想接的评论用 bili_reply 回他"
        "（回评论不占视频额度，受每个视频 20 条互动上限和 8 秒间隔限制；找不到具体的人再发主楼 bili_comment）；\n"
        "3. 主楼评论每个视频最多 20 条、两条之间至少隔 8 秒（插件会自己拦）；"
        "回别人评论同样算在这个视频的 20 条里，回主人不计；没想说的别硬发。\n"
        "做完不用给主人汇报。"
    ),
    "note_care": (
        "顺手做一次主动关心检查（别声张）：\n"
        "跑 工作目录\\proactive\\check_idle.py，看 should_checkin：\n"
        "- false → 什么都不做，结束；\n"
        "- true → 给主人发一句自然、短、像真人的话（1-2 行），可从当天 bili-notes 里挑一条随口分享；"
        "发完记账 record_ping('checkin')。\n"
        "主人若说了去睡觉/吃饭/忙，别打扰。"
    ),
    "note_first": (
        "今天第一次醒来，先看有没有人找你：\n"
        "1) 读 工作目录\\notes\\checked-{date}.txt，存在就跳过整段；\n"
        "2) 不存在 → 看 B 站 mentions（bili_mentions）与 QQ（NapCat HTTP 127.0.0.1:6700，"
        "token 7731a0c4b9d84a52，get_recent_contact / get_group_msg_history）；\n"
        "3) 有主人的 @/回复就去回，和主人的互动不占配额；群友 @ 或喊名字按群里规矩短句回；\n"
        "4) 收尾写 checked-{date}.txt（时间+结论）。没人找就安静，不用汇报。"
    ),
    "note_diary": (
        "今天的最后一次醒来，写日记：\n"
        "公开日记 工作目录\\diary\\公开日记.md、私人日记 工作目录\\diary\\私人日记.md；\n"
        "取材：当天 bili-notes、聊天里值得记的（新规矩、被纠正、被夸）；\n"
        "公开 5-10 行口语短句，私人写偏好校准与没说出口的话；用「## 日期 · 小标题」追加，不覆盖。\n"
        "写完别给主人发消息。"
    ),
}


def _today() -> str:
    return date.today().isoformat()


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hhmm_minutes(text: str, fallback: int) -> int:
    try:
        hh, mm = str(text or "").split(":")[:2]
        return max(0, min(24 * 60, int(hh) * 60 + int(mm)))
    except Exception:  # noqa: BLE001
        return fallback


def _load(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return default


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


@register("astrbot_plugin_wake_plan", "DSH", "一天只有一张醒来时刻表，所有定时任务都挂在醒来点上", "1.0.3")
class WakePlan(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._cfg_obj = config if isinstance(config, dict) else None
        self._task: asyncio.Task | None = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------- 配置 ----------------
    def _cfg(self) -> dict:
        merged = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
        merged.update(self._cfg_obj or {})
        return merged

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    # ---------------- 计划 ----------------
    def _plan(self) -> dict:
        plan = _load(PLAN_PATH, {})
        if plan.get("date") != _today():
            plan = {"date": _today(), "slots": [], "done": []}
        if not plan.get("slots"):
            plan["slots"] = self._make_slots()
            _save(PLAN_PATH, plan)
        return plan

    def _windows(self, cfg: dict) -> list[tuple[int, int]]:
        """支持多时间窗：windows 里写 ["12:00-14:00", "18:00-24:00"]。"""
        out: list[tuple[int, int]] = []
        for item in cfg.get("windows") or []:
            txt = str(item or "").replace("—", "-").replace("~", "-").replace("－", "-")
            if "-" not in txt:
                continue
            a, b = txt.split("-")[:2]
            st = _hhmm_minutes(a, -1)
            en = _hhmm_minutes(b, -1)
            if st < 0 or en < 0 or en <= st:
                continue
            out.append((st, en))
        if not out:
            st = _hhmm_minutes(cfg.get("active_from") or "08:00", 8 * 60)
            en = _hhmm_minutes(cfg.get("active_to") or "23:00", 23 * 60)
            if en > st:
                out.append((st, en))
        return out

    def _wake_count(self, cfg: dict) -> int:
        lo = int(cfg.get("wakes_min") or cfg.get("target_min") or 5)
        hi = int(cfg.get("wakes_max") or cfg.get("target_max") or 6)
        if hi < lo:
            hi = lo
        rng = random.Random(f"{_today()}|wake_count")
        n = rng.randint(lo, hi)
        values = self._mood_values()
        if values:
            try:
                v = float(values.get("valence", 55))
                if v > 65 and n < hi:
                    n += 1
                elif v < 40 and n > lo:
                    n -= 1
            except Exception:  # noqa: BLE001
                pass
        return max(lo, min(hi, n))

    def _mood_values(self) -> dict:
        try:
            meta = self.context.get_registered_star("astrbot_plugin_mood")
            star = getattr(meta, "star_cls", None)
            if star is not None and hasattr(star, "mood_snapshot"):
                return (star.mood_snapshot() or {}).get("values") or {}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _make_slots(self) -> list[dict]:
        cfg = self._cfg()
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        segs = [(st, en) for (st, en) in self._windows(cfg) if en > now_min + 2]
        if not segs:
            return []
        segs = [(max(st, now_min + 2), en) for (st, en) in segs]
        gap = int(cfg.get("min_gap_minutes") or 25)
        # 每段能装几次：按最小间隔算容量，装不下就少醒几次，不许硬挤
        caps = [max(0, (en - st - 2) // gap + 1) for st, en in segs]
        segs = [seg for seg, cap in zip(segs, caps) if cap > 0]
        caps = [cap for cap in caps if cap > 0]
        if not segs:
            return []
        count = min(self._wake_count(cfg), sum(caps))
        total = max(1, sum(en - st for st, en in segs))
        rng = random.Random(f"{_today()}|{now_min}|{count}|{segs}")
        alloc = [max(1, int(round(count * (en - st) / total))) for st, en in segs]
        alloc = [min(a, c) for a, c in zip(alloc, caps)]
        while sum(alloc) > count:
            i = alloc.index(max(alloc))
            if alloc[i] <= 1:
                break
            alloc[i] -= 1
        while sum(alloc) < count:
            room = [(caps[i] - alloc[i], segs[i][1] - segs[i][0]) for i in range(len(alloc))]
            i = max(range(len(room)), key=lambda k: (room[k][0], room[k][1]))
            if room[i][0] <= 0:
                break
            alloc[i] += 1
        points: list[int] = []
        for (st, en), n in zip(segs, alloc):
            if n <= 0:
                continue
            span = en - st
            step = span / n
            g = max(1, gap)
            for i in range(n):
                at = st + step * (i + 0.5) + rng.uniform(-0.35, 0.35) * step
                at = int(round(at))
                if points and at - points[-1] < g:
                    at = points[-1] + g
                if at >= en - 1 or (points and at <= points[-1]):
                    continue      # 塞不下就不再硬挤，宁可少醒一次
                points.append(at)
        points.sort()
        return [
            {"at": f"{p // 60:02d}:{p % 60:02d}", "done": False, "tasks": []}
            for p in points
        ]

    # ---------------- 派发 ----------------
    def _cron(self):
        return getattr(self.context, "cron_manager", None)

    async def _queue(self, kind: str, note: str, at: datetime) -> str | None:
        mgr = self._cron()
        if mgr is None:
            return None
        cfg = self._cfg()
        if not str(cfg.get("target_session") or "").strip():
            logger.warning("[wake_plan] 没配 target_session，跳过排班")
            return None
        try:
            job = await mgr.add_active_job(
                name=f"醒来·{kind}·{at.strftime('%H:%M')}",
                cron_expression=None,
                payload={
                    "session": cfg.get("target_session"),
                    "note": note,
                    "origin": "wake_plan",
                },
                run_once=True,
                run_at=at,
            )
            return getattr(job, "job_id", None) or getattr(job, "id", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wake_plan] 排队失败 {kind}: {exc}")
            return None

    async def _ensure_login(self) -> None:
        """每天第一次醒来时，顺手让 B 站插件保持登录状态。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_bili_activity")
            star = getattr(meta, "star_cls", None)
            if star is not None and hasattr(star, "_daily_login") and hasattr(star, "_sched"):
                res = star._daily_login(star._sched())
                if asyncio.iscoroutine(res):
                    await res
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wake_plan] 登录检查失败: {exc}")

    def _interest_remaining(self) -> int | None:
        """读兴趣点引擎今天的剩余额度；拿不到返回 None（退回纯 1-4 的写法）。"""
        try:
            from astrbot.core.star.context import Context  # noqa: F401
        except Exception:
            pass
        try:
            meta = self.context.get_registered_star("astrbot_plugin_bili_interest")
            star = getattr(meta, "star_cls", None) if meta is not None else None
            if star is not None and hasattr(star, "remaining_videos"):
                return int(star.remaining_videos())
        except Exception:
            pass
        return None

    def _interest_plan_line(self) -> str:
        """读兴趣点引擎今日的分区预算与兴趣点，拼成一行给提示词用。"""
        try:
            meta = self.context.get_registered_star("astrbot_plugin_bili_interest")
            star = getattr(meta, "star_cls", None) if meta is not None else None
            if star is not None and hasattr(star, "plan_line"):
                return str(star.plan_line() or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _render_note_watch(self, cfg: dict) -> str:
        """把 1-4 条的写法与兴趣点引擎的剩余额度对齐：以引擎总额为准。"""
        note = (cfg.get("note_watch") or "")
        lo = int(cfg.get("watch_min") or 1)
        hi = int(cfg.get("watch_max") or 4)
        left = self._interest_remaining()
        if left is not None:
            if left <= 0:
                return ("今天兴趣点引擎给的视频额度已经用完了，这一轮别再 bili_watch 看新视频了。"
                        "但评论区照旧要逛、要回：先用 bili_mentions 看主人有没有回你、有没有 @ 你，有就先 bili_reply 回；"
                        "再用 bili_comments 翻一翻，挑 1-2 条真想接的评论用 bili_reply 回他们（回评论不占视频额度，"
                        "只受每个视频 20 条互动上限和 8 秒间隔限制）；确实一条都不想接，才只看看就好。")
            hi = max(1, min(hi, int(left)))
            note = note.replace("{watch_left}", f"（今天还剩 {int(left)} 个额度）")
        else:
            note = note.replace("{watch_left}", "")
        note = note.replace("{watch_min}", str(max(1, min(lo, hi))))
        note = note.replace("{watch_max}", str(hi))
        plan = self._interest_plan_line()
        if plan:
            head = f"今天还剩 {int(left)} 个视频额度。" if left else ""
            note = (note.rstrip()
                    + f"\n{head}分区预算：{plan}"
                    + "\nbili_watch 不给 BV，插件就会按这个预算自己挑（兴趣点分区优先），你不用自己记数；"
                    + "先补还没看的兴趣点分区，再看探索/热门。")
        return note

    async def _fire_slot(self, index: int, total: int, slot: dict) -> None:
        cfg = self._cfg()
        if index == 0:
            await self._ensure_login()
        at = datetime.now() + timedelta(seconds=20)
        hhmm = slot.get("at")
        header = f"【醒来 · 今天第 {index + 1}/{total} 次 · {hhmm}】\n"
        queued: list[str] = []

        jobs = [(f"刷视频", self._render_note_watch(cfg))]
        if index == 0 and cfg.get("first_wake_check", True):
            note = (cfg.get("note_first") or "").replace("{date}", _today())
            jobs.append(("查消息", note))
        if cfg.get("care_every", 1) and index % max(1, int(cfg.get("care_every") or 1)) == 0:
            jobs.append(("主动关心", cfg.get("note_care") or ""))
        if cfg.get("diary_on_last", True) and index == total - 1:
            jobs.append(("写日记", cfg.get("note_diary") or ""))

        for i, (kind, body) in enumerate(jobs):
            when = at + timedelta(seconds=45 * i)
            job_id = await self._queue(kind, header + body, when)
            if job_id:
                queued.append(kind)
        slot["done"] = True
        slot["tasks"] = queued
        slot["fired_at"] = _now_text()
        plan = self._plan()
        for s in plan.get("slots") or []:
            if s.get("at") == hhmm:
                s.update({"done": True, "tasks": queued, "fired_at": slot["fired_at"]})
        plan["done"] = list(plan.get("done") or []) + [hhmm]
        _save(PLAN_PATH, plan)
        logger.info(f"[wake_plan] {hhmm} 醒来派发：{'、'.join(queued) or '无任务'}")

    async def _loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                if self.enabled:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[wake_plan] 巡检出错: {exc}")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        cfg = self._cfg()
        plan = self._plan()
        slots = plan.get("slots") or []
        if not slots:
            return
        now_min = datetime.now().hour * 60 + datetime.now().minute
        catch = int(cfg.get("catchup_minutes") or 90)
        for i, slot in enumerate(slots):
            if slot.get("done"):
                continue
            at_min = _hhmm_minutes(slot.get("at") or "", 0)
            if at_min > now_min:
                continue
            if now_min - at_min > catch:
                slot["done"] = True
                slot["skipped"] = "错过补跑窗口"
                _save(PLAN_PATH, plan)
                continue
            await self._fire_slot(i, len(slots), slot)
            return

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="wake_plan_loop")
        slots = self._plan().get("slots") or []
        logger.info(f"[wake_plan] 醒来时刻表已启动：今天 {len(slots)} 个醒来点")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    # ---------------- 命令 ----------------
    @filter.command("wake")
    async def wake_cmd(self, event: AstrMessageEvent):
        """/wake：看今天的醒来时刻表"""
        argc = len(str(getattr(event, "message_str", "") or "").split())
        parts = str(getattr(event, "message_str", "") or "").split()
        arg = parts[-1].lower() if len(parts) > 1 else "status"
        if arg in ("plan", "status"):
            plan = self._plan()
            slots = plan.get("slots") or []
            now_min = datetime.now().hour * 60 + datetime.now().minute
            lines = [f"今天共 {len(slots)} 次醒来："]
            for i, s in enumerate(slots, 1):
                mark = "✅" if s.get("done") else ("→" if _hhmm_minutes(s["at"], 0) <= now_min else "·")
                tasks = "、".join(s.get("tasks") or []) or ""
                lines.append(f"{mark} {s['at']}　{tasks}")
            yield event.plain_result("\n".join(lines))
        elif arg == "now":
            plan = self._plan()
            slots = plan.get("slots") or []
            if slots:
                await self._fire_slot(0, len(slots), slots[0])
            yield event.plain_result("已手动触发一次醒来。")

