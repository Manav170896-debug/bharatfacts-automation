"""
BharatFacts animated renderer.

Turns a list of scene images + word timings + a voiceover into a 1080x1920 Short with:
  - camera motion on every scene (zoom in/out, pans) with eased movement
  - punch-in + crossfade transitions between scenes
  - word-by-word captions (active word highlighted, pop-in)
  - hook banner for the first seconds, progress bar, channel tag
Pure Python (Pillow) + FFmpeg, so it runs free on GitHub Actions.
"""
from __future__ import annotations

import functools
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FPS = 30
XFADE = 0.25        # seconds of crossfade between scenes
PUNCH = 0.35        # seconds of punch-in at scene start

YELLOW = (255, 214, 10)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (230, 57, 70)

FONT_CANDIDATES = {
    "latin": [
        str(Path(__file__).parent / "fonts" / "Poppins-Bold.woff"),
        "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Black.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "devanagari": [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Black.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        str(Path(__file__).parent / "fonts" / "NotoSansDevanagari-Bold.woff"),
    ],
}


@functools.lru_cache(maxsize=256)
def _font(script: str, size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES[script] + FONT_CANDIDATES["latin"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size, layout_engine=ImageFont.Layout.RAQM)
    return ImageFont.load_default(size)


def _script_of(text: str) -> str:
    return "devanagari" if any("\u0900" <= c <= "\u097f" for c in text) else "latin"


_RUNS = re.compile(r"[\u0900-\u097F\u200c\u200d]+|[^\u0900-\u097F\u200c\u200d]+")


def _runs(text: str, size: int):
    """Split text into (segment, font) runs so Latin punctuation never hits the Devanagari font."""
    return [(seg, _font(_script_of(seg), size)) for seg in _RUNS.findall(text)]


def _mixed_len(d: ImageDraw.ImageDraw, text: str, size: int) -> float:
    return sum(d.textlength(seg, font=f) for seg, f in _runs(text, size))


def _mixed_text(d: ImageDraw.ImageDraw, xy, text: str, size: int, **kw):
    x, y = xy
    for seg, f in _runs(text, size):
        d.text((x, y), seg, font=f, anchor="ls", **kw)
        x += d.textlength(seg, font=f)


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 0.5 - 0.5 * math.cos(math.pi * t)


def _back_out(t: float) -> float:
    """Overshoot easing for pop-ins."""
    t = min(max(t, 0.0), 1.0)
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


@dataclass
class Scene:
    image: Path
    start: float
    end: float
    motion: str = "zoom_in"   # zoom_in | zoom_out | pan_left | pan_right


@dataclass
class Word:
    text: str
    start: float
    end: float


MOTIONS = ["zoom_in", "pan_right", "zoom_out", "pan_left"]


class _SceneCam:
    """Computes the source crop box for each frame (Ken Burns style)."""

    def __init__(self, img: Image.Image, motion: str):
        self.img = img.convert("RGB")
        self.sw, self.sh = self.img.size
        self.motion = motion
        # largest 9:16 box that fits inside the source
        self.bh = min(self.sh, self.sw * 16 / 9)
        self.bw = self.bh * 9 / 16

    def frame(self, p: float, local_t: float) -> Image.Image:
        e = _ease(p)
        zoom, cx, cy = 1.0, 0.5, 0.5
        m = self.motion
        if m == "zoom_in":
            zoom = 1.04 + 0.2 * e
        elif m == "zoom_out":
            zoom = 1.24 - 0.2 * e
        elif m == "pan_right":
            zoom, cx = 1.08, 0.25 + 0.5 * e
        elif m == "pan_left":
            zoom, cx = 1.08, 0.75 - 0.5 * e
        # punch-in at scene start (slight overshoot then settle)
        if local_t < PUNCH:
            zoom *= 1.0 + 0.10 * (1 - _back_out(local_t / PUNCH))
        zoom = max(zoom, 1.0)
        # gentle float so nothing is ever fully static
        cy += 0.012 * math.sin(local_t * 1.6)
        bw, bh = self.bw / zoom, self.bh / zoom
        x0 = (self.sw - bw) * cx
        y0 = min(max((self.sh - bh) * cy, 0), self.sh - bh)
        x0 = min(max(x0, 0), self.sw - bw)
        return self.img.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + bw, y0 + bh))


def _chunk_words(words: list[Word], max_words: int = 3) -> list[list[Word]]:
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or w.text.endswith((".", ",", "!", "?", "\u0964")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _draw_caption(frame: Image.Image, chunk: list[Word], t: float):
    script = _script_of(" ".join(w.text for w in chunk))
    base_size = 92 if script == "latin" else 86
    pop = _back_out((t - chunk[0].start) / 0.14)
    size = max(int(base_size * (0.75 + 0.25 * pop)), 10)
    big = int(size * 1.12)
    texts = [w.text.upper() if script == "latin" else w.text for w in chunk]
    active = next((i for i, w in enumerate(chunk) if w.start <= t < w.end + 0.05), -1)

    d = ImageDraw.Draw(frame)
    space = d.textlength(" ", font=_font("latin", size))
    sizes = [big if i == active else size for i in range(len(texts))]
    widths = [_mixed_len(d, tx, sz) for tx, sz in zip(texts, sizes)]
    # wrap onto two lines if needed
    lines, line, lw = [], [], 0.0
    for i, wdt in enumerate(widths):
        if line and lw + space + wdt > W - 120:
            lines.append((line, lw))
            line, lw = [], 0.0
        lw = lw + (space if line else 0) + wdt
        line.append(i)
    lines.append((line, lw))

    y = 1240
    for idxs, lw in lines:
        x = (W - lw) / 2
        for i in idxs:
            color = YELLOW if i == active else WHITE
            _mixed_text(d, (x, y), texts[i], sizes[i], fill=color,
                        stroke_width=max(size // 11, 4), stroke_fill=BLACK)
            x += widths[i] + space
        y += int(size * 1.3)


def _draw_hook(frame: Image.Image, hook: str, t: float, dur: float = 2.8):
    if not hook or t > dur:
        return
    script = _script_of(hook)
    p = _back_out(t / 0.3)
    fade = 1.0 if t < dur - 0.3 else max(0.0, (dur - t) / 0.3)
    size = max(int(70 * (0.6 + 0.4 * p)), 10)
    text = hook.upper() if script == "latin" else hook
    d = ImageDraw.Draw(frame, "RGBA")
    tw = _mixed_len(d, text, size)
    if tw > W - 140:  # shrink long hooks
        size = int(size * (W - 140) / tw)
        tw = _mixed_len(d, text, size)
    pad, y = 34, 300
    box = (W / 2 - tw / 2 - pad, y - size - 18, W / 2 + tw / 2 + pad, y + 30)
    d.rounded_rectangle(box, radius=26, fill=RED + (int(235 * fade),))
    _mixed_text(d, (W / 2 - tw / 2, y), text, size, fill=WHITE + (int(255 * fade),))


def _overlay(channel: str) -> Image.Image:
    """Static overlay: bottom gradient for caption legibility + channel tag."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    grad = Image.new("L", (1, H))
    for y in range(H):
        a = 0 if y < H * 0.52 else int(170 * ((y - H * 0.52) / (H * 0.48)) ** 1.3)
        grad.putpixel((0, y), a)
    ov.paste((0, 0, 0, 255), (0, 0, W, H), grad.resize((W, H)))
    d = ImageDraw.Draw(ov)
    f = _font("latin", 38)
    d.text((W / 2, H - 150), channel, font=f, fill=(255, 255, 255, 210),
           stroke_width=3, stroke_fill=(0, 0, 0, 200), anchor="ms")
    return ov


def render(scenes: list[Scene], words: list[Word], audio: Path, out: Path,
           hook: str = "", channel: str = "@BharatFacts") -> Path:
    total = max(scenes[-1].end, words[-1].end if words else 0) + 0.4
    cams = [_SceneCam(Image.open(s.image), s.motion or MOTIONS[i % 4]) for i, s in enumerate(scenes)]
    chunks = _chunk_words(words)
    overlay = _overlay(channel)

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-af", "apad", "-t", f"{total:.2f}",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    n = int(total * FPS)
    ci = 0
    for f in range(n):
        t = f / FPS
        si = max(i for i, s in enumerate(scenes) if s.start <= t or i == 0)
        s = scenes[si]
        dur = max(s.end - s.start, 0.1)
        img = cams[si].frame((t - s.start) / dur, t - s.start)
        # crossfade from previous scene
        if si > 0 and t - s.start < XFADE:
            prev = scenes[si - 1]
            pd = max(prev.end - prev.start, 0.1)
            pimg = cams[si - 1].frame(min((t - prev.start) / pd, 1.0), t - prev.start)
            img = Image.blend(pimg, img, (t - s.start) / XFADE)
        img = img.convert("RGBA")
        img.alpha_composite(overlay)
        img = img.convert("RGB")

        while ci < len(chunks) - 1 and t >= chunks[ci + 1][0].start:
            ci += 1
        if chunks and chunks[ci][0].start <= t <= chunks[ci][-1].end + 0.35:
            _draw_caption(img, chunks[ci], t)
        _draw_hook(img, hook, t)
        # progress bar
        ImageDraw.Draw(img).rectangle((0, 0, int(W * t / total), 12), fill=YELLOW)
        proc.stdin.write(img.tobytes())

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg render failed")
    return out
