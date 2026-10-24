"""Find what ranking Shorts are going viral right now (any niche) and add new topics.

  python discover.py            # research + let the agent append new topics to topics.json
  python discover.py --no-agent # only write research/trending.md
"""
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
PY = ROOT / ".venv/Scripts/python.exe"
HERMES = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\asus\AppData\Local")) / "hermes/hermes-agent/venv/Scripts/hermes.exe"
TOPICS = ROOT / "topics.json"
TREND = ROOT / "research" / "trending.md"
QUERIES = ["ranking", "ranking best", "ranking most", "ranking craziest", "ranking strongest",
           "ranking fastest", "ranking funniest", "ranking satisfying"]


def research(days=30, min_views=1_000_000):
    import yt
    c = yt.yt()
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = {}
    for q in QUERIES:
        r = c.search().list(part="snippet", q=q, type="video", videoDuration="short", order="viewCount",
                            publishedAfter=after, maxResults=50).execute()
        for i in r["items"]:
            if i["snippet"]["title"].lower().startswith("ranking"):
                ids[i["id"]["videoId"]] = i["snippet"]["channelTitle"]
    rows = []
    idl = list(ids)
    for k in range(0, len(idl), 50):
        for v in c.videos().list(part="snippet,statistics", id=",".join(idl[k:k + 50])).execute()["items"]:
            views = int(v["statistics"].get("viewCount", 0))
            if views >= min_views:
                rows.append((views, v["snippet"]["title"], ids[v["id"]], v["snippet"]["publishedAt"][:10]))
    rows.sort(reverse=True)
    TREND.parent.mkdir(exist_ok=True)
    lines = [f"# Viral 'Ranking' Shorts, last {days} days (>= {min_views:,} views), {dt.date.today()}\n",
             "| views | title | channel | date |", "|---|---|---|---|"]
    lines += [f"| {v:,} | {t} | {ch} | {d} |" for v, t, ch, d in rows]
    TREND.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def check_topics(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    slugs = set()
    for t in data:
        assert set(t) >= {"slug", "category", "name", "queries"}, f"missing keys in {t}"
        assert re.fullmatch(r"[a-z0-9-]+", t["slug"]), f"bad slug {t['slug']}"
        assert t["slug"] not in slugs, f"duplicate slug {t['slug']}"
        assert isinstance(t["queries"], list) and len(t["queries"]) == 3, f"need 3 queries in {t['slug']}"
        slugs.add(t["slug"])
    return len(data)


PROMPT = """You maintain the topic list for a YouTube Shorts "Ranking ..." channel (6 real viral clips ranked 6 -> 1, 40-50 s).
Goal of every Short: viewers watch to the end. Any niche is fine (technology, physique/fitness, cars, animals, sports, science, food, places, funny, talent, extreme sports, engineering ...).

1. Read `{TREND}`: ranking Shorts that went viral in the last 30 days, with views.
2. Read `{TOPICS}` (existing topics; do not duplicate or near-duplicate them).
3. Append {N} NEW topics to `{TOPICS}` (keep every existing entry unchanged, keep valid JSON). Prefer subjects that are trending or proven in the research, spread across several different niches.
   Each topic must be filmable with plenty of real, visual, viral short clips on YouTube (not talking, not reviews, not CGI) where the moment is understood in 1 second.
   Entry format: {{"slug": "lowercase-hyphen-slug", "category": "<one-word niche>", "name": "Most Insane X" (the subject as it appears after "Ranking "), "queries": ["<search 1> #shorts", "<search 2>", "<search 3>"]}}
   Queries must find single-moment clips, not compilations.
4. Check with: `{PY} -c "import sys; sys.path.insert(0, r'{ROOT}'); import discover; print(discover.check_topics(r'{TOPICS}'))"`
5. Reply with just DONE.
"""


def run_agent(n=12):
    backup = TOPICS.with_suffix(".bak.json")
    shutil.copy(TOPICS, backup)
    before = check_topics(TOPICS)
    pf = ROOT / "research" / "discover_prompt.md"
    pf.write_text(PROMPT.format(TREND=TREND.as_posix(), TOPICS=TOPICS.as_posix(), N=n, PY=PY.as_posix(),
                                ROOT=ROOT.as_posix()), encoding="utf-8")
    subprocess.run([str(HERMES), "chat", "--provider", "copilot", "-m", "gpt-4o",
                    "--query-file", str(pf), "-Q", "--yolo", "--in", str(ROOT),
                    "--run-budget", "900", "--source", "yt-discover"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=1200, creationflags=0x08000000)
    try:
        after = check_topics(TOPICS)
        old = {t["slug"] for t in json.loads(backup.read_text(encoding="utf-8"))}
        assert old <= {t["slug"] for t in json.loads(TOPICS.read_text(encoding="utf-8"))}, "existing topics removed"
        return after - before
    except Exception as e:
        shutil.copy(backup, TOPICS)
        raise RuntimeError(f"topic update rejected, restored backup: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-agent", action="store_true")
    a = ap.parse_args()
    rows = research()
    print(f"{len(rows)} viral ranking Shorts in the last 30 days -> {TREND}")
    for v, t, ch, d in rows[:25]:
        print(f"{v:>12,}  {t[:70]}  | {ch}")
    if not a.no_agent:
        print(f"added {run_agent()} topics")
