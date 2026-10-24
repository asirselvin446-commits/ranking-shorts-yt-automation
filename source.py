"""Find viral YouTube Short clips for a ranking topic (add --cc-only for Creative Commons only).

  python source.py "trick shot" "pool trick shot" --min-views 50000 --out projects/<slug>/candidates.json
"""
import argparse, json, re
from pathlib import Path
import yt

BAD = re.compile(r"\b(compilation|rate this|roblox|minecraft|fortnite|gta|tutorial|reaction|ranking|ranked|top \d+|full video|podcast|live|ai generated|animation)\b", re.I)


def dur(s):
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', s)
    h, mi, se = [int(x or 0) for x in m.groups()]
    return h * 3600 + mi * 60 + se


def search(queries, min_views=50_000, max_sec=90, per_query=50, cc_only=False):
    c = yt.yt()
    seen, out = set(), []
    for q in queries:
        kw = dict(videoLicense="creativeCommon") if cc_only else {}
        r = c.search().list(part="snippet", q=q, type="video", maxResults=per_query,
                            order="viewCount", videoDuration="short", **kw).execute()
        ids = [i["id"]["videoId"] for i in r["items"] if i["id"]["videoId"] not in seen]
        if not ids:
            continue
        vs = c.videos().list(part="snippet,statistics,status,contentDetails", id=",".join(ids)).execute()["items"]
        for v in vs:
            seen.add(v["id"])
            title = v["snippet"]["title"]
            row = {"id": v["id"], "title": title, "channel": v["snippet"]["channelTitle"],
                   "channel_id": v["snippet"]["channelId"], "views": int(v["statistics"].get("viewCount", 0)),
                   "sec": dur(v["contentDetails"]["duration"]), "license": v["status"].get("license"),
                   "embeddable": v["status"].get("embeddable"), "query": q,
                   "url": f"https://www.youtube.com/watch?v={v['id']}"}
            if (cc_only and row["license"] != "creativeCommon") or row["views"] < min_views or row["sec"] > max_sec or row["sec"] < 4:
                continue
            if BAD.search(title):
                continue
            out.append(row)
    # one clip per channel (best), sorted by views
    best = {}
    for r in sorted(out, key=lambda r: -r["views"]):
        best.setdefault(r["channel_id"], r)
    return sorted(best.values(), key=lambda r: -r["views"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--min-views", type=int, default=50_000)
    ap.add_argument("--out")
    ap.add_argument("--cc-only", action="store_true")
    ap.add_argument("--max-sec", type=int, default=60)
    a = ap.parse_args()
    rows = search(a.queries, a.min_views, a.max_sec, cc_only=a.cc_only)
    for r in rows:
        print(f"{r['views']:>11,} {r['sec']:>3}s  {r['id']}  {r['title'][:70]} | {r['channel']}")
    print(f"-> {len(rows)} usable clips (distinct channels)")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
