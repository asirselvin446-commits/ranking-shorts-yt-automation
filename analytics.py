"""Performance feedback for the daily pipeline.

- refresh(state): pulls stats for every published Short (YouTube Analytics API if enabled,
  otherwise basic view/like counts from the Data API) into state["slots"][...]["stats"].
- mood_scores(state): average "% of video watched" (or views) per mood.
- feedback_text(state): short summary handed to the clip-editor agent.

  python analytics.py        # print the current table
"""
import datetime as dt, json, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_AGE_H = 48            # stats settle after ~2 days


def _topics():
    return {t["slug"]: t for t in json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))}


def _published(state):
    now = dt.datetime.now().astimezone()
    out = []
    for key, s in state.get("slots", {}).items():
        slot = dt.datetime.strptime(key, "%Y-%m-%d %H:%M").astimezone()
        if (now - slot).total_seconds() / 3600 >= MIN_AGE_H:
            out.append(s)
    return out


def refresh(state, log=print):
    pubs = _published(state)
    if not pubs:
        return state
    import yt
    ids = [s["video_id"] for s in pubs]
    stats = {}
    try:
        from googleapiclient.discovery import build
        a = build("youtubeAnalytics", "v2", credentials=yt.get_creds(), cache_discovery=False)
        for i in range(0, len(ids), 200):
            r = a.reports().query(ids="channel==MINE", startDate="2026-01-01", endDate=dt.date.today().isoformat(),
                                  metrics="views,averageViewPercentage,averageViewDuration,likes,subscribersGained",
                                  dimensions="video", filters="video==" + ",".join(ids[i:i + 200])).execute()
            for row in r.get("rows", []):
                stats[row[0]] = {"views": row[1], "watch_pct": row[2], "watch_sec": row[3], "likes": row[4],
                                 "subs": row[5], "source": "analytics"}
    except Exception as e:
        log(f"analytics API unavailable ({str(e)[:90]}...) - using basic view counts")
    missing = [v for v in ids if v not in stats]
    if missing:
        c = yt.yt()
        for i in range(0, len(missing), 50):
            for v in c.videos().list(part="statistics", id=",".join(missing[i:i + 50])).execute()["items"]:
                s = v["statistics"]
                stats[v["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                  "source": "basic"}
    for s in pubs:
        if s["video_id"] in stats:
            s["stats"] = stats[s["video_id"]]
    return state


def mood_scores(state):
    topics = _topics()
    by = {}
    for s in _published(state):
        m = topics.get(s.get("topic"), {}).get("category")
        if m and s.get("stats"):
            by.setdefault(m, []).append(s["stats"])
    use_pct = any("watch_pct" in x for v in by.values() for x in v)
    key = "watch_pct" if use_pct else "views"
    return {m: st.mean(x.get(key, 0) for x in v) for m, v in by.items()}, key, sum(len(v) for v in by.values())


def feedback_text(state):
    topics = _topics()
    rows = [s for s in _published(state) if s.get("stats")]
    if len(rows) < 2:
        return ""
    key = "watch_pct" if any("watch_pct" in s["stats"] for s in rows) else "views"
    rows.sort(key=lambda s: -s["stats"].get(key, 0))
    fmt = lambda s: (f"- {s['title']} ({topics.get(s.get('topic'), {}).get('category', '?')}): "
                     + (f"{s['stats']['watch_pct']:.0f}% watched, " if "watch_pct" in s["stats"] else "")
                     + f"{s['stats']['views']:,} views")
    best, worst = rows[:3], rows[-2:]
    return ("\n## What worked on this channel so far (real stats)\nBest:\n" + "\n".join(map(fmt, best))
            + "\nWeakest:\n" + "\n".join(map(fmt, worst))
            + "\nLean towards the kind of moments in the best ones.\n")


COUNTRY_TZ = {"US": "America/New_York", "CA": "America/Toronto", "GB": "Europe/London", "IN": "Asia/Kolkata",
              "AU": "Australia/Sydney", "PH": "Asia/Manila", "PK": "Asia/Karachi", "ID": "Asia/Jakarta",
              "BR": "America/Sao_Paulo", "MX": "America/Mexico_City", "DE": "Europe/Berlin", "FR": "Europe/Paris",
              "NG": "Africa/Lagos", "BD": "Asia/Dhaka", "AE": "Asia/Dubai", "SA": "Asia/Riyadh",
              "MY": "Asia/Kuala_Lumpur", "VN": "Asia/Ho_Chi_Minh", "TR": "Europe/Istanbul", "EG": "Africa/Cairo"}


def audience_countries(days=28):
    """[(country, views)] for the channel over the last `days` days (YouTube Analytics API)."""
    import yt
    from googleapiclient.discovery import build
    a = build("youtubeAnalytics", "v2", credentials=yt.get_creds(), cache_discovery=False)
    end = dt.date.today()
    r = a.reports().query(ids="channel==MINE", startDate=(end - dt.timedelta(days=days)).isoformat(),
                          endDate=end.isoformat(), metrics="views", dimensions="country", sort="-views",
                          maxResults=10).execute()
    return [(row[0], int(row[1])) for row in r.get("rows", [])]


def retarget_schedule(log=print, min_views=500, min_share=0.35):
    """Auto mode: move the daily publish time to 19:00 in the channel's dominant audience country."""
    path = ROOT / "schedule.json"
    sch = json.loads(path.read_text(encoding="utf-8"))
    if sch.get("mode") != "auto":
        return sch
    rows = audience_countries()
    total = sum(v for _, v in rows)
    if total < min_views or not rows:
        return sch
    top, views = rows[0]
    tz = COUNTRY_TZ.get(top)
    if not tz or views / total < min_share or tz == sch.get("audience_tz"):
        return sch
    old = sch.get("audience_tz")
    sch.pop("local_time", None)
    sch.update({"audience_tz": tz, "slots": ["18:00", "21:00"],
                "reason": f"Auto-retargeted {dt.date.today()}: {top} is {views * 100 // total}% of the channel's "
                          f"last-28-day views ({total:,}); publishing at 18:00 and 21:00 {tz}. Previous: {old}."})
    path.write_text(json.dumps(sch, indent=1), encoding="utf-8")
    log(f"schedule retargeted {old} -> {tz} ({top} = {views * 100 // total}% of views)")
    return sch


COMMENTS = ["Which one deserves #1? 👇", "Did we get the order right? Drop your #1 👇",
            "Which one surprised you the most? 👇", "What should we rank next? 👇"]


def post_comments(state, log=print):
    """Once a scheduled Short is public, post a question comment from the channel to start replies."""
    import yt
    now = dt.datetime.now().astimezone()
    c = None
    for i, (key, s) in enumerate(sorted(state.get("slots", {}).items())):
        if s.get("commented"):
            continue
        slot = dt.datetime.strptime(key, "%Y-%m-%d %H:%M").astimezone()
        if now < slot + dt.timedelta(minutes=10):
            continue
        c = c or yt.yt()
        try:
            st_ = c.videos().list(part="status", id=s["video_id"]).execute()["items"][0]["status"]
            if st_["privacyStatus"] != "public":
                continue
            text = COMMENTS[i % len(COMMENTS)]
            c.commentThreads().insert(part="snippet", body={"snippet": {"videoId": s["video_id"],
                                      "topLevelComment": {"snippet": {"textOriginal": text}}}}).execute()
            s["commented"] = True
            log(f"posted comment on {s['video_id']}: {text}")
        except Exception as e:
            log(f"comment on {s['video_id']} failed: {str(e)[:120]}")
    return state


if __name__ == "__main__":
    state = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
    try:
        print("audience:", audience_countries())
    except Exception as e:
        print("audience: n/a", e)
    refresh(state)
    for k, s in state["slots"].items():
        print(k, s.get("title"), s.get("stats", "(too new)"))
    print(mood_scores(state))
