"""Validate a ranking project.json. Prints OK + timing summary, or the list of problems (exit 1).

  python validate.py projects/<slug>/project.json
"""
import json, os, subprocess, sys
from pathlib import Path

MIN_TOTAL, MAX_TOTAL = 30.0, 44.0
MIN_ITEM, MAX_ITEM = 5.0, 8.5  # 2026-10-25: was 4.5 - a 4s #1 payoff is too rushed to publish
NOWIN = 0x08000000 if os.name == "nt" else 0


def src_duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, creationflags=NOWIN)
    return float(r.stdout.strip())


def validate(path):
    path = Path(path).resolve()
    errs = []
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"not valid JSON: {e}"], None
    for k in ("title", "items", "yt_title"):
        if k not in spec:
            errs.append(f"missing key '{k}'")
    if errs:
        return errs, None
    if not isinstance(spec["title"], list) or not all(isinstance(t, list) and len(t) == 2 for t in spec["title"]):
        errs.append("title must be a list of [text, color] pairs")
    items = spec["items"]
    n = len(items)
    if n != 6:
        errs.append(f"need exactly 6 items, got {n}")
    ranks = [it.get("rank") for it in items]
    if ranks != list(range(n, 0, -1)):
        errs.append(f"items must be in play order {list(range(n, 0, -1))}, got {ranks}")
    srcs = [it.get("src") for it in items]
    if len(set(srcs)) != len(srcs):
        errs.append("each clip (src) may be used only once")
    # Label rule (2026-10-25 style bar): labels must be unique and must
    # not be a generic truncation like "i built a" - a duplicate or
    # generic label fails validation instead of publishing.
    _labels = [str(it.get("label", "")).lower().strip() for it in items]
    if len(set(_labels)) != len(_labels):
        errs.append("rank labels must be unique, got duplicates: " + ", ".join(_labels))
    for _lab in _labels:
        _core = _lab.replace("🔥", "").strip()
        if _core in ("i built a", "i built", "viral moment", ""):
            errs.append(f"rank label '{_lab}' is generic - use a specific item name or a Level number")
    total, lens = 0.0, []
    for it in items:
        tag = f"#{it.get('rank')} {it.get('src')}"
        raw = path.parent / "raw" / f"{it.get('src')}.mp4"
        if not raw.exists():
            errs.append(f"{tag}: raw/{it.get('src')}.mp4 not found"); continue
        dur = src_duration(raw)
        speed = float(it.get("speed", 1.0))
        if not 0.8 <= speed <= 1.3:
            errs.append(f"{tag}: speed {speed} outside 0.8-1.3")
        segs = it.get("segments") or []
        if not segs:
            errs.append(f"{tag}: no segments")
        L = 0.0
        prev_end = -1
        for a, b in segs:
            if not (0 <= a < b <= dur + 0.05):
                errs.append(f"{tag}: segment [{a}, {b}] outside clip length {dur:.2f}s")
            if a < prev_end:
                errs.append(f"{tag}: segments overlap / go backwards")
            prev_end = b
            L += (b - a) / speed
        if not MIN_ITEM <= L <= MAX_ITEM:
            errs.append(f"{tag}: item length {L:.1f}s outside {MIN_ITEM}-{MAX_ITEM}s")
        if not it.get("label") or len(it["label"]) > 26:
            errs.append(f"{tag}: label missing or longer than 26 chars")
        for c in it.get("captions", []):
            if len(c) != 3 or not (0 <= c[0] < c[1] <= L + 0.05):
                errs.append(f"{tag}: caption {c} must be [start, end, text] within 0-{L:.1f}s")
            elif len(c[2]) > 22:
                errs.append(f"{tag}: caption '{c[2]}' longer than 22 chars")
        crop = it.get("crop")
        if crop and not (len(crop) == 4 and all(0 <= v <= 1 for v in crop) and crop[0] + crop[2] <= 1.001
                         and crop[1] + crop[3] <= 1.001):
            errs.append(f"{tag}: bad crop {crop}")
        lens.append(round(L, 1))
        total += L
    if not MIN_TOTAL <= total <= MAX_TOTAL:
        errs.append(f"total length {total:.1f}s outside {MIN_TOTAL}-{MAX_TOTAL}s")
    if len(spec.get("yt_title", "")) > 90:
        errs.append("yt_title longer than 90 chars")
    return errs, (total, lens)


if __name__ == "__main__":
    errs, info = validate(sys.argv[1])
    if errs:
        print("INVALID:\n- " + "\n- ".join(errs))
        sys.exit(1)
    print(f"OK  total={info[0]:.1f}s  items={info[1]}")
