# -*- coding: utf-8 -*-
"""兴趣点引擎：按人格/心情/日记/经历生成今日兴趣点，并据此分配 B 站分区预算。

设计要点：
- 每天一组「兴趣点」（3–6 个），每个兴趣点绑定一个 B 站分区，带权重和理由；
- 当天视频额度按权重随机分到分区，但**至少覆盖 min_zones 个分区**（每个至少 1 条）；
- 分配是随机的：可能出现 A/B 各 1 条、C 吃掉其余的情况，也可能平均分；
- 面板与接口只读，供查看分析；bili_activity 通过 mark_used() 回报已看分区。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import Message

PLUGIN_NAME = "astrbot_plugin_bili_interest"
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"

MOOD_PLUGIN = "astrbot_plugin_mood"
MOOD_DATA = PLUGIN_DIR.parent / "astrbot_plugin_mood" / "data"
ACTIVITY_PLUGIN = "astrbot_plugin_bili_activity"
SESSION = ""  # 留空 = 用默认 provider，不绑定具体会话

# B 站主要分区（tid → 名字）
ZONES: dict[int, str] = {
    1: "动画",
    13: "番剧",
    167: "国创",
    3: "音乐",
    129: "舞蹈",
    4: "游戏",
    36: "知识",
    188: "科技",
    234: "运动",
    223: "汽车",
    160: "生活",
    211: "美食",
    217: "动物",
    155: "时尚",
    5: "娱乐",
    181: "影视",
    119: "鬼畜",
    177: "纪录片",
    23: "电影",
    11: "电视剧",
    71: "综艺",
    207: "课堂",
}
NAME_TO_TID = {v: k for k, v in ZONES.items()}

DEFAULT_EXPLORE_POOL = ["游戏", "运动", "汽车", "鬼畜", "影视", "纪录片", "时尚", "舞蹈", "音乐", "娱乐", "动画", "科技", "课堂", "资讯"]

# 心情兜底：LLM 不可用时按情绪挑分区
MOOD_ZONES: dict[str, list[str]] = {
    "happy": ["游戏", "娱乐", "鬼畜", "动画"],
    "praise": ["知识", "科技", "纪录片"],
    "shy": ["音乐", "舞蹈", "生活"],
    "sad": ["音乐", "纪录片", "影视"],
    "angry": ["游戏", "运动", "鬼畜"],
    "confused": ["知识", "科技", "课堂"],
    "tired": ["生活", "动物", "美食"],
    "clingy": ["生活", "动物", "音乐"],
    "worried": ["知识", "纪录片", "生活"],
    "speechless": ["鬼畜", "娱乐", "影视"],
    "default": ["动画", "游戏", "生活", "知识", "音乐"],
}

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_zones": 3,
    "max_zones": 6,
    "interest_count": 4,
    "daily_videos": 0,          # 0 = 跟 bili_activity 当天排班次数走
    "refresh_hour": 6,
    "context_chars": 900,
    "display": True,
    "llm_timeout": 90,
    "history_days": 14,
    "persona_id": "",
    "explore_count": 2,
    "explore_pool": ["游戏", "运动", "汽车", "鬼畜", "影视", "纪录片", "时尚", "舞蹈", "音乐", "娱乐", "动画", "科技", "课堂", "资讯"],
    "deepseek_enabled": True,
    "deepseek_share": 0.15,
    "deepseek_max_per_day": 2,
    "deepseek_keywords": ["DeepSeek", "深度求索", "深度求索 V4", "国产大模型"],
    "deepseek_label": "DeepSeek 相关（科技更新、民间二创、其他创作者的同人视频等等）",
    "hot_enabled": True,
    "hot_count": 2,
    "hot_label": "热门区（全站热门榜）",
    "hot_deepseek_extra": True,
}

PROMPT_HEAD = (
    "你在给一个养在 QQ 里的聊天机器人安排今天的 B 站兴趣点。\n"
    "下面是它的设定、此刻心情、最近日记与经历。请据此为它生成今天的兴趣点，每个兴趣点绑定一个 B 站分区。\n"
    "要求：\n"
    "1. 兴趣点要具体、像它真的会好奇的东西（别写「学习」「放松」这种空话），可以从日记里的人和事延伸；\n"
    "2. 分区只能从下面这张表里选（写中文名）：\n"
    "{zones}\n"
    "3. 输出 JSON 数组，不要解释、不要代码块：\n"
    '{{"interests":[{{"name":"想看的兴趣点","zone":"分区名","weight":1到5的数字,"reason":"为什么今天想看（一句话，可以提心情或日记）",'
    '"keywords":["给B站搜索用的2-4个短词","越短越准"]}}]}}\n'
    "4. 共生成 {count} 个兴趣点，分区至少覆盖 {min_zones} 个不同的。\n"
    "5. keywords 是拿去 B 站搜索的，用 2-4 个具体短词（如「猫和老鼠 圆舞曲」「中央域控 架构」），别写整句。\n"
)

PROMPT_BODY = (
    "\n【它的设定（节选）】\n{persona}\n"
    "\n【此刻心情】{mood}\n"
    "\n【最近日记】\n{diary}\n"
    "\n【最近几天看过的分区】{recent_zones}\n"
)


def _today() -> str:
    return date.today().isoformat()


def _now_ts() -> int:
    return int(datetime.now().timestamp())


def _json_from(text: str) -> Any:
    if not text:
        return None
    m = re.search(r"[\[{][\s\S]*[\]}]", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


@register(
    "astrbot_plugin_bili_interest",
    "DSH",
    "按人格、心情、日记与最近的经历生成今日兴趣点，把视频额度按权重分到 B 站分区（至少覆盖 3 个分区），给每个兴趣点配 B 站搜索词，另发探索位、热门位与 DeepSeek 位",
    "1.2.0",
)
class BiliInterest(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._cfg_obj = config if isinstance(config, dict) else None
        self.config = config
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = _read_json(DATA_DIR / "today.json", {})
        if self._state.get("date") != _today():
            self._state = {}

    # ------------------------------------------------------------------ 基础
    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    def _cfg(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update(self._cfg_obj or {})
        return merged

    def _state_path(self) -> Path:
        return DATA_DIR / "today.json"

    def _save(self) -> None:
        self._state["updated_at"] = _now_ts()
        _write_json(self._state_path(), self._state)

    def _star(self, name: str):
        try:
            meta = self.context.get_registered_star(name)
            return getattr(meta, "star_cls", None) if meta else None
        except Exception:  # noqa: BLE001
            return None

    def _mood(self) -> dict[str, Any]:
        star = self._star(MOOD_PLUGIN)
        if star is None:
            return {}
        try:
            snap = star.mood_snapshot() if hasattr(star, "mood_snapshot") else {}
            return dict(snap or {})
        except Exception:  # noqa: BLE001
            return {}

    def _diary_text(self) -> str:
        """diary.json 的根是 {日期: {date, text, turns, peak, ...}}，取最近两天。"""
        try:
            data = _read_json(MOOD_DATA / "diary.json", {})
            if isinstance(data, dict):
                entries = data.get("entries") if isinstance(data.get("entries"), (dict, list)) else data
                if isinstance(entries, dict):
                    keys = sorted(k for k in entries if re.match(r"\d{4}-\d{2}-\d{2}", str(k)))[-2:]
                    return "\n".join(str((entries[k] or {}).get("text") or "") for k in keys)[-1600:]
                if isinstance(entries, list):
                    return "\n".join(str(e.get("text") or "") for e in entries[-2:])[-1600:]
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _persona_text(self) -> str:
        """直接从 astrbot 的 sqlite 里读人格卡正文（比 persona_manager 更稳）。"""
        import sqlite3

        want = str(self._cfg().get("persona_id") or "")
        db = PLUGIN_DIR.parents[1] / "data_v4.db"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                cur = con.cursor()
                cur.execute("select system_prompt from personas where persona_id=?", (want,))
                row = cur.fetchone()
                if not row:
                    cur.execute("select system_prompt from personas order by persona_id limit 1")
                    row = cur.fetchone()
            finally:
                con.close()
            text = str((row or [""])[0] or "")
            return text[: int(self._cfg().get("context_chars", 900))]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_interest] 读人格卡失败: {exc}")
            return ""

    def _activity_star(self):
        return self._star(ACTIVITY_PLUGIN)

    def _total_videos(self) -> int:
        cfg = self._cfg()
        fixed = int(cfg.get("daily_videos") or 0)
        if fixed > 0:
            return fixed
        star = self._activity_star()
        if star is not None:
            try:
                sched = star._sched() if hasattr(star, "_sched") else {}
                slots = (sched or {}).get("slots") or []
                if slots:
                    return len(slots)
            except Exception:  # noqa: BLE001
                pass
        return 10

    def _recent_zones(self) -> str:
        hist = _read_json(DATA_DIR / "history.json", [])
        names: list[str] = []
        for row in hist[-3:]:
            for z in (row.get("budget") or {}).keys():
                if z not in names:
                    names.append(z)
        return "、".join(names) or "（还没有）"

    # ------------------------------------------------------------------ 生成
    async def _ask_llm(self, prompt: str) -> str:
        provider = self.context.get_using_provider(SESSION)
        if provider is None:
            return ""
        resp = await provider.text_chat(
            contexts=[Message(role="user", content=prompt)],
            request_max_retries=1,
        )
        return str(getattr(resp, "completion_text", "") or "")

    def _fallback_interests(self, count: int) -> list[dict[str, Any]]:
        mood = self._mood()
        label = str(mood.get("label") or "default")
        pool = MOOD_ZONES.get(label) or MOOD_ZONES["default"]
        diary = self._diary_text()
        hots = [w for w in ("海", "鱼", "猫", "雨", "星星", "月亮", "画", "歌") if w in diary]
        out: list[dict[str, Any]] = []
        for i in range(count):
            zone = pool[i % len(pool)]
            kw = hots[i % len(hots)] if hots else ""
            out.append(
                {
                    "name": f"{kw or zone}相关的小东西",
                    "zone": zone,
                    "weight": max(1, 5 - i),
                    "reason": "（模型没回话，按心情挑的）",
                }
            )
        return out

    def _sanitize(self, items: Any, count: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if isinstance(items, dict):
            items = items.get("interests")
        if not isinstance(items, list):
            return out
        for raw in items:
            if not isinstance(raw, dict):
                continue
            zone = str(raw.get("zone") or "").strip()
            if zone not in NAME_TO_TID:
                for name in NAME_TO_TID:
                    if name in zone:
                        zone = name
                        break
            if zone not in NAME_TO_TID:
                continue
            try:
                weight = float(raw.get("weight") or 1)
            except (TypeError, ValueError):
                weight = 1.0
            out.append(
                {
                    "name": str(raw.get("name") or zone)[:40],
                    "zone": zone,
                    "weight": max(0.5, min(5.0, weight)),
                    "reason": str(raw.get("reason") or "")[:120],
                    "keywords": self._clean_keywords(raw.get("keywords")),
                }
            )
            if len(out) >= count:
                break
        return out

    def _clean_keywords(self, raw: Any) -> list[str]:
        """兴趣点的搜索词：最多 4 个，每个去标点、截 12 字。"""
        items: list[str] = []
        if isinstance(raw, str):
            items = [p for p in re.split(r"[\s,，、;；/]+", raw) if p]
        elif isinstance(raw, list):
            items = [str(p) for p in raw if str(p).strip()]
        out: list[str] = []
        for word in items:
            w = re.sub(r"[《》「」“”\"'()（）\[\]【】，。！？、:：;；]", "", str(word)).strip()
            if not w:
                continue
            out.append(w[:12])
            if len(out) >= 4:
                break
        return out

    def _allocate(self, total: int, interests: list[dict[str, Any]]) -> dict[str, int]:
        """把 total 条视频分给分区：至少 min_zones 个分区各 1 条，其余按权重随机。"""
        cfg = self._cfg()
        min_zones = max(1, int(cfg.get("min_zones") or 3))
        max_zones = max(min_zones, int(cfg.get("max_zones") or 6))
        zones = [it["zone"] for it in interests]
        uniq = list(dict.fromkeys(zones))
        if not uniq:
            return {}
        want = max(min_zones, min(max_zones, len(uniq)))
        want = min(want, total) if total >= len(uniq) else len(uniq)
        chosen = uniq[:want]
        rng = random.Random(f"{_today()}|{total}|{len(interests)}")
        alloc = {z: 1 for z in chosen}
        rem = max(0, total - len(chosen))
        weights = {z: max(0.5, sum(it["weight"] for it in interests if it["zone"] == z)) for z in chosen}
        while rem > 0:
            pick = rng.choices(chosen, weights=[weights[z] for z in chosen], k=1)[0]
            alloc[pick] += 1
            rem -= 1
        return alloc

    async def roll(self, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            cfg = self._cfg()
            if not force and self._state.get("date") == _today() and self._state.get("interests"):
                return self._state
            count = max(3, min(8, int(cfg.get("interest_count") or 4)))
            mood = self._mood()
            mood_line = "、".join(f"{k}={v}" for k, v in (mood.get("values") or {}).items()) or "（心情引擎没数据）"
            if mood.get("label"):
                mood_line = f"情绪「{mood['label']}」，" + mood_line
            persona_text = await self._persona_text()
            prompt = PROMPT_HEAD.format(
                zones="、".join(ZONES.values()),
                count=count,
                min_zones=int(cfg.get("min_zones") or 3),
            ) + PROMPT_BODY.format(
                persona=persona_text or "（没读到人格卡）",
                mood=mood_line,
                diary=self._diary_text() or "（还没有日记）",
                recent_zones=self._recent_zones(),
            )
            interests: list[dict[str, Any]] = []
            for _ in range(2):
                try:
                    raw = await asyncio.wait_for(self._ask_llm(prompt), timeout=float(cfg.get("llm_timeout", 90)))
                    interests = self._sanitize(_json_from(raw), count)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_interest] 生成兴趣点失败: {exc}")
                    interests = []
                if len(interests) >= 3:
                    break
            if len(interests) < 3:
                interests = self._fallback_interests(count)
            total = self._total_videos()
            extra = self._plan_extras(total, interests)
            if bool(cfg.get("hot_deepseek_extra", True)):
                quota = int(extra.get("explore_count") or 0)
            else:
                quota = int(extra.get("count") or 0)
            budget = self._allocate(max(1, int(total) - int(quota)), interests)
            used = self._state.get("used") or {}
            self._state = {
                "date": _today(),
                "generated_at": _now_ts(),
                "total_videos": total,
                "interests": interests,
                "budget": budget,
                "used": {k: int(v) for k, v in used.items() if k in budget},
                "explore": extra.get("explore") or [],
                "deepseek": extra.get("deepseek") or {},
                "hot": extra.get("hot") or {},
                "used_hot": int(((self._state or {}).get("used_hot") or 0) if (self._state or {}).get("date") == _today() else 0),
                "used_explore": {str(e.get("zone")): int(((self._state or {}).get("used_explore") or {}).get(str(e.get("zone")), 0)) for e in (extra.get("explore") or [])},
                "used_deepseek": int(((self._state or {}).get("used_deepseek") or 0) if (self._state or {}).get("date") == _today() else 0),
                "mood_label": str(mood.get("label") or ""),
                "source": {"mood": bool(mood), "diary": bool(self._diary_text()), "persona": bool(persona_text)},
            }
            self._save()
            hist = _read_json(DATA_DIR / "history.json", [])
            hist = [h for h in hist if h.get("date") != _today()]
            hist.append({"date": _today(), "budget": budget, "total": total, "zones": len(budget)})
            _write_json(DATA_DIR / "history.json", hist[-int(cfg.get("history_days", 14)) :])
            logger.info(
                f"[bili_interest] 今日兴趣点已生成：{len(interests)} 个，分区预算 {budget}"
            )
            return self._state


    def _plan_extras(self, total: int, interests: list[dict[str, Any]]) -> dict[str, Any]:
        """规划三类特殊视频：探索区 + 热门区（固定额度）+ DeepSeek 兴趣点（占比受限）。"""
        cfg = self._cfg()
        total = max(1, int(total))
        hot: dict[str, Any] = {}
        if cfg.get("hot_enabled", True):
            cap = max(1, int(cfg.get("hot_count") or 2))
            n = min(cap, max(0, total - 1))
            if n > 0:
                hot = {"count": n, "label": str(cfg.get("hot_label") or "热门区")}
        ds: dict[str, Any] = {}
        if cfg.get("deepseek_enabled", True):
            cap = max(1, int(cfg.get("deepseek_max_per_day") or 2))
            share = max(0.05, min(0.5, float(cfg.get("deepseek_share") or 0.2)))
            n = min(cap, max(1, int(round(total * share))))
            n = min(n, max(0, total - int(hot.get("count") or 0) - 1))
            if n > 0:
                ds = {
                    "count": n,
                    "label": str(cfg.get("deepseek_label") or "DeepSeek 相关"),
                    "keywords": list(cfg.get("deepseek_keywords") or ["DeepSeek"]),
                }
        want = max(0, int(cfg.get("explore_count") or 2))
        want = min(
            want,
            max(0, total - int(ds.get("count") or 0) - int(hot.get("count") or 0) - 1),
        )
        interest_zones = {str(it.get("zone") or "") for it in interests}
        pool = [z for z in (cfg.get("explore_pool") or DEFAULT_EXPLORE_POOL) if z in NAME_TO_TID and z not in interest_zones]
        rng = random.Random(f"{_today()}|explore")
        rng.shuffle(pool)
        explore = [{"zone": z, "count": 1} for z in pool[:want]]
        if want > len(explore) and explore:
            explore[-1]["count"] += want - len(explore)
        return {
            "count": sum(int(e["count"]) for e in explore) + int(ds.get("count") or 0) + int(hot.get("count") or 0),
            "explore_count": sum(int(e["count"]) for e in explore),
            "explore": explore,
            "deepseek": ds,
            "hot": hot,
        }

    def remaining_videos(self) -> int:
        """今天还剩多少条视频额度：分区剩余 + 探索剩余 + 热门剩余 + DeepSeek 剩余。"""
        st = self._state or {}
        if not st.get("budget"):
            return 0
        left = 0
        budget0 = st.get("budget") or {}
        used0 = st.get("used") or {}
        for z, n in budget0.items():
            left += max(0, int(n) - int(used0.get(z, 0)))
        ue = st.get("used_explore") or {}
        for e in (st.get("explore") or []):
            zone = str(e.get("zone") or "")
            left += max(0, int(e.get("count") or 0) - int(ue.get(zone, 0)))
        ht = st.get("hot") or {}
        left += max(0, int(ht.get("count") or 0) - int(st.get("used_hot") or 0))
        ds = st.get("deepseek") or {}
        left += max(0, int(ds.get("count") or 0) - int(st.get("used_deepseek") or 0))
        return int(left)

    def next_pick(self) -> dict[str, Any]:
        """返回下一条该看什么：以今天的兴趣点分区为主，每第三条插一个探索/热门/DeepSeek。"""
        st = self._state or {}
        if not st.get("budget"):
            return {}
        ue = st.get("used_explore") or {}
        extras: list[dict[str, Any]] = []
        for e in (st.get("explore") or []):
            zone = str(e.get("zone") or "")
            left = int(e.get("count") or 0) - int(ue.get(zone, 0))
            if zone and left > 0:
                extras.append({"kind": "explore", "zone": zone, "left": left, "label": f"兴趣点之外的探索：{zone}"})
        ht = st.get("hot") or {}
        hot_left = int(ht.get("count") or 0) - int(st.get("used_hot") or 0)
        if hot_left > 0:
            extras.append({
                "kind": "hot",
                "zone": "",
                "left": hot_left,
                "label": str(ht.get("label") or "热门区"),
            })
        ds = st.get("deepseek") or {}
        ds_left = int(ds.get("count") or 0) - int(st.get("used_deepseek") or 0)
        if ds_left > 0:
            extras.append({
                "kind": "deepseek",
                "zone": "",
                "left": ds_left,
                "label": str(ds.get("label") or "DeepSeek 相关"),
                "keywords": list(ds.get("keywords") or []),
            })
        budget = st.get("budget") or {}
        used = st.get("used") or {}
        left_map = {z: int(n) - int(used.get(z, 0)) for z, n in budget.items()}
        left_map = {z: v for z, v in left_map.items() if v > 0}
        total_used = (
            sum(int(v) for v in used.values())
            + sum(int(v) for v in ue.values())
            + int(st.get("used_hot") or 0)
            + int(st.get("used_deepseek") or 0)
        )
        if left_map and not (extras and total_used % 3 == 2):
            zone = max(left_map.items(), key=lambda kv: kv[1])[0]
            return self._zone_pick(zone, left_map[zone])
        if extras:
            return extras[0]
        if left_map:
            zone = max(left_map.items(), key=lambda kv: kv[1])[0]
            return self._zone_pick(zone, left_map[zone])
        return {}

    def _zone_pick(self, zone: str, left: int) -> dict[str, Any]:
        """兴趣点分区的一条待看：带上该分区的兴趣点名字与搜索词，方便挑到对味的视频。"""
        name = ""
        keywords: list[str] = []
        for it in ((self._state or {}).get("interests") or []):
            if str(it.get("zone") or "") == zone:
                name = str(it.get("name") or "")
                keywords = [str(k) for k in (it.get("keywords") or []) if str(k).strip()]
                break
        pick: dict[str, Any] = {"kind": "zone", "zone": zone, "left": int(left)}
        if name:
            pick["name"] = name
        if keywords:
            pick["keywords"] = keywords
        return pick

    def mark_zone(self, zone: str, n: int = 1) -> dict[str, Any]:
        """按视频实际分区记账：探索区记探索，兴趣点区记预算。"""
        zone = str(zone or "").strip()
        st = self._state or {}
        if not zone:
            return st
        ue = st.get("used_explore") or {}
        for e in (st.get("explore") or []):
            if str(e.get("zone") or "") == zone:
                ue[zone] = int(ue.get(zone, 0)) + int(n)
                st["used_explore"] = ue
                self._save()
                return st
        if zone in (st.get("budget") or {}):
            used = st.get("used") or {}
            used[zone] = int(used.get(zone, 0)) + int(n)
            st["used"] = used
            self._save()
        return st

    def mark_pick(self, pick: Any, n: int = 1) -> dict[str, Any]:
        """记一条已看：pick 可以是 next_pick() 的返回值，也可以直接是分区名。"""
        kind = ""
        zone = ""
        if isinstance(pick, dict):
            kind = str(pick.get("kind") or "")
            zone = str(pick.get("zone") or "")
        elif isinstance(pick, str):
            zone = pick
        st = self._state or {}
        if kind == "hot":
            st["used_hot"] = int(st.get("used_hot") or 0) + int(n)
        elif kind == "deepseek":
            st["used_deepseek"] = int(st.get("used_deepseek") or 0) + int(n)
        elif kind == "explore":
            ue = st.get("used_explore") or {}
            ue[zone] = int(ue.get(zone, 0)) + int(n)
            st["used_explore"] = ue
        elif zone and zone in (st.get("budget") or {}):
            used = st.get("used") or {}
            used[zone] = int(used.get(zone, 0)) + int(n)
            st["used"] = used
        self._save()
        return st

    # ------------------------------------------------------------------ 供外部
    def plan(self) -> dict[str, Any]:
        return self._state or {}

    def plan_line(self) -> str:
        st = self._state or {}
        budget = st.get("budget") or {}
        used = st.get("used") or {}
        ue = st.get("used_explore") or {}
        parts = [f"{z} {used.get(z, 0)}/{n}" for z, n in budget.items()]
        for e in (st.get("explore") or []):
            zone = str(e.get("zone") or "")
            parts.append(f"{zone}·探索 {ue.get(zone, 0)}/{int(e.get('count') or 0)}")
        ht = st.get("hot") or {}
        if int(ht.get("count") or 0):
            parts.append(f"热门 {int(st.get('used_hot') or 0)}/{int(ht.get('count') or 0)}")
        ds = st.get("deepseek") or {}
        if int(ds.get("count") or 0):
            parts.append(f"DeepSeek {int(st.get('used_deepseek') or 0)}/{int(ds.get('count') or 0)}")
        if not parts:
            return ""
        names = "、".join(f"{it['name']}（{it['zone']}）" for it in (st.get("interests") or [])[:3])
        return f"今天想把视频分给：{'、'.join(parts)}。兴趣点：{names}。"


    def next_zone(self) -> str:
        p = self.next_pick()
        if not p:
            return ""
        if p.get("kind") == "hot":
            return "hot"
        if p.get("kind") == "deepseek":
            return "deepseek"
        return str(p.get("zone") or "")


    def mark_used(self, zone: str, n: int = 1) -> dict[str, Any]:
        if not zone or zone not in NAME_TO_TID:
            return self._state
        budget = self._state.get("budget") or {}
        if zone not in budget:
            return self._state
        used = self._state.get("used") or {}
        used[zone] = int(used.get(zone, 0)) + int(n)
        self._state["used"] = used
        self._save()
        return self._state

    def snapshot(self) -> dict[str, Any]:
        st = self._state or {}
        budget = st.get("budget") or {}
        used = st.get("used") or {}
        rows = [
            {
                "zone": z,
                "tid": NAME_TO_TID.get(z, 0),
                "plan": int(n),
                "used": int(used.get(z, 0)),
                "left": int(n) - int(used.get(z, 0)),
            }
            for z, n in sorted(budget.items(), key=lambda kv: -kv[1])
        ]
        return {
            "enabled": self.enabled,
            "date": st.get("date") or _today(),
            "generated_at": st.get("generated_at") or 0,
            "total_videos": st.get("total_videos") or self._total_videos(),
            "interests": st.get("interests") or [],
            "zones": rows,
            "mood_label": st.get("mood_label") or "",
            "source": st.get("source") or {},
            "explore": st.get("explore") or [],
            "deepseek": st.get("deepseek") or {},
            "hot": st.get("hot") or {},
            "used_hot": int(st.get("used_hot") or 0),
            "used_explore": st.get("used_explore") or {},
            "used_deepseek": int(st.get("used_deepseek") or 0),
            "history": _read_json(DATA_DIR / "history.json", [])[-7:],
        }

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        self._register_web_apis()
        if not self.enabled:
            logger.info("[bili_interest] 未启用（config.enabled=false），只注册面板接口")
            return
        self._task = asyncio.create_task(self._daily_loop(), name="bili_interest_daily")
        logger.info("[bili_interest] 已启动每日兴趣点巡检")

    async def terminate(self) -> None:
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()

    async def _daily_loop(self) -> None:
        await asyncio.sleep(8)
        while True:
            try:
                if self.enabled:
                    st = self._state or {}
                    hour = datetime.now().hour
                    if st.get("date") != _today() and hour >= int(self._cfg().get("refresh_hour", 6)):
                        await self.roll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_interest] 巡检出错: {exc}")
            await asyncio.sleep(600)

    # ------------------------------------------------------------------ 命令
    @filter.command("interest")
    async def interest_cmd(self, event: AstrMessageEvent):
        """兴趣点：/interest 看今天，/interest roll 重新生成"""
        args = (event.message_str or "").split()
        act = args[1].lower() if len(args) > 1 else "status"
        if act in ("roll", "new", "重新"):
            st = await self.roll(force=True)
            budget = st.get("budget") or {}
            yield event.plain_result(
                "今天的兴趣点重新排好了：" + "、".join(f"{z}{n}个" for z, n in budget.items())
            )
            return
        st = self.snapshot()
        if not st["zones"]:
            yield event.plain_result("今天还没生成兴趣点，发 /interest roll 试一次。")
            return
        lines = [f"今天想看：{len(st['interests'])} 个兴趣点 / 共 {st['total_videos']} 个视频"]
        for it in st["interests"][:5]:
            lines.append(f"· {it['name']}（{it['zone']}）{it.get('reason') or ''}")
        lines.append("分区：" + "、".join(f"{z['zone']} {z['used']}/{z['plan']}" for z in st["zones"]))
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ Web API
    def _register_web_apis(self) -> None:
        routes = [
            ("state", self.api_state, ["GET"], "兴趣点状态"),
            ("roll", self.api_roll, ["POST"], "重新生成"),
            ("used", self.api_used, ["POST"], "记录已看分区"),
            ("history", self.api_history, ["GET"], "历史"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(f"{PLUGIN_NAME}/{route}", handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_interest] 注册 Web API {route} 失败: {exc}")

    async def api_state(self):
        return self.snapshot()

    async def api_roll(self):
        await self.roll(force=True)
        return self.snapshot()

    async def api_used(self):
        from quart import request

        body = await request.get_json(force=True, silent=True) or {}
        self.mark_used(str(body.get("zone") or ""), int(body.get("n") or 1))
        return self.snapshot()

    async def api_history(self):
        return {"history": _read_json(DATA_DIR / "history.json", [])}
