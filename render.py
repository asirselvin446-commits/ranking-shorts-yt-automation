"""Render a "Ranking ..." Short from a project spec.

  python render.py projects/<slug>/project.json        -> projects/<slug>/out/final.mp4

project.json:
{
  "title": [["Ranking ", "white"], ["Craziest", "yellow"], ["\\n", ""], ["Trick Shots", "blue"], [" 🎯", ""]],
  "subtitle": "Last one is insane 💀",            # optional small line under the title
  "items": [                                       # PLAY order: rank N first ... rank 1 last
    {"rank": 6, "src": "VIDEO_ID",
     "segments": [[3.5, 10.0]],                    # source seconds; several segments = cut out dead air
     "speed": 1.0,                                 # optional, keep 1.0 unless a slow build-up
     "crop": [0, 0, 1, 0.79],                      # optional [x, y, w, h] fractions of source frame
     "label": "dog goalie 🐕",                     # 2-3 words + emoji, shown in the rank list
     "captions": [[0.0, 2.0, "WAIT FOR IT 😳"]]     # optional, seconds relative to this item's output
    }
  ]
}
Clips are read from projects/<slug>/raw/<src>.mp4
"""
import json, os, subprocess, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
W, H, HEADER, FPS = 1080, 1920, 380, 30
FONT_XB = str(ROOT / "assets/fonts/Poppins-ExtraBold.ttf")
FONT_B = str(ROOT / "assets/fonts/Poppins-Bold.ttf")
_emoji_candidates = [
    Path("C:/Windows/Fonts/seguiemj.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
    Path("/usr/share/fonts/noto-cjk/NotoColorEmoji.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
]
EMOJI = None
for ec in _emoji_candidates:
    if ec.exists():
        EMOJI = str(ec)
        break
if not EMOJI:
    EMOJI = FONT_XB

WHOOSH = ROOT / "assets/sfx/whoosh.wav"
COLORS = {"white": (255, 255, 255), "yellow": (255, 230, 0), "blue": (40, 160, 255), "red": (255, 60, 60),
          "green": (60, 230, 90), "orange": (255, 150, 30), "pink": (255, 90, 200), "cyan": (0, 230, 255)}
RANK_COLORS = {1: (255, 214, 0), 2: (200, 210, 225), 3: (230, 140, 60)}
ACTIVE = (255, 230, 0)
LIST_TOP, LIST_STEP = HEADER + 80, 140
CAPTION_Y = 1330                                   # above the Shorts title/channel UI
NOWIN = 0x08000000 if os.name == "nt" else 0


def run(cmd):
    subprocess.run(cmd, check=True, creationflags=NOWIN)


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


def text_w(text, size, font_path=FONT_XB):
    f = ImageFont.truetype(font_path, size)
    try:
        fe = ImageFont.truetype(EMOJI, int(size * 0.9))
    except Exception:
        fe = f
    return sum((fe if e else f).getlength(t) for t, e in runs(text))


def draw_rich(d, x, y, text, size, fill, font_path=FONT_XB, stroke=0):
    f = ImageFont.truetype(font_path, size)
    try:
        fe = ImageFont.truetype(EMOJI, int(size * 0.9))
    except Exception:
        fe = f
    for t, e in runs(text):
        if e:
            try:
                d.text((x, y + size * 0.12), t, font=fe, embedded_color=True)
            except Exception:
                d.text((x, y + size * 0.12), t, font=fe, fill=fill)
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
    size = 84
    while max(sum(text_w(t, size) for t, _ in ln) for ln in lines) > W - 70:
        size -= 2
    sub = spec.get("subtitle")
    block = len(lines) * size * 1.18 + (size * 0.55 if sub else 0)
    y = (HEADER - block) / 2 - size * 0.1
    for ln in lines:
        x = (W - sum(text_w(t, size) for t, _ in ln)) / 2
        for t, c in ln:
            x = draw_rich(d, x, y, t, size, c)
        y += size * 1.18
    if sub:
        s2 = int(size * 0.46)
        draw_rich(d, (W - text_w(sub, s2, FONT_B)) / 2, y + 4, sub, s2, (255, 255, 255), FONT_B)
    return im


def list_layer(spec, active_rank):
    n = max(i["rank"] for i in spec["items"])
    labels = {i["rank"]: i["label"] for i in spec["items"]}
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for r in range(1, n + 1):
        y = LIST_TOP + (r - 1) * LIST_STEP
        active = r == active_rank
        nsize, lsize = (86, 56) if active else (70, 44)
        if active:  # translucent pill behind the active row
            lw = text_w(f"{r}.", nsize) + 22 + text_w(labels[r], lsize)
            d.rounded_rectangle([18, y - 18, min(W - 30, 50 + lw + 24), y + 92], radius=26, fill=(0, 0, 0, 120))
        x = draw_rich(d, 34, y - (nsize - 70) / 2, f"{r}.", nsize, RANK_COLORS.get(r, (255, 255, 255)), stroke=7)
        if r >= active_rank:
            draw_rich(d, x + 16, y + (70 - lsize) * 0.55, labels[r], lsize,
                      ACTIVE if active else (255, 255, 255), stroke=6)
    return im


def pop_frames(rank, folder, n=16):
    """Big '#N' that slams in and fades out at the start of each item."""
    txt = f"#{rank}"
    col = RANK_COLORS.get(rank, (255, 255, 255))
    for i in range(n):
        p = i / (n - 1)
        scale = 1.7 - 0.7 * min(1, p / 0.3)          # 1.7 -> 1.0 in the first 30%
        alpha = 255 if p < 0.6 else int(255 * (1 - (p - 0.6) / 0.4))
        size = int(250 * scale)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        draw_rich(d, (W - text_w(txt, size)) / 2, HEADER + (H - HEADER) * 0.40 - size / 2, txt, size, col,
                  stroke=max(8, size // 18))
        if alpha < 255:
            layer.putalpha(layer.getchannel("A").point(lambda v: v * alpha // 255))
        layer.save(folder / f"pop_{rank}_{i:02d}.png")
    return folder / f"pop_{rank}_%02d.png"


def caption_png(text, path):
    im = Image.new("RGBA", (W, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    size = 92
    while text_w(text, size) > W - 120:
        size -= 3
    draw_rich(d, (W - text_w(text, size)) / 2, (200 - size) / 2 - 10, text, size, (255, 255, 255), stroke=10)
    im.save(path)


def ensure_whoosh():
    if WHOOSH.exists():
        return
    WHOOSH.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=0.55:c=pink:a=0.9:r=48000",
         "-af", "highpass=f=250,lowpass=f=4500,afade=t=in:d=0.18,afade=t=out:st=0.22:d=0.33,"
                "aformat=channel_layouts=stereo,volume=0.55", str(WHOOSH)])


def probe_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, creationflags=NOWIN)
    return bool(r.stdout.strip())


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, creationflags=NOWIN)
    return float(r.stdout.strip())


def render(spec_path):
    spec_path = Path(spec_path).resolve()
    proj = spec_path.parent
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out, tmp = proj / "out", proj / "out/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    ensure_whoosh()
    head = tmp / "header.png"
    header_layer(spec).save(head)
    parts = []
    for k, it in enumerate(spec["items"]):
        src = proj / "raw" / f"{it['src']}.mp4"
        ov = tmp / f"list_{k}.png"
        list_layer(spec, it["rank"]).save(ov)
        pop = pop_frames(it["rank"], tmp)
        speed = float(it.get("speed", 1.0))
        has_a = probe_audio(src)
        segs = it["segments"]
        crop = it.get("crop")
        cropf = f"crop=iw*{crop[2]}:ih*{crop[3]}:iw*{crop[0]}:ih*{crop[1]}," if crop else ""
        inputs = ["-i", str(src), "-i", str(head), "-i", str(ov), "-framerate", str(FPS), "-i", str(pop),
                  "-i", str(WHOOSH)]
        fc, vl, al = [], [], []
        for j, (a, b) in enumerate(segs):
            fc.append(f"[0:v]trim={a}:{b},setpts=(PTS-STARTPTS)/{speed},fps={FPS},{cropf}"
                      f"scale={W}:{H - HEADER}:force_original_aspect_ratio=increase,crop={W}:{H - HEADER},setsar=1[v{j}]")
            if has_a:
                fc.append(f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS,atempo={speed},aresample=48000,"
                          f"aformat=channel_layouts=stereo[a{j}]")
            else:
                fc.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{(b - a) / speed}[a{j}]")
            vl.append(f"[v{j}]"); al.append(f"[a{j}]")
        fc.append("".join(v + a for v, a in zip(vl, al)) + f"concat=n={len(segs)}:v=1:a=1[cv][ca]")
        fc.append(f"[cv]pad={W}:{H}:0:{HEADER}:black[pv]")
        fc.append("[pv][1:v]overlay=0:0[hv]")
        fc.append("[hv][2:v]overlay=0:0[lv]")
        last = "lv"
        for c, (t0, t1, text) in enumerate(it.get("captions", [])):
            cp = tmp / f"cap_{k}_{c}.png"
            caption_png(text, cp)
            inputs += ["-i", str(cp)]
            fc.append(f"[{last}][{5 + c}:v]overlay=0:{CAPTION_Y}:enable='between(t,{t0},{t1})'[c{c}]")
            last = f"c{c}"
        fc.append(f"[{last}][3:v]overlay=0:0:eof_action=pass,format=yuv420p[vout]")
        fc.append("[ca]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[ln]")
        fc.append("[ln][4:a]amix=inputs=2:duration=first:normalize=0[aout]")
        part = tmp / f"part_{k}.mp4"
        run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fc),
             "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(part)])
        parts.append(part)
        print(f"  rendered #{it['rank']} ({it['src']}) {duration(part):.1f}s", flush=True)
    lst = tmp / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    final = out / "final.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c:v", "libx264", "-preset", "slow", "-crf", "21", "-maxrate", "6M", "-bufsize", "12M",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)])
    print(f"done: {final}  ({duration(final):.1f}s)", flush=True)
    return final


if __name__ == "__main__":
    render(sys.argv[1])
