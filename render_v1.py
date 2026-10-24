"""Render a "Ranking ..." Short from a project spec.

  python render.py projects/<slug>/project.json        -> projects/<slug>/out/final.mp4

project.json:
{
  "title": [["Ranking ", "white"], ["Craziest", "yellow"], ["\\n", ""], ["Trick Shots", "blue"], [" 🎯", ""]],
  "subtitle": "Last one is insane 💀",          # optional, small line under the title
  "items": [                                       # in PLAY order (worst rank first, #1 last)
    {"rank": 6, "src": "VIDEO_ID", "segments": [[3.5, 7.5]], "speed": 1.0, "label": "dog goalie 🐕"},
    ...
  ]
}
Clips are read from projects/<slug>/raw/<src>.mp4
"""
import json, subprocess, sys, unicodedata
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H, HEADER, FPS = 1080, 1920, 380, 30
FONT_XB = str(ROOT / "assets/fonts/Poppins-ExtraBold.ttf")
FONT_B = str(ROOT / "assets/fonts/Poppins-Bold.ttf")
EMOJI = "C:/Windows/Fonts/seguiemj.ttf"
COLORS = {"white": (255, 255, 255), "yellow": (255, 230, 0), "blue": (40, 160, 255), "red": (255, 60, 60),
          "green": (60, 230, 90), "orange": (255, 150, 30), "pink": (255, 90, 200)}
RANK_COLORS = {1: (255, 214, 0), 2: (200, 210, 225), 3: (230, 140, 60)}
LIST_TOP, LIST_STEP = HEADER + 90, 150          # y of rank 1 and spacing


def is_emoji(ch):
    cp = ord(ch)
    return cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0x2190 <= cp <= 0x21FF or 0x2B00 <= cp <= 0x2BFF


def runs(text):
    out = []
    for ch in text:
        if ch in "\ufe0f\u200d":
            continue
        e = is_emoji(ch)
        if out and out[-1][1] == e:
            out[-1][0] += ch
        else:
            out.append([ch, e])
    return out


def text_w(text, size, font_path):
    f, fe = ImageFont.truetype(font_path, size), ImageFont.truetype(EMOJI, int(size * 0.9))
    return sum((fe if e else f).getlength(t) for t, e in runs(text))


def draw_rich(d, x, y, text, size, fill, font_path=FONT_XB, stroke=0):
    """Draw text with emoji fallback; (x, y) = left, top-of-cap. Returns end x."""
    f, fe = ImageFont.truetype(font_path, size), ImageFont.truetype(EMOJI, int(size * 0.9))
    for t, e in runs(text):
        if e:
            d.text((x, y + size * 0.12), t, font=fe, embedded_color=True)
            x += fe.getlength(t)
        else:
            d.text((x, y), t, font=f, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0))
            x += f.getlength(t)
    return x


def header_layer(spec):
    im = Image.new("RGBA", (W, HEADER), (0, 0, 0, 255))
    d = ImageDraw.Draw(im)
    lines, cur = [], []
    for txt, col in spec["title"]:
        if txt == "\n":
            lines.append(cur); cur = []
        else:
            cur.append((txt, COLORS.get(col, (255, 255, 255))))
    lines.append(cur)
    size = 82
    while max(sum(text_w(t, size, FONT_XB) for t, _ in ln) for ln in lines) > W - 70:
        size -= 2
    sub = spec.get("subtitle")
    block = len(lines) * size * 1.18 + (size * 0.55 if sub else 0)
    y = (HEADER - block) / 2 - size * 0.1
    for ln in lines:
        lw = sum(text_w(t, size, FONT_XB) for t, _ in ln)
        x = (W - lw) / 2
        for t, c in ln:
            x = draw_rich(d, x, y, t, size, c)
        y += size * 1.18
    if sub:
        s2 = int(size * 0.45)
        draw_rich(d, (W - text_w(sub, s2, FONT_B)) / 2, y + 4, sub, s2, (255, 255, 255), FONT_B)
    return im


def list_layer(spec, active_rank):
    """Overlay for the clip area: rank numbers + labels revealed up to the active item."""
    n = max(i["rank"] for i in spec["items"])
    labels = {i["rank"]: i["label"] for i in spec["items"]}
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for r in range(1, n + 1):
        y = LIST_TOP + (r - 1) * LIST_STEP
        num_col = RANK_COLORS.get(r, (255, 255, 255))
        nsize = 84 if r == active_rank else 70
        x = draw_rich(d, 34, y - (nsize - 70) / 2, f"{r}.", nsize, num_col, FONT_XB, stroke=7)
        if r >= active_rank:
            lsize = 54 if r == active_rank else 44
            col = (255, 255, 255)
            draw_rich(d, x + 16, y + (70 - lsize) * 0.55, labels[r], lsize, col, FONT_XB, stroke=6)
    return im


def probe_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def render(spec_path):
    spec_path = Path(spec_path).resolve()
    proj = spec_path.parent
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out, tmp = proj / "out", proj / "out/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    head = tmp / "header.png"
    header_layer(spec).save(head)
    parts = []
    for k, it in enumerate(spec["items"]):
        src = proj / "raw" / f"{it['src']}.mp4"
        ov = tmp / f"list_{k}.png"
        list_layer(spec, it["rank"]).save(ov)
        speed = float(it.get("speed", 1.0))
        has_a = probe_audio(src)
        segs = it["segments"]
        fc, vlabels, alabels = [], [], []
        crop = it.get("crop")  # [x, y, w, h] as fractions of the source frame
        cropf = f"crop=iw*{crop[2]}:ih*{crop[3]}:iw*{crop[0]}:ih*{crop[1]}," if crop else ""
        for j, (a, b) in enumerate(segs):
            fc.append(f"[0:v]trim={a}:{b},setpts=(PTS-STARTPTS)/{speed},fps={FPS},{cropf}"
                      f"scale={W}:{H - HEADER}:force_original_aspect_ratio=increase,crop={W}:{H - HEADER},setsar=1[v{j}]")
            if has_a:
                fc.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS,atempo={speed},aresample=48000,"
                          f"aformat=channel_layouts=stereo[a{j}]")
            else:
                fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{(b - a) / speed}[a{j}]")
            vlabels.append(f"[v{j}]"); alabels.append(f"[a{j}]")
        fc.append("".join(v + a for v, a in zip(vlabels, alabels)) + f"concat=n={len(segs)}:v=1:a=1[cv][ca]")
        fc.append(f"[cv]pad={W}:{H}:0:{HEADER}:black[pv]")
        fc.append("[pv][1:v]overlay=0:0[hv]")
        fc.append("[hv][2:v]overlay=0:0,format=yuv420p[vout]")
        fc.append("[ca]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
        part = tmp / f"part_{k}.mp4"
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-i", str(head), "-i", str(ov),
               "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(FPS),
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(part)]
        subprocess.run(cmd, check=True)
        parts.append(part)
        print(f"  rendered #{it['rank']} ({it['src']})")
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    final = out / "final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(final)], check=True)
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                       capture_output=True, text=True).stdout.strip()
    print(f"done: {final}  ({float(d):.1f}s)")
    return final


if __name__ == "__main__":
    render(sys.argv[1])
