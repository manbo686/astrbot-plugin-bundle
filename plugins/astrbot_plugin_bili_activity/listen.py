"""听声音：把 B 站视频的音轨拉到本地，转成字 + 情绪标签 + 音量节拍。

设计要点：
- 全部本机跑：ffmpeg 抽音轨 → 本地 ASR（faster-whisper / SenseVoice）→ 零token
- 只吐一小段文字回来，token 可控
- 拿不到模型时降级成「只有声音特征」，不整段失败
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
import time

def ffmpeg_path() -> str:
    env = os.environ.get("FFMPEG_PATH", "").strip()
    if env:
        return env
    cand = os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe")
    if os.path.exists(cand):
        return cand
    return "ffmpeg"


EMO_CN = {
    "HAPPY": "开心", "SAD": "难过", "ANGRY": "生气", "NEUTRAL": "平静",
    "FEARFUL": "害怕", "DISGUSTED": "嫌弃", "SURPRISED": "惊讶",
}
EVT_CN = {
    "Speech": "说话", "BGM": "背景音乐", "Applause": "掌声", "Laughter": "笑声",
    "Crying": "哭声", "Cough": "咳嗽", "Sneeze": "喷嚏", "Breath": "呼吸声",
}

ASR_VENV_PY = os.environ.get("ASR_PYTHON", "").strip()
AUDIO_PROBE = os.environ.get("ASR_AUDIO_PROBE", "").strip()
SENSEVOICE_DIR = os.environ.get("ASR_SENSEVOICE_DIR", "").strip()


def asr_ready() -> tuple[bool, str]:
    if not ASR_VENV_PY or not os.path.exists(ASR_VENV_PY):
        return False, "没配本地转写环境（环境变量 ASR_PYTHON），只能给声音特征"
    if not os.path.exists(AUDIO_PROBE):
        return False, "没找到 audio_probe.py"
    if not os.path.exists(os.path.join(SENSEVOICE_DIR, "model.int8.onnx")):
        return False, "没找到 SenseVoice 模型"
    return True, ""


def fetch_audio(url: str, headers: str, out_wav: str, *, max_seconds: float = 0,
                start: float = 0) -> str:
    """把远程流的音轨抠成 16k 单声道 wav。start 秒开始，取 max_seconds 秒。"""
    os.makedirs(os.path.dirname(out_wav), exist_ok=True)
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error"]
    if headers and str(url).startswith(("http://", "https://")):
        cmd += ["-headers", headers]     # 本地文件没有这个选项，加上会直接报错
    if start and start > 0:
        cmd += ["-ss", "%.1f" % float(start)]
    cmd += ["-i", url, "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", "%.1f" % float(max_seconds)]
    cmd.append(out_wav)
    subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return out_wav if os.path.exists(out_wav) and os.path.getsize(out_wav) > 1024 else ""


def probe(wav: str, max_seconds: float = 0, timeout: int = 900) -> dict:
    """调本地脚本，拿文字、情绪、声音特征。"""
    cmd = [ASR_VENV_PY, AUDIO_PROBE, wav, "--json"]
    if max_seconds and max_seconds > 0:
        cmd += ["--max-seconds", "%.0f" % float(max_seconds)]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["OMP_NUM_THREADS"] = "4"
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore",
                       timeout=timeout, env=env)
    txt = (r.stdout or "").strip()
    if not txt.startswith("{"):
        raise RuntimeError("听音脚本没返回 JSON：" + ((r.stderr or txt)[-200:] or "空输出"))
    return json.loads(txt)


def summarize(data: dict, *, keep_lines: int = 14) -> dict:
    """把一堆段压成一句句人话，别把几十段全塞回去。"""
    speech = data.get("speech") or []
    said = [s for s in speech if (s.get("text") or "").strip()]
    emo: dict[str, int] = {}
    evt: dict[str, int] = {}
    for s in said:
        e = EMO_CN.get((s.get("emotion") or "").upper(), "")
        if e:
            emo[e] = emo.get(e, 0) + 1
        v = EVT_CN.get(s.get("event") or "", "")
        if v and v != "说话":
            evt[v] = evt.get(v, 0) + 1
    # 话多的段优先，剩下的按时间排
    picked = said if len(said) <= keep_lines else sorted(
        said, key=lambda s: -len(s.get("text") or ""))[:keep_lines]
    picked = sorted(picked, key=lambda s: s.get("start", 0))
    return {
        "duration": (data.get("sound") or {}).get("duration_s"),
        "lines": [{"t": s.get("start"), "text": (s.get("text") or "").strip()[:120],
                   "emotion": EMO_CN.get((s.get("emotion") or "").upper(), ""),
                   "event": EVT_CN.get(s.get("event") or "", "")}
                  for s in picked],
        "segments": len(said),
        "emotions": emo,
        "events": evt,
        "sound": data.get("sound") or {},
        "vibe": data.get("vibe") or "",
    }


def run(bv: str, url: str, headers: str, out_dir: str, *, max_seconds: float = 180,
        start: float = 0) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    wav = os.path.join(out_dir, "audio.wav")
    t0 = time.time()
    got = fetch_audio(url, headers, wav, max_seconds=max_seconds, start=start)
    if not got:
        raise RuntimeError("音轨没抽出来（这条视频可能没有音轨）")
    data = probe(got, max_seconds=max_seconds)
    if start and start > 0:      # 时间戳换成视频里的绝对秒数
        for seg in (data.get("speech") or []):
            seg["start"] = round(float(seg.get("start") or 0) + float(start), 1)
            seg["end"] = round(float(seg.get("end") or 0) + float(start), 1)
    out = summarize(data)
    out["wav"] = got
    out["seconds_used"] = max_seconds
    out["start"] = start
    out["elapsed"] = round(time.time() - t0, 1)
    try:
        os.remove(got)          # wav 用完就清，几百兆不囤
    except OSError:
        pass
    return out
