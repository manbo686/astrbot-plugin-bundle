"""抽帧、拼图、残影、OCR——B 站视频「看一眼画面」用的小工具集。

设计要点：
- 只依赖 ffmpeg + PIL（本机已有），OCR 用 rapidocr_onnxruntime，都在本地跑，不吃token
- 长边固定：横屏 640x360、竖屏 360x640，一格像素量一样，整张图 token 可控
- 连拍用密集帧（默认 0.1 秒一张）看运动；残影把几帧叠一起看轨迹
"""
from __future__ import annotations

import os
import sys
import glob
import re
import subprocess
import time
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
FONT_CANDIDATES = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
                   r"C:\Windows\Fonts\Deng.ttf")


def ffmpeg_path() -> str:
    env = os.environ.get("FFMPEG_PATH", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),  # 跟着 AstrBot 的 venv
        os.path.join(here, "ffmpeg.exe"),  # 或放在插件目录里
    ):
        if os.path.exists(cand):
            return cand
    return "ffmpeg"


def probe_duration(url: str, headers: str, timeout: int = 60) -> float:
    """读容器头拿真实时长。B 站偶尔元数据比流长，按元数据取点会全部落空。"""
    try:
        r = subprocess.run([ffmpeg_path(), "-hide_banner", "-headers", headers, "-i", url,
                            "-f", "null", "-t", "0", "-"],
                           capture_output=True, text=True, timeout=timeout)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or "")
        if m:
            h, mi, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + sec
    except Exception:
        pass
    return 0.0


def _font(size: int = 22):
    for c in FONT_CANDIDATES:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    return ImageFont.load_default()


def hhmmss(t: float) -> str:
    t = max(0, int(t))
    return "%d:%02d" % (t // 60, t % 60)


def _clear(outdir: str, prefix: str) -> None:
    for f in glob.glob(os.path.join(outdir, prefix + "_*.jpg")):
        try:
            os.remove(f)
        except OSError:
            pass


def grab(url: str, headers: str, t: float, *, outdir: str, prefix: str = "f",
         n: int = 1, fps: float | None = None, size: int = 640) -> list[str]:
    """从远程流里抠帧。size 是长边：横屏 640x360，竖屏 360x640。"""
    os.makedirs(outdir, exist_ok=True)
    _clear(outdir, prefix)
    pattern = os.path.join(outdir, prefix + "_%02d.jpg")
    scale = "scale='if(gt(iw,ih),%d,-2)':'if(gt(iw,ih),-2,%d)'" % (size, size)
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-headers", headers,
           "-ss", "%.2f" % max(0.0, t), "-i", url]
    if fps:
        cmd += ["-t", "%.2f" % (n / float(fps)), "-vf", "fps=%s,%s" % (fps, scale)]
    else:
        cmd += ["-frames:v", str(n), "-vf", scale]
    cmd.append(pattern)
    subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return sorted(glob.glob(os.path.join(outdir, prefix + "_*.jpg")))


def _label(im: Image.Image, text: str, size: int = 22) -> Image.Image:
    d = ImageDraw.Draw(im)
    f = _font(size)
    box = d.textbbox((0, 0), text, font=f)
    d.rectangle([0, 0, box[2] + 12, box[3] + 8], fill=(0, 0, 0))
    d.text((6, 3), text, font=f, fill=(255, 255, 255))
    return im


def _cell(im: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.pad(im.convert("RGB"), (w, h), color=(0, 0, 0), method=Image.LANCZOS)


def compose(paths: Iterable[str], labels: Iterable[str], out_path: str,
            cols: int = 2, cell_w: int = 360, cell_h: int = 640) -> str | None:
    ims = []
    for p, lab in zip(paths, labels):
        try:
            ims.append(_label(_cell(Image.open(p), cell_w, cell_h), lab))
        except Exception:
            continue
    if not ims:
        return None
    rows = (len(ims) + cols - 1) // cols
    grid = Image.new("RGB", (cell_w * cols, cell_h * rows), (0, 0, 0))
    for i, im in enumerate(ims):
        grid.paste(im, ((i % cols) * cell_w, (i // cols) * cell_h))
    grid.save(out_path, quality=88)
    return out_path


def ghost(paths: list[str], out_path: str, cell_w: int = 270, cell_h: int = 480) -> str | None:
    """残影：几帧按比例叠在一起，轨迹会留成一条带。"""
    ims = []
    for p in paths[:8]:
        try:
            ims.append(_cell(Image.open(p), cell_w, cell_h))
        except Exception:
            continue
    if not ims:
        return None
    acc = ims[0]
    for im in ims[1:]:
        acc = Image.blend(acc, im, 0.45)
    acc.save(out_path, quality=88)
    return out_path


def _signature(path: str) -> list[int]:
    im = Image.open(path).convert("L").resize((32, 32))
    return list(im.getdata())


def _too_similar(a: list[int], b: list[int], thresh: int = 6) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) < thresh


def ocr(paths: Iterable[str], labels: Iterable[str] | None = None) -> list[str]:
    """本地 OCR，把画面上的字抠成文本（不吃token）。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return []
    try:
        engine = RapidOCR()
    except Exception:
        return []
    out: list[str] = []
    labels = list(labels or [])
    for i, p in enumerate(paths):
        try:
            res, _ = engine(p)
        except Exception:
            continue
        pieces = [str(r[1]).strip() for r in (res or []) if str(r[1]).strip()]
        if not pieces:
            continue
        tag = labels[i] if i < len(labels) else os.path.basename(p)
        out.append("[%s] %s" % (tag, " / ".join(pieces[:6])))
    return out


def run(bv: str, url: str, headers: str, duration: float, out_dir: str, *,
        motion_at: float | None = None, tiles: int = 4, tile_size: int = 640,
        motion_frames: int = 8, motion_fps: float = 10.0, motion_size: int = 480,
        do_ocr: bool = True) -> dict:
    """跑一遍：铺底拼图 + 连拍 + 残影 + OCR。返回各文件路径和时间点。"""
    os.makedirs(out_dir, exist_ok=True)
    duration = max(1.0, float(duration or 1))
    probed = probe_duration(url, headers)
    if probed > 0.5:
        # 元数据比流长时以流为准；流比元数据长就用元数据
        duration = min(duration, probed) if probed < duration * 0.6 else duration
    leftovers = [f for f in os.listdir(out_dir) if f.endswith((".jpg", ".png"))]
    for f in leftovers:
        try:
            os.remove(os.path.join(out_dir, f))
        except OSError:
            pass

    # 铺底：均匀取点，太像的往后挪一点，避免四张一个样
    ratios = [0.12] if tiles == 1 else [(i + 0.5) / tiles for i in range(tiles)]
    times: list[float] = []
    kept: list[str] = []
    sigs: list[list[int]] = []
    for r in ratios:
        t = duration * r
        got = None
        for attempt in range(3):
            files = grab(url, headers, t, outdir=out_dir, prefix="g%d" % len(kept), size=tile_size)
            if not files:
                t = max(0.0, t - duration * 0.12)      # 越界了就往回退，别把这一格丢了
                continue
            got = (files[0], t)
            sig = _signature(files[0])
            if kept and _too_similar(sigs[-1], sig) and attempt < 2:
                t = min(duration * 0.95, t + duration * 0.08)   # 跟上一张太像就往后挪
                continue
            kept.append(files[0]); sigs.append(sig); times.append(t)
            got = None
            break
        if got and kept:            # 三次都没拿到更好的，就用这张
            kept[-1] = got[0]; times[-1] = got[1]; sigs[-1] = _signature(got[0])

    # 统一方形格子：横竖屏混排也不会一张高一张矮
    cell_w = cell_h = int(tile_size * 0.75)
    labels = [hhmmss(t) for t in times]
    overview = compose(kept, labels, os.path.join(out_dir, "overview.jpg"),
                       cols=2 if len(kept) <= 4 else 3, cell_w=cell_w, cell_h=cell_h)

    # 连拍：默认落在中段，也可以指定秒数
    base = float(motion_at) if motion_at is not None else duration * 0.45
    base = max(0.0, min(max(0.0, duration - 1.2), base))
    m_files = grab(url, headers, base, outdir=out_dir, prefix="m",
                   n=motion_frames, fps=motion_fps, size=motion_size)
    m_labels = ["%.2fs" % (base + i / motion_fps) for i in range(len(m_files))]
    motion = None
    gh = None
    if m_files:
        m_cell = int(motion_size * 0.72)
        motion = compose(m_files, m_labels, os.path.join(out_dir, "motion.jpg"),
                         cols=4, cell_w=m_cell, cell_h=m_cell)
        g_cell = int(motion_size * 0.95)
        gh = ghost(m_files, os.path.join(out_dir, "ghost.jpg"), cell_w=g_cell, cell_h=g_cell)

    texts = ocr(kept, labels) if do_ocr else []
    return {
        "overview": overview, "motion": motion, "ghost": gh,
        "times": [hhmmss(t) for t in times], "raw_times": times,
        "motion_from": base, "motion_frames": len(m_files), "ocr": texts,
        "dir": out_dir, "ffmpeg": ffmpeg_path(),
    }
