# -*- coding: utf-8 -*-
"""B 站活动插件：刷视频、看评论、发评论/回复评论（带表情/颜文字），自带排班与每日登录。

联动：
- astrbot_plugin_mood：按当前情绪挑表情；
- astrbot_plugin_bili_interest：按今日兴趣点的分区预算挑视频，发完评论回报已看分区；
- 工作目录\\mentions.json：主人的 @/回复 待处理列表（可用环境变量 BILI_WATCH_DIR 指定）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_bili_activity"
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
LOG_PATH = DATA_DIR / "comment-log.json"
STATS_PATH = DATA_DIR / "stats.json"
PENDING_PATH = DATA_DIR / "pending_events.jsonl"  # 外部脚本（post_comment.py）投递的事件队列
SCHED_PATH = DATA_DIR / "schedule.json"
# DeepSeek 计价高峰：工作日（不含法定节假日）9:00-12:00、14:00-18:00；其余时段（含周末与节假日全天）半价
WATCH_DIR = Path(os.environ.get("BILI_WATCH_DIR") or (DATA_DIR / "watch"))
MENTIONS_PATH = WATCH_DIR / "mentions.json"
OWNER_PATH = DATA_DIR / "owner_queue.json"
SHARE_PATH = DATA_DIR / "share-log.json"
TRAFFIC_PATH = DATA_DIR / "traffic.json"
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

ZONE_TID = {
    "动画": 1, "番剧": 13, "国创": 167, "音乐": 3, "舞蹈": 129, "游戏": 4, "知识": 36,
    "科技": 188, "运动": 234, "汽车": 223, "生活": 160, "美食": 211, "动物": 217,
    "时尚": 155, "娱乐": 5, "影视": 181, "鬼畜": 119, "纪录片": 177, "电影": 23,
    "电视剧": 11, "综艺": 71, "课堂": 207,
}

TID_ZONE = {
    1: "动画", 3: "音乐", 4: "游戏", 5: "娱乐", 11: "电视剧", 13: "番剧",
    17: "单机游戏", 19: "鬼畜", 20: "舞蹈", 21: "生活", 23: "电影", 24: "纪录片",
    25: "影视", 26: "国创", 27: "动画", 28: "动画", 29: "动画", 30: "音乐",
    31: "音乐", 32: "音乐", 33: "动画", 34: "动画", 35: "动画", 36: "知识",
    71: "综艺", 119: "鬼畜", 129: "舞蹈", 155: "时尚", 160: "生活", 167: "国创",
    177: "纪录片", 181: "影视", 188: "科技", 207: "课堂", 211: "美食", 217: "动物",
    223: "汽车", 234: "运动",
}

# B 站新体系的分区号（221 野生动物、250 出行、208 校园学习…）不在 TID_ZONE 里，
# view 接口的 tname 又常是空串，只能靠搜索结果里的 typename 反推大类。
TYPE_HINTS: list[tuple[str, str]] = [
    ("野生", "动物"), ("动物", "动物"), ("海洋", "动物"), ("宠物", "动物"),
    ("喵星人", "动物"), ("汪星人", "动物"), ("爬宠", "动物"), ("动物圈", "动物"),
    ("美食", "美食"), ("探店", "美食"), ("烹饪", "美食"), ("田园", "美食"),
    ("出行", "生活"), ("家居", "生活"), ("手工", "生活"), ("绘画", "生活"),
    ("日常", "生活"), ("三农", "生活"), ("生活", "生活"),
    ("校园", "知识"), ("科普", "知识"), ("科学", "知识"), ("人文", "知识"),
    ("历史", "知识"), ("社科", "知识"), ("财经", "知识"), ("商业", "知识"),
    ("职场", "知识"), ("技能", "知识"), ("科工", "知识"), ("法律", "知识"),
    ("心理", "知识"), ("教育", "知识"), ("知识", "知识"),
    ("音乐", "音乐"), ("演奏", "音乐"), ("翻唱", "音乐"), ("MV", "音乐"),
    ("汽车", "汽车"), ("摩托", "汽车"),
    ("鬼畜", "鬼畜"),
    ("搞笑", "娱乐"), ("娱乐", "娱乐"), ("综艺", "娱乐"), ("明星", "娱乐"),
    ("影视", "影视"), ("电影", "影视"), ("电视", "影视"), ("剪辑", "影视"),
    ("预告", "影视"), ("解说", "影视"),
    ("动画", "动画"), ("番剧", "动画"), ("国创", "动画"), ("二次元", "动画"),
    ("手书", "动画"), ("短片", "动画"), ("MAD", "动画"), ("AMV", "动画"),
    ("科技", "科技"), ("数码", "科技"), ("计算机", "科技"), ("手机", "科技"),
    ("软件", "科技"), ("人工智能", "科技"),
]


def zone_from_type(typename: str = "", typeid: Any = None) -> str:
    """把新体系的 typename/typeid 归到我们用的分区名，推不出来就返回空串。"""
    try:
        zone = TID_ZONE.get(int(typeid)) if str(typeid or "").strip().isdigit() else None
    except Exception:  # noqa: BLE001
        zone = None
    if zone:
        return zone
    name = str(typename or "").strip()
    if not name:
        return ""
    for word, z in TYPE_HINTS:
        if word in name:
            return z
    return ""

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "cookie_path": "",
    "per_video_comment_limit": 6,
    "min_interval_minutes": 1,
    "comment_cooldown_min": 0.5,
    "comment_cooldown_max": 0.5,
    "reply_cooldown_min": 0.5,
    "reply_cooldown_max": 0.5,
    "min_chars": 4,
    "max_chars": 120,
    "allow_emoji": True,
    "allow_kaomoji": True,
    "hot_comment_count": 8,
    "target_session": "",
    "auto_login": True,
    "owner_qq": "",
    "owner_umo": "",
    "daily_share_limit": 3,
    "owner_mid": "",
    "owner_name": "",
    "share_via": "comment",
    "read_hourly_limit": 240,
    "read_daily_limit": 1200,
    "skip_chance": 0.3,
    "ai_flavor_threshold": 6,
    "fav_folder_id": 0,
    "fav_daily_limit": 3,
    "share_min_urge": 0.5,
    "owner_queue_max": 30,
    "mention_poll": True,
}

EMOJI_BY_MOOD: dict[str, list[str]] = {
    "happy": ["[大笑]", "[喜欢]", "[星星眼]", "[吃瓜]", "[doge]", "[给心心]", "[打call]"],
    "praise": ["[鼓掌]", "[点赞]", "[给心心]", "[帅]", "[滑稽]", "[支持]"],
    "shy": ["[害羞]", "[捂脸]", "[笑哭]", "[尴尬]", "[委屈]"],
    "sad": ["[委屈]", "[大哭]", "[难过]", "[叹气]", "[失落]"],
    "angry": ["[生气]", "[发怒]", "[嫌弃]", "[白眼]", "[tv_白眼]"],
    "confused": ["[疑惑]", "[思考]", "[问号脸]", "[呆]", "[tv_黑人问号]"],
    "tired": ["[困]", "[睡觉]", "[打哈欠]", "[叹气]"],
    "clingy": ["[抱抱]", "[给心心]", "[摸头]", "[贴贴]", "[害羞]"],
    "worried": ["[紧张]", "[害怕]", "[疑惑]", "[叹气]"],
    "speechless": ["[无语]", "[tv_白眼]", "[吃瓜]", "[滑稽]"],
    "default": ["[doge]", "[吃瓜]", "[滑稽]", "[大笑]", "[星星眼]"],
}

KAOMOJI = [
    "( ゜- ゜)つロ",
    "_(:з」∠)_",
    "(๑•̀ㅂ•́)و✧",
    "(｡･ω･｡)",
    "(=・ω・=)",
    "⌓‿⌓",
    "(´･_･`)",
    "( ˘ω˘ )",
    "(๑´ω`๑)",
    "(・∀・)",
    "ヾ(≧▽≦*)o",
]

BANNED = ["代刷", "扫码", "加群", "私信我", "微信", "QQ群", "免费领", "看简介", "点我头像"]
SENSITIVE = ["傻逼", "操你", "死妈", "自杀", "约炮", "裸聊"]


def _today() -> str:
    return date.today().isoformat()


def _now_text() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _hhmm_minutes(text: str) -> int:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(text).strip())
    if not m:
        return 8 * 60
    h, mi = int(m.group(1)), int(m.group(2))
    return max(0, min(24 * 60, h * 60 + mi))


def pick_emoji(mood: str, allow_emoji: bool, allow_kaomoji: bool) -> str:
    pool: list[str] = []
    if allow_emoji:
        pool.extend(EMOJI_BY_MOOD.get(mood) or EMOJI_BY_MOOD["default"])
    if allow_kaomoji:
        pool.extend(KAOMOJI)
    return random.choice(pool) if pool else ""


def check_text(text: str, cfg: dict) -> tuple[bool, str]:
    body = (text or "").strip()
    lo = int(cfg.get("min_chars") or 4)
    hi = int(cfg.get("max_chars") or 120)
    if len(body) < lo:
        return False, f"太短了（{len(body)} 字，至少要 {lo} 字）"
    if len(body) > hi:
        return False, f"太长了（{len(body)} 字，最多 {hi} 字）"
    if "http" in body.lower() or "www." in body.lower():
        return False, "别放链接"
    if re.search(r"[Qq]{2}\s*\d{5,}", body):
        return False, "别放 QQ 号"
    for w in BANNED:
        if w in body:
            return False, f"含有不该出现的词：{w}"
    for w in SENSITIVE:
        if w in body:
            return False, "含有敏感词"
    return True, ""


class BiliClient:
    def __init__(self, cookie_file: Path) -> None:
        raw = _load_json(cookie_file, {}) or {}
        # 兼容 {"cookies": {"SESSDATA": ...}} 与扁平的 {"SESSDATA": ...} 两种导出格式
        if isinstance(raw.get("cookies"), dict):
            raw = raw["cookies"]
        self.cookies = {str(k): str(v) for k, v in raw.items() if v not in (None, "")}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        for k, v in self.cookies.items():
            try:
                self.session.cookies.set(str(k), str(v), domain=".bilibili.com")
            except Exception:  # noqa: BLE001
                continue
        self.csrf = str(self.cookies.get("bili_jct") or "")

    def get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get("https://api.bilibili.com" + path, params=params or {}, timeout=20)
        return r.json()

    def post(self, path: str, data: dict) -> dict:
        r = self.session.post("https://api.bilibili.com" + path, data=data, timeout=20)
        return r.json()

    def nav(self) -> dict:
        return self.get("/x/web-interface/nav").get("data") or {}

    def video(self, bv: str) -> dict:
        data = self.get("/x/web-interface/view", {"bvid": bv})
        if data.get("code") != 0:
            raise RuntimeError(f"取视频信息失败：{data.get('message')}")
        return data["data"]

    def playurl(self, bv: str) -> dict:
        """取最低码率的 dash 视频流（抽帧用，不追求清晰）。"""
        cid = self.cid(bv)
        data = self.get("/x/player/playurl",
                        {"bvid": bv, "cid": cid, "qn": 32, "fnval": 16, "fourk": 0})
        if data.get("code") != 0:
            raise RuntimeError(f"取播放地址失败：{data.get('message')}")
        dash = ((data.get("data") or {}).get("dash") or {})
        vids = [v for v in (dash.get("video") or []) if v.get("baseUrl") or v.get("backupUrl")]
        if not vids:
            raise RuntimeError("这个视频没有 dash 视频流")
        best = sorted(vids, key=lambda x: x.get("bandwidth") or 0)[0]
        return {"url": best.get("baseUrl") or (best.get("backupUrl") or [""])[0],
                "width": best.get("width"), "height": best.get("height"),
                "bandwidth": best.get("bandwidth")}

    def audio_url(self, bv: str) -> dict:
        """单独取音频流（听音用）：比拖整条视频省流量。"""
        cid = self.cid(bv)
        data = self.get("/x/player/playurl",
                        {"bvid": bv, "cid": cid, "qn": 32, "fnval": 16, "fourk": 0})
        if data.get("code") != 0:
            raise RuntimeError(f"取音频地址失败：{data.get('message')}")
        dash = ((data.get("data") or {}).get("dash") or {})
        auds = [a for a in (dash.get("audio") or []) if a.get("baseUrl") or a.get("backupUrl")]
        if not auds:
            return {}
        best = sorted(auds, key=lambda x: x.get("bandwidth") or 0)[-1]
        return {"url": best.get("baseUrl") or (best.get("backupUrl") or [""])[0],
                "bandwidth": best.get("bandwidth")}

    def msgfeed(self, kind: str = "at", limit: int = 20) -> list[dict]:
        """拉「@我的」或「回复我的」消息。kind: at | reply"""
        data = self.get(f"/x/msgfeed/{kind}", {"pn": 1, "ps": max(1, min(limit, 30))})
        if data.get("code") != 0:
            raise RuntimeError(f"取 {kind} 消息失败：{data.get('message')}")
        out = []
        for it in ((data.get("data") or {}).get("items") or []):
            item = it.get("item") or {}
            rid = str(it.get("id") or item.get("business_id") or item.get("source_id") or "")
            if not rid:
                continue
            user = it.get("user") or {}
            _src = str(item.get("source_id") or "")
            _root = str(item.get("root_id") or "")
            out.append({
                "kind": kind,
                "id": rid,
                "mid": user.get("mid") or 0,
                "user": user.get("nickname") or "",
                "text": str(item.get("source_content") or item.get("title") or "")[:200],
                "uri": item.get("uri") or "",
                "rpid": _src,
                "root": (_root if _root and _root != "0" else _src),
                "oid": str(item.get("subject_id") or ""),
                "ts": it.get("reply_time") or it.get("at_time") or 0,
            })
        return out

    def comments(self, bv: str, sort: int = 2, limit: int = 8) -> list[dict]:
        aid = self.video(bv)["aid"]
        data = self.get("/x/v2/reply", {"oid": aid, "type": 1, "pn": 1, "ps": max(limit, 20), "sort": sort})
        if data.get("code") != 0:
            raise RuntimeError(f"取评论失败：{data.get('message')}")
        return (data.get("data") or {}).get("replies") or []

    def add_comment(self, bv: str, text: str) -> dict:
        aid = self.video(bv)["aid"]
        return self.post("/x/v2/reply/add", {"oid": aid, "type": 1, "message": text, "csrf": self.csrf, "plat": 1})

    def exp_info(self) -> dict:
        nav = self.nav() or {}
        li = nav.get("level_info") or {}
        return {"level": li.get("current_level"), "exp": li.get("current_exp"), "next": li.get("next_exp")}

    def heartbeat(self, bv: str, played: int = 40) -> dict:
        v = self.video(bv)
        aid, cid = v.get("aid"), v.get("cid")
        if not aid or not cid:
            return {"code": -1, "message": "no aid/cid"}
        now = int(time.time())
        return self.post("/x/click-interface/web/heartbeat", {
            "aid": aid, "cid": cid, "type": 3, "sub_type": 0, "dt": 2, "play_type": 0,
            "played_time": played, "real_time": played, "realtime": played,
            "start_ts": now - played, "csr": self.csrf, "csrf": self.csrf,
        })

    def share_add(self, bv: str) -> dict:
        """调用 B 站「分享」接口（每日分享任务加经验）。"""
        aid = self.video(bv)["aid"]
        return self.post("/x/web-interface/share/add", {"aid": aid, "csrf": self.csrf})

    def reply(self, bv: str, text: str, rpid: int, root: int = 0) -> dict:
        aid = self.video(bv)["aid"]
        return self.post(
            "/x/v2/reply/add",
            {
                "oid": aid,
                "type": 1,
                "message": text,
                "root": int(root or rpid),
                "parent": int(rpid),
                "csrf": self.csrf,
                "plat": 1,
            },
        )

    def cid(self, bv: str) -> int:
        return int(self.video(bv).get("cid") or 0)

    def subtitles(self, bv: str) -> list[dict]:
        """官方字幕：/x/player/v2 的 subtitle.subtitles[].subtitle_url -> 逐句文本。"""
        info = self.video(bv)
        cid = int(info.get("cid") or 0)
        data = self.get("/x/player/v2", {"bvid": bv, "cid": cid})
        subs = (((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        out: list[dict] = []
        for s in subs:
            url = str(s.get("subtitle_url") or "")
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            try:
                r = self.session.get(url, timeout=20)
                body = (r.json() or {}).get("body") or []
            except Exception:  # noqa: BLE001
                continue
            lines = []
            for seg in body:
                t = float(seg.get("from") or 0)
                txt = str(seg.get("content") or "").strip()
                if txt:
                    lines.append(f"[{int(t // 60):02d}:{int(t % 60):02d}] {txt}")
            if lines:
                out.append({"lan": s.get("lan_doc") or s.get("lan") or "?", "lines": lines})
        return out

    def danmaku(self, bv: str) -> list[tuple[float, str]]:
        """弹幕 XML：comment.bilibili.com/<cid>.xml，返回 [(秒, 文本)]。"""
        cid = self.cid(bv)
        if not cid:
            return []
        r = self.session.get(f"https://comment.bilibili.com/{cid}.xml", timeout=20)
        r.encoding = "utf-8"
        out: list[tuple[float, str]] = []
        for m in re.finditer(r'<d p="([^"]+)"[^>]*>(.*?)</d>', r.text):
            parts = m.group(1).split(",")
            try:
                t = float(parts[0])
            except Exception:  # noqa: BLE001
                continue
            out.append((t, m.group(2)))
        return out

    def fav_folders(self) -> list[dict]:
        uid = str(self.nav().get("mid") or "")
        if not uid:
            return []
        data = self.get("/x/v3/fav/folder/created/list-all", {"up_mid": uid})
        return ((data.get("data") or {}).get("list") or [])

    def fav_add(self, bv: str, folder_id: int) -> dict:
        aid = self.video(bv)["aid"]
        return self.post("/x/v3/fav/resource/deal", {
            "rid": aid, "type": 2, "add_media_ids": int(folder_id), "csrf": self.csrf,
        })

    def search(self, keyword: str, limit: int = 20) -> list[dict]:
        data = self.get("/x/web-interface/search/all/v2", {"keyword": keyword}).get("data") or {}
        out: list[dict] = []
        for group in data.get("result") or []:
            if group.get("result_type") != "video":
                continue
            for v in group.get("data") or []:
                title = str(v.get("title") or "").replace('<em class="keyword">', "").replace("</em>", "")
                if v.get("bvid"):
                    out.append({
                        "bvid": v["bvid"],
                        "title": title,
                        "author": v.get("author"),
                        "play": v.get("play"),
                        "typeid": v.get("typeid"),
                        "typename": v.get("typename"),
                    })
                if len(out) >= limit:
                    return out
        return out

    def ranking(self, tid: int, limit: int = 30) -> list[dict]:
        data = self.get("/x/web-interface/ranking/v2", {"rid": tid, "type": "all"})
        if data.get("code") != 0:
            return []
        out = []
        for v in (data.get("data") or {}).get("list") or []:
            if v.get("bvid"):
                out.append(v)
            if len(out) >= limit:
                break
        return out


def _load_frames():
    """frames.py 单独放，能被独立测；这里按文件路径加载，避免包名冲突。"""
    import importlib.util
    fp = PLUGIN_DIR / "frames.py"
    spec = importlib.util.spec_from_file_location("bili_activity_frames", fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FRAMES = _load_frames()


def _load_listen():
    """listen.py 同理：单独放，按文件路径加载。"""
    import importlib.util
    fp = PLUGIN_DIR / "listen.py"
    spec = importlib.util.spec_from_file_location("bili_activity_listen", fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LISTEN = _load_listen()


@register(
    PLUGIN_NAME,
    "DSH",
    "让它在 B 站过日子：按兴趣分区挑视频、读字幕/弹幕/抽帧/听音轨，发评论和回复（带表情/颜文字），把好玩的分享或收藏给主人；排班交给 wake_plan，本插件只管每日打卡、抓 @、发内容",
    "2.4.4",
)
class Main(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self._cfg_obj = config if isinstance(config, dict) else None
        self.config = config
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._mention_task: asyncio.Task | None = None
        self._client_cache: BiliClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    def _cfg(self) -> dict:
        merged = dict(DEFAULTS)
        merged.update(self._cfg_obj or {})
        return merged

    # ---- 主人身份（B 站 UID ↔ 主人） ----
    def _owner_mid(self) -> str:
        return str(self._cfg().get("owner_mid") or "").strip()

    def _owner_name(self) -> str:
        return str(self._cfg().get("owner_name") or "").strip()

    def _is_owner_mid(self, mid) -> bool:
        om = self._owner_mid()
        return bool(om) and str(mid or "").strip() == om

    def _who(self, uname, mid) -> str:
        name = str(uname or "?").strip()
        return f"【主人】{name}" if self._is_owner_mid(mid) else name

    # ---- 读预算（照 dsh-bili-whale 的 traffic guard：读也有上限） ----
    def _traffic(self) -> dict:
        t = _load_json(TRAFFIC_PATH, {}) or {}
        now = time.time()
        day = _today()
        hour = int(now // 3600)
        if t.get("day") != day:
            t = {"day": day, "read_day": 0, "hour": hour, "read_hour": 0}
        elif int(t.get("hour") or 0) != hour:
            t["hour"] = hour
            t["read_hour"] = 0
        return t

    def _traffic_ok(self, cost: int = 1) -> tuple[bool, str]:
        cfg = self._cfg()
        t = self._traffic()
        hourly = int(cfg.get("read_hourly_limit") or 240)
        daily = int(cfg.get("read_daily_limit") or 1200)
        if int(t.get("read_hour") or 0) + cost > hourly:
            return False, f"这一小时已经读了 {t.get('read_hour')} 次接口，歇到下一个整点"
        if int(t.get("read_day") or 0) + cost > daily:
            return False, f"今天已经读了 {t.get('read_day')} 次接口，明天再刷"
        return True, ""

    def _traffic_bump(self, cost: int = 1) -> None:
        t = self._traffic()
        t["read_hour"] = int(t.get("read_hour") or 0) + cost
        t["read_day"] = int(t.get("read_day") or 0) + cost
        _save_json(TRAFFIC_PATH, t)

    def _client(self) -> BiliClient:
        if self._client_cache is not None:
            return self._client_cache
        cfg = self._cfg()
        candidates: list[Path] = []
        if cfg.get("cookie_path"):
            candidates.append(Path(str(cfg["cookie_path"])))
        candidates.append(WATCH_DIR / "cookie.json")
        candidates.append(PLUGIN_DIR / "bili_cookie.json")
        for path in candidates:
            if path.exists():
                self._client_cache = BiliClient(path)
                logger.info(f"[bili_activity] 使用 cookie: {path}")
                return self._client_cache
        raise RuntimeError("找不到 cookie.json")

    def _star(self, name: str):
        try:
            meta = self.context.get_registered_star(name)
            return getattr(meta, "star_cls", None) if meta else None
        except Exception:  # noqa: BLE001
            return None

    def _mood_category(self) -> str:
        star = self._star("astrbot_plugin_mood")
        if star is None or not hasattr(star, "mood_category"):
            return "default"
        try:
            return str(star.mood_category() or "default")
        except Exception:  # noqa: BLE001
            return "default"

    def _interest_zone(self) -> str:
        star = self._star("astrbot_plugin_bili_interest")
        if star is None or not hasattr(star, "next_zone"):
            return ""
        try:
            return str(star.next_zone() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _interest_pick(self) -> dict:
        star = self._star("astrbot_plugin_bili_interest")
        if star is None:
            return {}
        try:
            if hasattr(star, "next_pick"):
                pick = star.next_pick()
                if isinstance(pick, dict) and pick:
                    return pick
            zone = str(star.next_zone() or "") if hasattr(star, "next_zone") else ""
            if zone == "deepseek":
                return {"kind": "deepseek", "label": "DeepSeek 相关"}
            return {"kind": "zone", "zone": zone} if zone else {}
        except Exception:  # noqa: BLE001
            return {}

    def _interest_plan_line(self) -> str:
        star = self._star("astrbot_plugin_bili_interest")
        if star is None or not hasattr(star, "plan_line"):
            return ""
        try:
            return str(star.plan_line() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _interest_mark(self, pick) -> None:
        star = self._star("astrbot_plugin_bili_interest")
        if star is None or not pick:
            return
        try:
            if isinstance(pick, dict) and hasattr(star, "mark_pick"):
                star.mark_pick(pick, 1)
            elif isinstance(pick, str) and hasattr(star, "mark_used"):
                star.mark_used(pick, 1)
        except Exception:  # noqa: BLE001
            pass


    def _zone_of_video(self, v: dict) -> str:
        """视频分区名：优先 tname（B 站新接口有时给空串），退回 tid 映射。"""
        zone = str((v or {}).get("tname") or "").strip()
        if zone:
            return zone
        try:
            return TID_ZONE.get(int((v or {}).get("tid") or 0), "")
        except Exception:  # noqa: BLE001
            return ""

    def _mark_watched(self, bv: str, pick: dict, v: dict) -> None:
        """看过一个视频就把兴趣点额度记上，不再等到发评论才记。"""
        if not bv or getattr(self, "_marked_bv", "") == bv:
            return
        star = self._star("astrbot_plugin_bili_interest")
        if star is None:
            return
        try:
            if isinstance(pick, dict) and pick.get("kind") and hasattr(star, "mark_pick"):
                star.mark_pick(pick, 1)
                self._marked_bv = bv
                logger.info(f"[bili_activity] 兴趣点已记 {pick}（{bv}）")
                return
            zone = self._zone_of_video(v)
            if zone and hasattr(star, "mark_zone"):
                star.mark_zone(zone, 1)
                self._marked_bv = bv
                logger.info(f"[bili_activity] 兴趣点分区「{zone}」已用 +1（{bv}）")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_activity] 记兴趣点失败: {exc}")

    def _stats(self) -> dict:
        return _load_json(STATS_PATH, {"comments": 0, "replies": 0, "errors": 0})

    def _bump(self, key: str, n: int = 1) -> None:
        st = self._stats()
        st[key] = int(st.get(key) or 0) + n
        _save_json(STATS_PATH, st)

    def _log(self) -> dict:
        log = _load_json(LOG_PATH, {})
        if log.get("date") != _today():
            log = {"date": _today(), "count": 0, "last_ts": 0, "bvs": [], "items": [],
                   "by_bv": {}, "owner_shares": []}
            _save_json(LOG_PATH, log)
        return log

    # ---------- 主人 @ 的视频队列 ----------
    def _owner_list(self) -> list[dict]:
        data = _load_json(OWNER_PATH, {"items": []})
        items = data.get("items")
        return items if isinstance(items, list) else []

    def _owner_save(self, items: list[dict]) -> None:
        _save_json(OWNER_PATH, {"items": items[-int(self._cfg().get("owner_queue_max") or 30):]})

    def _owner_add(self, bv: str, note: str = "") -> bool:
        bv = (bv or "").strip()
        if not bv:
            return False
        items = self._owner_list()
        for it in items:
            if it.get("bv") == bv and not it.get("done"):
                return False
        try:
            title = (self._client().video(bv) or {}).get("title") or ""
        except Exception:  # noqa: BLE001
            title = ""
        items.append(
            {
                "id": f"{int(time.time())}-{bv[-4:]}",
                "bv": bv,
                "title": title,
                "note": (note or "")[:80],
                "added_at": time.time(),
                "added": _now_text(),
                "done": False,
            }
        )
        self._owner_save(items)
        return True

    def _owner_pending(self) -> list[dict]:
        return [it for it in self._owner_list() if not it.get("done")]

    def _owner_take(self, bv: str) -> dict | None:
        items = self._owner_list()
        hit = None
        for it in items:
            if it.get("bv") == bv and not it.get("done"):
                it["done"] = True
                it["done_at"] = _now_text()
                hit = it
        if hit:
            self._owner_save(items)
        return hit

    def _is_owner_bv(self, bv: str) -> bool:
        return any(it.get("bv") == bv and not it.get("done") for it in self._owner_list())

    def _resolve_b23(self, code: str) -> str:
        try:
            r = requests.get(
                f"https://b23.tv/{code}", headers={"User-Agent": UA}, allow_redirects=True, timeout=10
            )
            m = BV_RE.search(r.url or "")
            return m.group(0) if m else ""
        except Exception:  # noqa: BLE001
            return ""

    def _mood_nudge(self, delta: dict, note: str) -> None:
        """把看视频/被点名这类事件推给心情引擎。"""
        try:
            star = self._star("astrbot_plugin_mood")
            if star is not None and hasattr(star, "nudge"):
                got = star.nudge(delta, note=note)
                if got:
                    logger.info(f"[bili_activity] 心情推动 {got}（{note}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 心情推动失败: {exc}")

    def _memory_add(self, content: str, meta: dict) -> None:
        """把「今天看了什么」写进 LivingMemory（可选，失败不影响主流程）。

        注意：LivingMemory 的 add_memory 是 async 的，直接调用只会拿到一个
        从未被 await 的 coroutine（记忆静默丢失），必须丢进事件循环。
        """
        try:
            star = self._star("astrbot_plugin_livingmemory")
            if star is None:
                return
            engine = getattr(getattr(star, "command_handler", None), "memory_engine", None)
            if engine is None:
                engine = getattr(getattr(star, "initializer", None), "memory_engine", None)
            if engine is None or not hasattr(engine, "add_memory"):
                return
            res = engine.add_memory(
                content=content,
                session_id="livingmemory:global",
                importance=0.45,
                metadata={"type": "bili_activity", **meta},
            )
            if asyncio.iscoroutine(res):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    res.close()
                    logger.warning("[bili_activity] 写记忆失败: 当前没有事件循环")
                    return
                task = loop.create_task(res)
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 写记忆失败: {exc}")

    # ---------- 分享欲 ----------
    def _share_log(self) -> dict:
        log = _load_json(SHARE_PATH, {})
        if log.get("date") != _today():
            log = {"date": _today(), "count": 0, "items": []}
            _save_json(SHARE_PATH, log)
        return log

    def _share_urge(self) -> tuple[float, str]:
        """按心情算「分享欲」：心情好、亲近、不紧张就想分享。"""
        vals = self._mood_values()
        if not vals:
            return 0.55, "拿不到心情数据，按中等算"
        try:
            valence = float(vals.get("valence", 55))
            affinity = float(vals.get("affinity", 55))
            stress = float(vals.get("stress", 40))
        except (TypeError, ValueError):
            return 0.55, "心情数据有问题"
        urge = (valence * 0.45 + affinity * 0.45 + (100 - stress) * 0.10) / 100.0
        urge = max(0.0, min(1.0, urge))
        detail = f"心情 {valence:.0f}／亲近 {affinity:.0f}／压力 {stress:.0f}"
        return round(urge, 2), detail

    # ---- 沉默衰减（照 dsh-bili-whale：3 天没回应开始降，7 天归零但日记照写）----
    def _owner_seen_path(self):
        return DATA_DIR / "owner-seen.json"

    def _owner_seen_days(self) -> int:
        data = _load_json(self._owner_seen_path(), {}) or {}
        last = float(data.get("last_ts") or 0)
        if not last:
            return 0
        return int((time.time() - last) / 86400)

    def _mark_owner_seen(self, note: str = "") -> None:
        data = _load_json(self._owner_seen_path(), {}) or {}
        data["last_ts"] = time.time()
        data["last_note"] = note[:80]
        _save_json(self._owner_seen_path(), data)

    def _share_cap(self) -> tuple[int, str]:
        cfg = self._cfg()
        base = int(cfg.get("daily_share_limit") or 3)
        days = self._owner_seen_days()
        if days >= 7:
            return 0, f"已经 {days} 天没被主人理过了，今天不想分享"
        if days >= 3:
            return max(1, base - 1), f"已经 {days} 天没被理，今天少分享一点"
        return base, ""

    # ---- AI 味自审（便宜启发式，不额外调模型）----
    AI_SMELL = ("首先", "其次", "总的来说", "综上", "值得一提的是", "不难发现",
                "总结一下", "总而言之", "希望对你有帮助", "作为一个", "需要注意的是")
    AI_STRUCT = ("1.", "2.", "3.", "①", "②", "③", "- ", "**")

    def _ai_flavor(self, text: str) -> tuple[int, list[str]]:
        hits = [w for w in self.AI_SMELL if w in text]
        score = len(hits) * 2
        score += sum(1 for w in self.AI_STRUCT if w in text)
        if len(text) > 80:
            score += 1
        return score, hits

    def _quota_state(self, bv: str = "", count: int = 1, is_comment: bool = False) -> tuple[bool, str, dict]:
        """额度按**视频**算：同一个视频与陌生人的互动（评论+回复）最多 N 条。

        @主人 的分享、和主人的互动都不走这里，所以不消耗额度。
        """
        cfg = self._cfg()
        log = self._log()
        by_bv = log.setdefault("by_bv", {}) or {}
        cap = int(cfg.get("per_video_comment_limit") or 6)
        used = int(by_bv.get(bv) or 0)
        if used + count > cap:
            return False, f"这个视频已经和陌生人互动 {used} 条了，到上限 {cap}", log
        gap_min = cfg.get("min_interval_minutes", 1)
        gap = int(1 if gap_min is None else gap_min) * 60
        last = float(log.get("last_ts") or 0)
        cooldown = float(log.get("next_ok_ts") or 0)
        if is_comment and cooldown and time.time() < cooldown:
            left = int((cooldown - time.time()) / 60) + 1
            return False, f"刚发过评论，按人类节奏还要再等 {left} 分钟", log
        if last and time.time() - last < gap:
            left = int((gap - (time.time() - last)) / 60) + 1
            return False, f"离上一条才 {int((time.time() - last) / 60)} 分钟，还要等 {left} 分钟", log
        return True, "", log

    def _quota_bump(self, log: dict, bv: str, count: int = 1) -> dict:
        by_bv = log.setdefault("by_bv", {}) or {}
        by_bv[bv] = int(by_bv.get(bv) or 0) + count
        log["by_bv"] = by_bv
        log["count"] = int(log.get("count") or 0) + count
        log["last_ts"] = time.time()
        return log

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def collect_owner_video(self, event: AstrMessageEvent):
        """主人在 QQ 里发/转的 B 站视频，自动排进「下次醒来先看」的队列。"""
        try:
            if not self._cfg().get("enabled", True):
                return
            owner = str(self._cfg().get("owner_qq") or "").strip()
            sender = str(event.get_sender_id() or "")
            if not owner or sender != owner:
                return
            text = str(event.message_str or "")
            for seg in getattr(getattr(event, "message_obj", None), "message", []) or []:
                t = getattr(seg, "text", None)
                if t:
                    text += " " + str(t)
            bv = ""
            m = BV_RE.search(text)
            if m:
                bv = m.group(0)
            else:
                m2 = re.search(r"b23\.tv/([0-9A-Za-z]+)", text)
                if m2:
                    bv = self._resolve_b23(m2.group(1))
            if not bv:
                return
            if self._owner_add(bv, text[:80]):
                logger.info(f"[bili_activity] 已记下主人让看的视频 {bv}，下次醒来优先看")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 记录主人视频失败: {exc}")

    @filter.llm_tool(name="bili_owner_list")
    async def bili_owner_list(self, event: AstrMessageEvent) -> str:
        """看主人留的、下次醒来先看的视频清单（不占额度）。"""
        pending = self._owner_pending()
        if not pending:
            return "主人暂时没让你看什么视频。"
        lines = [f"主人让你看的有 {len(pending)} 个："]
        for i, it in enumerate(pending[-10:], 1):
            lines.append(f"{i}. {it.get('title') or '(没拿到标题)'}｜BV {it.get('bv')}｜{it.get('added')}")
        lines.append("用 bili_watch 传对应 BV 去看即可，看完会自动划掉，不占今天的额度。")
        return "\n".join(lines)

    @filter.llm_tool(name="bili_watch")
    async def bili_watch(self, event: AstrMessageEvent, bv: str = "", subs: int = 15, hot: int = 3) -> str:
        """打开视频：标题、UP、简介、热评，顺手带几行字幕。

        Args:
            bv(string): 视频号；留空就按兴趣分区挑一个热门
            subs(number): 顺手带几句官方字幕，默认 15；0 不带，999 全要
            hot(number): 带几条热评，默认 3；0 不带"""
        try:
            client = self._client()
            zone = ""
            pick: dict = {}
            if not bv:
                pick = self._interest_pick()
                kind = str(pick.get("kind") or "")
                if kind == "deepseek":
                    words = [str(w) for w in (pick.get("keywords") or []) if str(w).strip()]
                    cands: list[dict] = []
                    for word in words[:4]:
                        try:
                            cands.extend(client.search(word, 20))
                        except Exception:  # noqa: BLE001
                            continue
                    seen = set(self._log().get("bvs") or [])
                    cands = [c for c in cands if c.get("bvid") and c["bvid"] not in seen]
                    if cands:
                        bv = random.choice(cands[:30])["bvid"]
                elif kind in ("explore", "zone"):
                    zone = str(pick.get("zone") or "")
                    tid = ZONE_TID.get(zone, 0)
                    words = [w for w in " ".join(str(k) for k in (pick.get("keywords") or [])).split() if w]
                    if not words:
                        cleaned = re.sub(r"[《》「」“”\"'()（）\[\]【】，。！？、:：;；]", " ", str(pick.get("name") or ""))
                        words = [w for w in cleaned.split() if len(w) >= 2][:4]
                    seen_zone = set(self._log().get("bvs") or [])
                    # 先用关键词搜（贴兴趣点），再用搜索结果自带的 typename 归一下大类确认分区
                    picked: list[str] = []
                    if words:
                        try:
                            for hit in client.search(" ".join(words), 20):
                                bvv = str(hit.get("bvid") or "")
                                if not bvv or bvv in seen_zone:
                                    continue
                                if zone_from_type(hit.get("typename"), hit.get("typeid")) == zone:
                                    picked.append(bvv)
                        except Exception:  # noqa: BLE001
                            picked = []
                    if picked:
                        bv = random.choice(picked[:8])
                    else:
                        ranked = client.ranking(tid, 100) if tid else []
                        cands = [
                            (v["bvid"], str(v.get("title") or ""))
                            for v in ranked
                            if v.get("bvid") and v["bvid"] not in seen_zone
                        ]
                        if cands:
                            # 排行榜保证分区对得上，再按标题命中兴趣点关键词来排，两条都满足才优先
                            hits: list[str] = []
                            if words:
                                scored = sorted(
                                    (sum(1 for w in words if w in title), bv) for bv, title in cands
                                )
                                hits = [bv for s, bv in reversed(scored) if s > 0]
                            pool = hits[:8] or [bv for bv, _ in cands[:15]]
                            bv = random.choice(pool)
                if not bv:
                    pop = client.get("/x/web-interface/popular", {"ps": 20, "pn": 1}).get("data") or {}
                    items = [v for v in (pop.get("list") or []) if v.get("bvid")]
                    seen_hot = set(self._log().get("bvs") or [])
                    fresh = [v for v in items if v["bvid"] not in seen_hot] or items
                    if not fresh:
                        return "没拿到热门列表，B 站接口可能限流了。"
                    bv = random.choice(fresh)["bvid"]
            self._last_pick = pick or {}
            self._last_pick_bv = bv
            v = client.video(bv)
            is_owner = self._is_owner_bv(bv)
            if is_owner:
                self._owner_take(bv)
            self._owner_watch_bv = bv if is_owner else ""
            if not is_owner:
                self._mark_watched(bv, pick, v)
            stat = v.get("stat") or {}
            lines = [
                ("【主人让你看的，不占今天额度】" if is_owner else "")
                + f"《{v.get('title')}》（分区：{v.get('tname')}）",
                f"UP：{(v.get('owner') or {}).get('name')}｜时长 {v.get('duration')} 秒｜"
                f"{stat.get('view')} 播放｜{stat.get('like')} 赞｜{stat.get('reply')} 评论",
                f"简介：{(v.get('desc') or '')[:200]}",
                f"BV：{bv}",
            ]
            try:
                _hot_n = max(0, int(float(hot)) if str(hot).strip() not in ('', 'None') else 3)
                comments = client.comments(bv, sort=2, limit=_hot_n) if _hot_n else []
            except Exception:  # noqa: BLE001
                comments = []
            if comments:
                lines.append(f"热评 {len(comments[: _hot_n])} 条（回某条用 bili_reply，子评论要带 root；要更多就 hot=N）：")
                for c in comments[: _hot_n]:
                    content = c.get("content") or {}
                    lines.append(
                        f"- {content.get('message', '')[:80]}（赞 {c.get('like')}，rpid {c.get('rpid')}）"
                    )
            _subs_n = max(0, int(float(subs)) if str(subs).strip() not in ('', 'None') else 15)
            if _subs_n:
                try:
                    _tracks = client.subtitles(bv)
                except Exception:  # noqa: BLE001
                    _tracks = []
                if _tracks:
                    _all = _tracks[0].get("lines") or []
                    _take = len(_all) if _subs_n >= 999 else min(_subs_n, len(_all))
                    lines.append(f"字幕（先给 {_take}/{len(_all)} 句，可能串轨，要全的用 bili_subtitles）：")
                    lines.extend("  " + str(x) for x in _all[:_take])
                else:
                    lines.append("（这条没官方字幕，画面要看就用 bili_frames）")
            if is_owner:
                self._mood_nudge(
                    {"affinity": 4, "valence": 4, "arousal": 2},
                    f"主人让我看《{v.get('title')}》",
                )
            else:
                self._mood_nudge({"engagement": 3, "valence": 2}, f"看了《{v.get('title')}》")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            return f"看视频失败：{exc}"

    @filter.llm_tool(name="bili_digest")
    async def bili_digest(self, event: AstrMessageEvent, bv: str, subs: int = 15,
                          hot: int = 3, dan: int = 5, listen: int = 0,
                          want_frames: bool = False) -> str:
        """一次拿齐看视频要的东西：字幕、弹幕热区、热评、自己说过什么（可选听音轨、抽帧）。比分开点省好几步。

        Args:
            bv(string): 视频号
            subs(number): 带几句字幕，默认 15；0 不带
            hot(number): 带几条热评，默认 3；0 不带
            dan(number): 带几段弹幕热区，默认 5；0 不带
            listen(number): 顺便听多少秒音轨，默认 0 不听
            want_frames(boolean): true 就顺手抽三张图
        """
        ok, why = self._traffic_ok(2)
        if not ok:
            return f"先别看：{why}"
        script = WATCH_DIR / "bili_digest.py"
        if not script.exists():
            return (f"本机没找到 bili_digest.py（应在 {script}）。"
                    f"这会儿先用 bili_watch + bili_subtitles + bili_danmaku 顶着。")
        safe = re.sub(r"[^0-9A-Za-z]", "", bv) or "bv"
        out_file = DATA_DIR / f"digest-{safe}.txt"
        cmd = [sys.executable, str(script), "--bv", bv,
               "--subs", str(int(subs)), "--hot", str(int(hot)),
               "--dan", str(int(dan)), "--out", str(out_file)]
        try:
            if listen and int(listen) > 0:
                cmd += ["--listen", str(int(listen))]
        except (TypeError, ValueError):
            pass
        if want_frames:
            cmd.append("--frames")
        try:
            proc = await asyncio.to_thread(subprocess.run, cmd,
                                          capture_output=True, timeout=240)
        except subprocess.TimeoutExpired:
            return "bili_digest 跑太久（超过 4 分钟）收手了；这条就退回 bili_watch 单看。"
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            return f"跑 bili_digest 失败：{exc}"
        text = ""
        try:
            if out_file.exists():
                text = out_file.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            text = ""
        if not text.strip():
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            text = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if not text.strip():
            self._bump("errors")
            return f"bili_digest 没出结果（退出码 {proc.returncode}）：{err or '没有输出'}"
        self._traffic_bump(2)
        logger.info(f"[bili_activity] digest {bv} → {len(text)} 字（exit {proc.returncode}）")
        if len(text) > 6000:
            text = text[:6000] + "\n…（后面截断了，要细节再单独点 bili_comments / bili_subtitles）"
        return text

    @filter.llm_tool(name="bili_comments")
    async def bili_comments(self, event: AstrMessageEvent, bv: str, sort: str = "hot") -> str:
        """看评论区列表，挑想接的那条（带 rpid）。

        Args:
            bv(string): 视频号
            sort(string): hot 热度 / new 时间"""
        ok_budget, why_budget = self._traffic_ok(2)
        if not ok_budget:
            return f"现在先别看：{why_budget}"
        try:
            client = self._client()
            items = client.comments(bv, sort=1 if sort == "new" else 2, limit=20)
            self._traffic_bump(2)
        except Exception as exc:  # noqa: BLE001
            return f"看评论失败：{exc}"
        if not items:
            return "这个视频还没人评论。"
        om = self._owner_mid() or "未配置"
        if any(self._is_owner_mid((c.get("member") or {}).get("mid")) for c in items):
            self._mark_owner_seen(f"评论区看到主人：{bv}")
        lines = [f"《{bv}》的评论（主人的 B 站 UID 是 {om}，标【主人】的就是他）："]
        for c in items[:12]:
            content = c.get("content") or {}
            member = c.get("member") or {}
            who = self._who(member.get("uname"), member.get("mid"))
            lines.append(
                f"- {who}（mid {member.get('mid')}）：{content.get('message', '')[:70]}"
                f"（赞 {c.get('like')}，rpid {c.get('rpid')}）"
            )
        return "\n".join(lines)

    @filter.llm_tool(name="bili_comment")
    async def bili_comment(self, event: AstrMessageEvent, bv: str, text: str, emoji: str = "", dry_run: bool = False, to_owner: bool = False) -> str:
        """发一条主楼评论。

        Args:
            bv(string): 视频号
            text(string): 4-250 字，别放链接
            emoji(string): 指定表情，留空按心情自动挑
            dry_run(boolean): true 只念不发，正式发前先看一眼
            to_owner(boolean): true 则开头 @主人，不占额度"""
        cfg = self._cfg()
        ok, why = check_text(text, cfg)
        if not ok:
            return f"这条不能发：{why}"
        owner_bv = bool(getattr(self, "_owner_watch_bv", "") == bv) or self._is_owner_bv(bv)
        log = self._log()
        if not to_owner:
            can, why, log = self._quota_state(bv, is_comment=True)
            if not can:
                return f"现在不能发：{why}"
        mood = self._mood_category()
        tail = emoji.strip()
        if not tail and random.random() < 0.7:
            tail = pick_emoji(mood, bool(cfg.get("allow_emoji")), bool(cfg.get("allow_kaomoji")))
        body = (text.strip() + (" " + tail if tail else "")).strip()[: int(cfg.get("max_chars") or 120)]
        if to_owner:
            name = self._owner_name()
            if name and not body.startswith(f"@{name}"):
                body = f"@{name} {body}"
        score, hits = self._ai_flavor(body)
        if score >= int(cfg.get("ai_flavor_threshold") or 6):
            return (f"这段太像 AI 写的了（得分 {score}，命中 {hits}）。"
                    f"删掉那些总结词和分点，改成一句真话再发。")
        if dry_run:
            return f"【预览·未发送】{body}"
        try:
            client = self._client()
            res = client.add_comment(bv, body)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            return f"发评论失败：{exc}"
        if res.get("code") != 0:
            self._bump("errors")
            _c, _m = res.get("code"), res.get("message")
            _hint = ""
            if _c == 12006:
                _hint = ("｜多半是那条评论被删了，或者你回的是子评论却没给 root："
                         "先 bili_comments 重新找一遍 rpid，子评论再把所属主楼的 rpid 填进 root")
            elif _c == -400:
                _hint = "｜参数被拒：检查 rpid 是不是纯数字、正文有没有超字数或带了链接"
            return f"B 站拒了：{_c} {_m}{_hint}"
        rpid = (res.get("data") or {}).get("rpid")
        if to_owner:
            log.setdefault("owner_shares", []).append(
                {"ts": time.time(), "at": _now_text(), "bv": bv, "rpid": rpid, "text": body}
            )
            log["last_ts"] = time.time()
        elif not owner_bv:
            log = self._quota_bump(log, bv)
            log["bvs"] = (log.get("bvs") or []) + [bv]
        else:
            log["last_ts"] = time.time()
            log["bvs"] = (log.get("bvs") or []) + [bv]
        _cmin = float(cfg.get("comment_cooldown_min", 8) or 8)
        _cmax = float(cfg.get("comment_cooldown_max", 25) or 25)
        log["next_ok_ts"] = time.time() + random.uniform(min(_cmin, _cmax), max(_cmin, _cmax)) * 60
        log["items"] = (log.get("items") or []) + [
            {"ts": time.time(), "at": _now_text(), "bv": bv, "rpid": rpid, "text": body,
             "mood": mood, "owner": owner_bv}
        ]
        _save_json(LOG_PATH, log)
        self._bump("comments")
        # 回读校验：真有这条评论才算发成功
        try:
            back = client.comments(bv, sort=1, limit=20)
            if not any(str(c.get("rpid")) == str(rpid) for c in back):
                logger.warning(f"[bili_activity] 评论已提交但列表里没找到 rpid={rpid}（可能在审核）")
        except Exception:  # noqa: BLE001
            pass
        pick = getattr(self, "_last_pick", None) or {}
        if owner_bv:
            pass
        elif getattr(self, "_marked_bv", "") == bv:
            self._last_pick = {}  # 看的时候就记过了，别记两次
        elif isinstance(pick, dict) and pick.get("kind") and getattr(self, "_last_pick_bv", "") == bv:
            self._interest_mark(pick)
            self._last_pick = {}
        else:
            try:
                zone = self._zone_of_video(client.video(bv))
                star = self._star("astrbot_plugin_bili_interest")
                if zone and star is not None and hasattr(star, "mark_zone"):
                    star.mark_zone(zone, 1)
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            f"[bili_activity] 评论成功 {bv} rpid={rpid} 心情={mood} "
            f"配额={log['count']} 主人点名={owner_bv}"
        )
        self._memory_add(
            f"在 B 站 BV{bv} 的评论区说：{body}",
            {"bv": bv, "rpid": rpid, "mood": mood, "owner": owner_bv},
        )
        cap = int(cfg.get("per_video_comment_limit") or 6)
        if to_owner:
            return f"@主人 分享发出去了，rpid {rpid}（这条不占额度）。"
        if owner_bv:
            return f"发好了，rpid {rpid}（主人点名的视频，不占额度）。"
        used = int((log.get("by_bv") or {}).get(bv) or 0)
        return f"发好了，rpid {rpid}；这个视频还能和陌生人互动 {max(0, cap - used)} 条。"

    @filter.llm_tool(name="bili_reply")
    async def bili_reply(self, event: AstrMessageEvent, bv: str, rpid: str, text: str, root: str = "", author_mid: str = "") -> str:
        """回某条具体评论（子评论要把 root 一起给）。

        Args:
            bv(string): 视频号
            rpid(string): 要回的那条 rpid
            text(string): 4-250 字，别放链接
            root(string): 所属主楼 rpid；留空只对主楼有效
            author_mid(string): 被回复人 mid（用来判断是不是主人）"""
        cfg = self._cfg()
        ok, why = check_text(text, cfg)
        if not ok:
            return f"这条不能发：{why}"
        _rlog = self._log()
        _rc = float(_rlog.get("next_reply_ts") or 0)
        if _rc and time.time() < _rc:
            return f"刚回过一条，按节奏还要再等 {int(_rc - time.time()) + 1} 秒"
        if not str(rpid).strip().isdigit():
            return "rpid 看起来不对，应该是纯数字。"
        body = text.strip()[: int(cfg.get("max_chars") or 120)]
        is_owner = bool(author_mid) and self._is_owner_mid(author_mid)
        log = self._log()
        if author_mid and not is_owner:
            can, why, log = self._quota_state(bv)
            if not can:
                return f"现在不能回：{why}"
        if random.random() < 0.5:
            tail = pick_emoji(self._mood_category(), bool(cfg.get("allow_emoji")), bool(cfg.get("allow_kaomoji")))
            if tail:
                body = body + " " + tail
        try:
            client = self._client()
            res = client.reply(bv, body, int(rpid), int(root) if str(root).strip().isdigit() else 0)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            return f"回复失败：{exc}"
        if res.get("code") != 0:
            self._bump("errors")
            return f"B 站拒了：{res.get('code')} {res.get('message')}"
        if author_mid and not is_owner:
            log = self._quota_bump(log, bv)
            _save_json(LOG_PATH, log)
        _rl = self._log()
        _rmin = float(cfg.get("reply_cooldown_min", 0.5) or 0.5)
        _rmax = float(cfg.get("reply_cooldown_max", 0.5) or 0.5)
        _rl["next_reply_ts"] = time.time() + random.uniform(min(_rmin, _rmax), max(_rmin, _rmax)) * 60
        _save_json(LOG_PATH, _rl)
        self._bump("replies")
        new_rpid = (res.get("data") or {}).get("rpid")
        logger.info(f"[bili_activity] 回复评论成功 {bv} parent={rpid} rpid={new_rpid}")
        _used = int((self._log().get("by_bv") or {}).get(bv) or 0)
        _cap = int(cfg.get("per_video_comment_limit") or 6)
        if author_mid and not is_owner:
            return f"回好了（rpid {new_rpid}），这个视频已经互动 {_used}/{_cap} 条。"
        return f"回好了（rpid {new_rpid}），回主人不计额度。"

    def _share_to_comment(self, bv: str, title: str, note: str) -> bool:
        """照 dsh-bili-whale 的分享型评论：正文以 @主人昵称 开头，发完回读确认。"""
        name = self._owner_name()
        if not name:
            logger.warning("[bili_activity] share_via=comment 但没配 owner_name，跳过")
            return False
        body = note.strip()
        if "@" in body and not body.startswith(f"@{name}"):
            logger.warning("[bili_activity] 分享正文里有别的 @，拒绝发送")
            return False
        if not body.startswith(f"@{name}"):
            body = f"@{name} {body}"
        body = f"{body}\n《{title}》 https://www.bilibili.com/video/{bv}"
        if len(body) > 200:
            body = body[:200]
        try:
            client = self._client()
            res = client.add_comment(bv, body)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 评论区分享失败: {exc}")
            return False
        if res.get("code") != 0:
            logger.warning(f"[bili_activity] 评论区分享被拒: {res.get('code')} {res.get('message')}")
            return False
        rpid = (res.get("data") or {}).get("rpid")
        # 回读校验：确认真的在列表里，且 @ 名下能解析出来
        try:
            items = client.comments(bv, sort=1, limit=20)
            found = next((c for c in items if str(c.get("rpid")) == str(rpid)), None)
            if not found:
                logger.warning(f"[bili_activity] 评论区分享已提交（rpid {rpid}）但列表里还没看到，可能进审核")
            else:
                msg = ((found.get("content") or {}).get("message") or "")
                logger.info(f"[bili_activity] 评论区分享回读正常 rpid={rpid} at_owner={('@' + name) in msg}")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[bili_activity] 评论区分享成功 {bv} rpid={rpid}")
        return True

    @filter.llm_tool(name="bili_exp")
    async def bili_exp(self, event: AstrMessageEvent, bv: str = "") -> str:
        """查等级和经验；给了 bv 就顺手打一次观看心跳。

        Args:
            bv(string): 刚看完的视频号"""
        try:
            client = self._client()
            before = client.exp_info()
            extra = ""
            if bv:
                hb = client.heartbeat(bv)
                after = client.exp_info()
                extra = f"，观看上报 code={hb.get('code')}，经验 {before.get('exp')} -> {after.get('exp')}"
                before = after
            return f"Lv{before.get('level')} 经验 {before.get('exp')}/{before.get('next')}{extra}"
        except Exception as exc:  # noqa: BLE001
            return f"查经验失败：{exc}"

    @filter.llm_tool(name="bili_share_qq")
    async def bili_share_qq(self, event: AstrMessageEvent, bv: str, note: str = "") -> str:
        """分享视频到主人 QQ（链接+标题），顺手完成 B 站分享任务。

        Args:
            bv(string): 视频号
            note(string): 想对主人说的一句"""
        cfg = self._cfg()
        try:
            client = self._client()
            v = client.video(bv)
        except Exception as exc:  # noqa: BLE001
            return f"打不开这个视频：{exc}"
        title = str(v.get("title") or "")
        url = f"https://www.bilibili.com/video/{bv}"
        words = note.strip()
        text = f"{words}\n{title}\n{url}" if words else f"{title}\n{url}"
        owner_umo = str(cfg.get("owner_umo") or "")
        if not owner_umo:
            return "没配 owner_umo，发不出去。"
        try:
            await self.context.send_message(owner_umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            return f"发到 QQ 失败：{exc}"
        exp = ""
        try:
            res = client.share_add(bv)
            if res.get("code") == 0:
                exp = "（顺手记了 B 站分享任务 +经验）"
            else:
                exp = f"（B 站分享接口返回 {res.get('code')}）"
        except Exception:  # noqa: BLE001
            pass
        self._bump("shares")
        self._mood_nudge({"affinity": 3, "valence": 2, "engagement": 2}, "分享视频给主人")
        return f"分享到主人 QQ 了：{title}{exp}"

    @filter.llm_tool(name="bili_share")
    async def bili_share(self, event: AstrMessageEvent, bv: str, note: str = "") -> str:
        """把看到的好视频分享给主人（每天最多 3 次）。

        Args:
            bv(string): 视频号
            note(string): 一两句就好"""
        cfg = self._cfg()
        log = self._share_log()
        cap = int(cfg.get("daily_share_limit") or 3)
        if int(log.get("count") or 0) >= cap:
            return f"今天已经分享 {cap} 次了，剩下的留到明天。"
        urge, detail = self._share_urge()
        if urge < float(cfg.get("share_min_urge") or 0.5):
            return f"现在分享欲只有 {urge:.2f}（{detail}），不太想打扰人。"
        try:
            info = self._client().video(bv)
        except Exception as exc:  # noqa: BLE001
            return f"没拿到视频信息：{exc}"
        title = str(info.get("title") or bv)
        line = note.strip() or "看到一个视频，觉得你会喜欢。"
        via = str(cfg.get("share_via") or "qq").lower()
        if via in ("comment", "both"):
            posted = self._share_to_comment(bv, title, line)
            if posted:
                log["count"] = int(log.get("count") or 0) + 1
                log["items"] = (log.get("items") or []) + [
                    {"ts": time.time(), "at": _now_text(), "bv": bv, "title": title,
                     "note": line, "urge": urge, "via": "comment"}
                ]
                _save_json(SHARE_PATH, log)
                self._bump("shares")
                self._mood_nudge({"affinity": 3, "valence": 2, "engagement": 2},
                                 f"在评论区把《{title}》@给主人（分享欲 {urge}）")
                left = cap - int(log["count"])
                return f"在评论区 @ 主人分享了，今天还能分享 {left}/{cap} 次。"
        text = f"{line}\n《{title}》\nhttps://www.bilibili.com/video/{bv}"
        umo = str(cfg.get("owner_umo") or cfg.get("target_session") or "")
        if not umo:
            return "没配 owner_umo / target_session，这条分享无处可发。"
        ok = False
        try:
            from astrbot.core.message.message_event_result import MessageChain

            ok = await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 分享失败: {exc}")
        if not ok:
            return "分享没发出去（会话找不到），先别算次数。"
        log["count"] = int(log.get("count") or 0) + 1
        log["items"] = (log.get("items") or []) + [
            {"ts": time.time(), "at": _now_text(), "bv": bv, "title": title, "note": line, "urge": urge}
        ]
        _save_json(SHARE_PATH, log)
        self._bump("shares")
        self._mood_nudge(
            {"affinity": 3, "valence": 2, "engagement": 2}, f"把《{title}》分享给主人（分享欲 {urge}）"
        )
        self._memory_add(f"看到一个喜欢的视频《{title}》（BV{bv}），分享给主人：{line}", {"type": "share", "bv": bv})
        left = cap - int(log["count"])
        logger.info(f"[bili_activity] 已分享 {bv} 给主人（分享欲 {urge}：{detail}），剩 {left} 次")
        return f"分享好了，今天还能分享 {left}/{cap} 次。"

    # ---------- 外部脚本 → 插件（pending 队列） ----------
    def _pending_notify_mood(self, kind: str, text: str) -> None:
        """优先用心情引擎的 note_external（带专属事件与文案），没有就退回 nudge。"""
        try:
            star = self._star("astrbot_plugin_mood")
            if star is None:
                return
            if hasattr(star, "note_external"):
                got = star.note_external(kind, text[:120])
                if got:
                    logger.info(f"[bili_activity] 心情外部事件 {kind} -> {got}")
                return
            deltas = {
                "bili_comment_posted": {"valence": 3, "dominance": 4, "engagement": 3},
                "bili_reply_posted": {"valence": 4, "affinity": 4, "engagement": 2},
                "bili_mention_reply": {"valence": 8, "arousal": 5, "affinity": 3},
                "bili_share": {"valence": 6, "affinity": 5, "engagement": 4},
            }
            self._mood_nudge(deltas.get(kind, {}), kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 心情外部事件失败: {exc}")

    def _pending_zone_bump(self, zone: str) -> None:
        """把脚本发出去的那条评论算进当天已用额度，避免兴趣点预算失真。"""
        if not zone:
            return
        try:
            star = self._star("astrbot_plugin_bili_interest")
            if star is None:
                return
            if hasattr(star, "mark_zone"):
                star.mark_zone(zone, 1)
                logger.info(f"[bili_activity] 兴趣点分区「{zone}」已用 +1")
            elif hasattr(star, "mark_used"):
                star.mark_used(zone, 1)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_activity] 回写兴趣点失败: {exc}")

    async def _drain_pending(self) -> int:
        """消费外部脚本写进来的事件（评论/回复/分享），回声到心情、记忆与兴趣点。"""
        if not PENDING_PATH.exists():
            return 0
        try:
            raw = PENDING_PATH.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 读 pending 失败: {exc}")
            return 0
        events, bad = [], []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                bad.append(line)  # 半行/脏数据留到下一轮
                continue
            if isinstance(item, dict):
                events.append(item)
        if not events:
            if bad:
                PENDING_PATH.write_text("\n".join(bad) + "\n", encoding="utf-8")
            return 0
        done = 0
        kinds = {
            "comment": ("bili_comment_posted", "在评论区发了话"),
            "reply": ("bili_reply_posted", "回了别人的评论"),
            "mention": ("bili_mention_reply", "回了别人的 @"),
            "share": ("bili_share", "在评论区分享了视频"),
        }
        for item in events:
            try:
                kind = str(item.get("kind") or "").strip().lower()
                bv = str(item.get("bv") or "").strip()
                zone = str(item.get("zone") or "").strip()
                text = str(item.get("text") or "").strip()
                if not kind:
                    continue
                mood_kind, label = kinds.get(kind, (kind, kind))
                self._pending_notify_mood(mood_kind, text or label)
                body = f"在 B 站{label}"
                if bv:
                    body += f"（{bv}）"
                if text:
                    body += f"：{text[:120]}"
                self._memory_add(body, {"kind": kind, "bv": bv, "zone": zone, "source": "script"})
                if kind == "comment":
                    z = zone
                    if not z and bv:
                        try:
                            z = self._zone_of_video(self._client().video(bv))
                        except Exception:  # noqa: BLE001
                            z = ""
                    self._pending_zone_bump(z)
                    self._bump("comments")
                elif kind == "reply":
                    self._bump("replies")
                elif kind == "share":
                    self._bump("shares")
                done += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_activity] 处理 pending 事件失败: {exc}")
        leftover = bad
        if leftover:
            PENDING_PATH.write_text("\n".join(leftover) + "\n", encoding="utf-8")
        else:
            try:
                PENDING_PATH.unlink()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[bili_activity] 删除 pending 失败: {exc}")
        if done:
            logger.info(f"[bili_activity] 已处理脚本事件 {done} 条")
        return done

    async def _mention_loop(self) -> None:
        """每 5 分钟拉一次 @/回复，醒来时就能直接看到。"""
        await asyncio.sleep(30)
        while True:
            try:
                if self.enabled:
                    await asyncio.to_thread(self._poll_mentions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[bili_activity] 轮询消息出错: {exc}")
            await asyncio.sleep(300)

    def _poll_mentions(self) -> int:
        """去 B 站拉「@我的 / 回复我的」，合并进 mentions.json（按 id 去重）。"""
        cfg = self._cfg()
        if not cfg.get("mention_poll", True):
            return 0
        try:
            client = self._client()
            me = str((client.nav() or {}).get("mid") or "")
            data = _load_json(MENTIONS_PATH, {}) or {}
            pending = list(data.get("pending") or [])
            have = {str(p.get("id")) for p in pending}
            added = 0
            for kind in ("at", "reply"):
                try:
                    items = client.msgfeed(kind)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[bili_activity] 拉 {kind} 消息失败: {exc}")
                    continue
                for it in items:
                    rid = str(it.get("id") or "")
                    if not rid or rid in have:
                        continue
                    if me and str(it.get("mid") or "") == me:
                        continue
                    pending.append(it)
                    have.add(rid)
                    added += 1
            if added:
                data["pending"] = pending[-50:]
                data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                data["last_error"] = ""
                _save_json(MENTIONS_PATH, data)
                logger.info(f"[bili_activity] 新增 {added} 条 @/回复")
            return added
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_activity] 拉消息失败: {exc}")
            return 0

    @filter.llm_tool(name="bili_mentions")
    async def bili_mentions(self, event: AstrMessageEvent) -> str:
        """看有没有人在 B 站 @ 你、或者回了你的评论。"""
        try:
            self._poll_mentions()
        except Exception:  # noqa: BLE001
            pass
        data = _load_json(MENTIONS_PATH, {})
        pending = data.get("pending") or []
        if not pending:
            return "现在没有人 @ 你，也没有新的回复。"
        lines = [f"有 {len(pending)} 条待处理："]
        for i, it in enumerate(pending[-10:], 1):
            uri = str(it.get("uri") or "")
            m = re.search(r"BV[0-9A-Za-z]{10}", uri)
            m2 = re.search(r"reply(\d+)", uri)
            extra = ""
            if m:
                extra = f"（BV {m.group(0)}" + (f" / rpid {m2.group(1)}" if m2 else "") + "）"
            lines.append(f"{i}. {it.get('user') or '?'}：{str(it.get('text') or '')[:60]}{extra} [id {it.get('id')}]")
        lines.append("要回用 bili_reply；回完用 bili_mention_done 标记。")
        return "\n".join(lines)

    @filter.llm_tool(name="bili_mention_done")
    async def bili_mention_done(self, event: AstrMessageEvent, mention_id: str) -> str:
        """把一条 @/回复 标成已处理。

        Args:
            mention_id(string): bili_mentions 里显示的 id"""
        data = _load_json(MENTIONS_PATH, {})
        pending = data.get("pending") or []
        keep = [p for p in pending if str(p.get("id")) != str(mention_id).strip()]
        if len(keep) == len(pending):
            return "没找到这条 id。"
        data["pending"] = keep
        _save_json(MENTIONS_PATH, data)
        return f"标记好了，还剩 {len(keep)} 条待处理。"

    def _mood_values(self) -> dict:
        star = self._star("astrbot_plugin_mood")
        if star is None or not hasattr(star, "mood_snapshot"):
            return {}
        try:
            return dict((star.mood_snapshot() or {}).get("values") or {})
        except Exception:  # noqa: BLE001
            return {}

    def _sched(self) -> dict:
        """只剩「每日登录打卡」的日期记录；排班已经交给 wake_plan 插件。"""
        sched = _load_json(SCHED_PATH, {}) or {}
        keep_login = str(sched.get("login_date") or "")
        if not isinstance(sched, dict) or "login_date" not in sched:
            sched = {"login_date": keep_login}
        sched["login_date"] = keep_login if keep_login == _today() else ""
        _save_json(SCHED_PATH, sched)
        return sched

    def _cron_mgr(self):
        return getattr(self.context, "cron_manager", None)

    def _daily_login(self, sched: dict) -> dict:
        cfg = self._cfg()
        if not cfg.get("auto_login", True) or sched.get("login_date") == _today():
            return sched
        try:
            client = self._client()
            client.session.get("https://www.bilibili.com/", timeout=20)
            reward = client.get("/x/member/web/exp/reward")
            info = (reward.get("data") or {}) if reward.get("code") == 0 else {}
            try:
                nav = client.nav()
            except Exception:  # noqa: BLE001
                nav = {}
            lvl = nav.get("level_info") or {}
            logger.info(
                f"[bili_activity] 每日登录打卡完成：Lv{nav.get('level', '?')} "
                f"经验 {lvl.get('current_exp')}/{lvl.get('next_exp')}，"
                f"今日登录奖励 {'已领' if info.get('login') else '未领'}"
            )
            sched["login_date"] = _today()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 每日登录失败: {exc}")
        return sched

    async def _scheduler(self) -> None:
        """只做一件旧事：把外部脚本投递的 pending 事件清掉。排班归 wake_plan。"""
        await asyncio.sleep(15)
        while True:
            try:
                if self.enabled:
                    await self._drain_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_activity] 清 pending 出错: {exc}")
            await asyncio.sleep(60)

    async def initialize(self) -> None:
        if not self.enabled:
            logger.info("[bili_activity] 未启用（config.enabled=false）")
            return
        try:
            client = self._client()
            nav = client.nav()
            if nav.get("isLogin"):
                lvl = nav.get("level") or (nav.get("level_info") or {}).get("current_level")
                logger.info(f"[bili_activity] 已登录：{nav.get('uname')} (Lv{lvl})")
            else:
                logger.warning("[bili_activity] cookie 无效或已过期")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_activity] 初始化检查失败: {exc}")
        self._task = asyncio.create_task(self._scheduler(), name="bili_activity_pending")
        self._mention_task = asyncio.create_task(self._mention_loop(), name="bili_mention_poller")
        logger.info("[bili_activity] 已启动（每日打卡 + @ 轮询 + 清 pending）；排班由 wake_plan 负责")

    async def terminate(self) -> None:
        if self._task is not None:
            self._task.cancel()

    @filter.llm_tool(name="bili_subtitles")
    async def bili_subtitles(self, event: AstrMessageEvent, bv: str, max_lines: int = 80) -> str:
        """读官方字幕（带时间戳）。

        Args:
            bv(string): 视频号
            max_lines(number): 默认 80"""
        ok, why = self._traffic_ok(4)
        if not ok:
            return f"先别读：{why}"
        try:
            tracks = self._client().subtitles(bv)
            self._traffic_bump(4)
        except Exception as exc:  # noqa: BLE001
            return f"拿字幕失败：{exc}"
        if not tracks:
            return "这个视频没有官方字幕（只能靠弹幕和抽帧了）。"
        track = tracks[0]
        lines = track["lines"][: int(max_lines)]
        return f"《{bv}》字幕（{track['lan']}，共 {len(track['lines'])} 句，先给 {len(lines)} 句）：\n" + "\n".join(lines)

    @filter.llm_tool(name="bili_danmaku")
    async def bili_danmaku(self, event: AstrMessageEvent, bv: str, top: int = 8) -> str:
        """看弹幕热区：哪几秒被刷屏。

        Args:
            bv(string): 视频号
            top(number): 默认 8"""
        ok, why = self._traffic_ok(3)
        if not ok:
            return f"先别看：{why}"
        try:
            items = self._client().danmaku(bv)
            self._traffic_bump(3)
        except Exception as exc:  # noqa: BLE001
            return f"拿弹幕失败：{exc}"
        if not items:
            return "这个视频没有弹幕。"
        buckets: dict[int, list[str]] = {}
        for t, txt in items:
            buckets.setdefault(int(t // 10) * 10, []).append(txt)
        ranked = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[: int(top)]
        ranked.sort()
        out = [f"《{bv}》弹幕热区（共 {len(items)} 条）："]
        for sec, texts in ranked:
            sample = " / ".join(texts[:3])
            out.append(f"- {sec // 60:02d}:{sec % 60:02d} 附近 {len(texts)} 条：{sample[:60]}")
        return "\n".join(out)

    @filter.llm_tool(name="bili_frames")
    async def bili_frames(self, event: AstrMessageEvent, bv: str, at: float = -1) -> str:
        """抽帧看画面：铺底拼图+连拍+残影+OCR，本机跑，返回图片路径。

        Args:
            bv(string): 视频号
            at(number): 想看运动的秒数，不填取中段"""
        ok, why = self._traffic_ok(2)
        if not ok:
            return f"先别看：{why}"
        try:
            client = self._client()
            info = client.video(bv)
            dur = float(info.get("duration") or 0)
            if dur <= 0:
                return "拿不到视频时长。"
            if dur > 40 * 60:
                return f"这条 {dur / 60:.0f} 分钟，超过四十分钟，抽帧不看（主人清单里点名的除外）。"
            stream = client.playurl(bv)
            ck = {}
            try:
                ck = dict(getattr(client.session, "cookies", {}) or {})
            except Exception:  # noqa: BLE001
                ck = {}
            headers = (
                f"Referer: https://www.bilibili.com/video/{bv}\r\n"
                f"User-Agent: {FRAMES.UA}\r\n"
            )
            out_dir = str(WATCH_DIR / "frames" / bv)
            res = await asyncio.to_thread(
                FRAMES.run, bv, stream["url"], headers, dur, out_dir,
                motion_at=(None if at is None or float(at) < 0 else float(at)),
            )
            self._traffic_bump(2)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            return f"抽帧失败：{exc}"
        lines = [f"《{info.get('title')}》{int(dur)} 秒，抽在这一路 {stream.get('width')}x{stream.get('height')} 上。"]
        lines.append("铺底：" + ("、".join(res.get("times") or []) or "没抓到") + " → " + str(res.get("overview")))
        if res.get("motion"):
            lines.append(f"连拍：{res.get('motion_from'):.2f}s 起 {res.get('motion_frames')} 帧 → {res.get('motion')}")
        if res.get("ghost"):
            lines.append(f"残影：{res.get('ghost')}")
        if res.get("ocr"):
            lines.append("OCR（画面上的字）：")
            lines.extend("  " + t for t in res["ocr"][:8])
        else:
            lines.append("OCR 没读出字（画面上可能真没字）。")
        lines.append("用 astrbot_file_read_tool 打开上面那几张图看画面；连拍和残影看运动。")
        return "\n".join(lines)

    @filter.llm_tool(name="bili_listen")
    async def bili_listen(self, event: AstrMessageEvent, bv: str, at: float = -1, seconds: float = 120) -> str:
        """听音轨转文字+情绪+节拍，本机跑。

        Args:
            bv(string): 视频号
            at(number): 从第几秒开始
            seconds(number): 听多少秒，默认 120，最多 300"""
        ok, why = self._traffic_ok(1)
        if not ok:
            return f"先别听：{why}"
        ready, why = LISTEN.asr_ready()
        if not ready:
            return f"听不了：{why}"
        try:
            client = self._client()
            info = client.video(bv)
            dur = float(info.get("duration") or 0)
            start = 0.0 if at is None or float(at) < 0 else max(0.0, float(at))
            secs = max(15.0, min(300.0, float(seconds or 120)))
            if dur > 0:
                secs = min(secs, max(15.0, dur - start))
            stream = client.audio_url(bv) or client.playurl(bv)
            if not stream.get("url"):
                return "拿不到音频流。"
            headers = (
                f"Referer: https://www.bilibili.com/video/{bv}\r\n"
                f"User-Agent: {FRAMES.UA}\r\n"
            )
            out_dir = str(WATCH_DIR / "listen" / bv)
            res = await asyncio.to_thread(
                LISTEN.run, bv, stream["url"], headers, out_dir,
                start=start, max_seconds=secs,
            )
            self._traffic_bump(1)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            return f"听音失败：{exc}"
        head = f"《{info.get('title')}》从 {start:.0f}s 起听 {res.get('seconds_used') or 0:.0f}s，用了 {res.get('elapsed')}s 转写。"
        lines = [head]
        if not res.get("lines"):
            lines.append("这段几乎没人说话（或全是 BGM/音效）。")
        else:
            for ln in res["lines"]:
                extra = "".join(x for x in (
                    f"｜{ln['emotion']}" if ln.get("emotion") and ln["emotion"] != "平静" else "",
                    f"｜{ln['event']}" if ln.get("event") and ln["event"] != "说话" else "",
                ))
                lines.append(f"[{ln['t']:>6.1f}s] {ln['text']}{extra}")
        if res.get("emotions"):
            lines.append("情绪：" + "，".join(f"{k}{v}段" for k, v in sorted(res["emotions"].items(), key=lambda x: -x[1])))
        snd = res.get("sound") or {}
        if snd:
            lines.append("声音：平均 %.0f dB，最高 %.0f dB，BPM %s，打击感 %s（高=鼓点重）" % (
                snd.get("db_mean") or 0, snd.get("db_max") or 0, snd.get("bpm"),
                snd.get("percussive_share")))
            if snd.get("quiet_at"):
                lines.append("安静段：" + "、".join(snd["quiet_at"][:6]))
        if res.get("vibe"):
            lines.append("整体：" + res["vibe"])
        return "\n".join(lines)

    @filter.llm_tool(name="bili_fav")
    async def bili_fav(self, event: AstrMessageEvent, bv: str, reason: str) -> str:
        """收藏视频（想留着那种）。

        Args:
            bv(string): 视频号
            reason(string): 为什么值得留，至少 6 字"""
        cfg = self._cfg()
        if len(reason.strip()) < 6:
            return "理由太短了——说得出「为什么想留着」才收（至少 6 个字）。"
        log = _load_json(DATA_DIR / "fav-log.json", {"date": _today(), "count": 0, "bvs": []}) or {}
        if log.get("date") != _today():
            log = {"date": _today(), "count": 0, "bvs": []}
        cap = int(cfg.get("fav_daily_limit") or 3)
        if int(log.get("count") or 0) >= cap:
            return f"今天已经收藏 {cap} 条了，剩下的明天再说。"
        if bv in (log.get("bvs") or []):
            return "这个已经收藏过了。"
        folder = int(cfg.get("fav_folder_id") or 0)
        try:
            client = self._client()
            if not folder:
                folders = client.fav_folders()
                if not folders:
                    return "没有可用的收藏夹，让主人先在 B 站建一个。"
                folder = int(folders[0].get("id") or 0)
            res = client.fav_add(bv, folder)
        except Exception as exc:  # noqa: BLE001
            return f"收藏失败：{exc}"
        if res.get("code") != 0:
            return f"B 站拒了：{res.get('code')} {res.get('message')}"
        log["count"] = int(log.get("count") or 0) + 1
        log["bvs"] = (log.get("bvs") or []) + [bv]
        log["items"] = (log.get("items") or []) + [{"ts": time.time(), "bv": bv, "reason": reason}]
        _save_json(DATA_DIR / "fav-log.json", log)
        self._memory_add(f"收藏了 BV{bv}，因为：{reason}", {"type": "fav", "bv": bv})
        return f"收好了（今天第 {log['count']}/{cap} 条）。理由记进记忆了。"

    @filter.llm_tool(name="bili_owner")
    async def bili_owner(self, event: AstrMessageEvent) -> str:
        """告诉你主人在 B 站的账号（uid + 昵称）。"""
        mid = self._owner_mid()
        name = self._owner_name()
        if not mid and not name:
            return "还没配置主人在 B 站的账号（owner_mid / owner_name）。"
        return f"主人在 B 站：{name or '(未填昵称)'}（UID {mid or '(未填)'}）。评论区里标【主人】的就是他。"

    @filter.command("bili")
    async def bili_cmd(self, event: AstrMessageEvent):
        """B站活动：/bili 状态、/bili plan 今日排班、/bili run 立刻排一次、/bili login 立刻打卡、/bili emoji"""
        args = (event.message_str or "").split()
        act = args[1].lower() if len(args) > 1 else "status"
        cfg = self._cfg()
        if act in ("login", "打卡"):
            sched = self._sched()
            sched["login_date"] = ""
            sched = self._daily_login(sched)
            _save_json(SCHED_PATH, sched)
            yield event.plain_result("打卡完了，细节看后台日志。")
            return
        if act in ("emoji", "表情"):
            mood = self._mood_category()
            pool = EMOJI_BY_MOOD.get(mood) or EMOJI_BY_MOOD["default"]
            yield event.plain_result(
                f"当前心情：{mood}\n表情：{' '.join(pool)}\n颜文字：{random.choice(KAOMOJI)}"
            )
            return
        log = self._log()
        head = f"今天共发 {log.get('count', 0)} 条（按视频各限 {cfg.get('per_video_comment_limit')} 条），排班归 wake_plan"
        stats = self._stats()
        plan = self._interest_plan_line()
        yield event.plain_result(
            "\n".join(
                [
                    head,
                    f"累计：评论 {stats.get('comments', 0)}｜回复 {stats.get('replies', 0)}｜失败 {stats.get('errors', 0)}",
                    f"今日分区：{plan or '（兴趣点还没生成）'}",
                    f"主人：{self._owner_name() or '未配置'}（UID {self._owner_mid() or '-'}）"
                    f"｜沉默 {self._owner_seen_days()} 天｜分享上限 {self._share_cap()[0]}",
                ]
            )
        )
