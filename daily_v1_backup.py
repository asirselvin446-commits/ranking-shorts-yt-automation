"""Daily Ranking-Short pipeline. Triggered at Windows logon / unlock / wake (Task Scheduler).

Makes ONE Short per day and schedules it to go public at 18:00 local time:
  - before 18:00 -> today's 18:00 slot;  after 18:00 -> tomorrow's 18:00 slot
  - exits immediately if that slot already has a video (safe to trigger many times a day)
  - if the slot is <10 min away when the upload finishes, it publishes immediately instead

  python daily.py             # normal run
  python daily.py --dry-run   # build the video but don't upload
  python daily.py --force     # ignore 'slot already filled'
"""
import argparse, datetime as dt, json, os, shutil, subprocess, sys, time, traceback, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv/Scripts/python.exe"
HERMES = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\asus\AppData\Local")) / "hermes/hermes-agent/venv/Scripts/hermes.exe"
STATE, LOCK, LOGS = ROOT / "state.json", ROOT / "daily.lock", ROOT / "logs"
PUBLISH_HOUR = 18
N_CANDIDATES = 14
NOWIN = 0x08000000 if os.name == "nt" else 0
EXTRA_PATH = [
    ROOT / ".venv/Scripts",
    Path.home() / "AppData/Local/hermes/tools/ffmpeg-9.0.1-win32-x64/bin",
    Path.home() / "AppData/Local/hermes/tools/node-26.7.0-win32-x64",
]
os.environ["PATH"] = os.pathsep.join(str(p) for p in EXTRA_PATH if p.exists()) + os.pathsep + os.environ.get("PATH", "")
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(ROOT))

LOGS.mkdir(exist_ok=True)
LOGFILE = LOGS / f"daily-{dt.date.today():%Y%m%d}.log"


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with LOGFILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass


def notify(title, body):
    """Best-effort Windows toast."""
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t.GetElementsByTagName('text').Item(0).AppendChild($t.CreateTextNode('{title.replace(chr(39), '')}')) > $null;"
        f"$t.GetElementsByTagName('text').Item(1).AppendChild($t.CreateTextNode('{body.replace(chr(39), '')}')) > $null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NOWIN, timeout=30,
                       capture_output=True)
    except Exception:
        pass


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"slots": {}, "used_topics": [], "used_clips": []}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")


def acquire_lock():
    if LOCK.exists():
        age = time.time() - LOCK.stat().st_mtime
        if age < 3 * 3600:
            return False
    LOCK.write_text(str(os.getpid()))
    return True


def load_schedule():
    p = ROOT / "schedule.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"mode": "auto", "audience_tz": "America/New_York", "local_time": "19:00"}


def target_slot(now):
    """Next daily slot: `local_time` in the audience's time zone (DST-safe), as local IST time."""
    from zoneinfo import ZoneInfo
    sch = load_schedule()
    tz = ZoneInfo(sch.get("audience_tz", "America/New_York"))
    hh, mm = map(int, sch.get("local_time", "19:00").split(":"))
    base = now.astimezone(tz).date()
    for off in range(0, 3):
        d = base + dt.timedelta(days=off)
        slot = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(now.tzinfo)
        if slot > now:
            return slot
    return slot


def wait_for_internet(max_wait=900):
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            urllib.request.urlopen("https://www.googleapis.com/discovery/v1/apis", timeout=10)
            return True
        except Exception:
            time.sleep(20)
    return False


def sh(cmd, timeout=None, cwd=None):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NOWIN, timeout=timeout, cwd=cwd)
    return r


def pick_topic(state):
    """Next unused topic from a different niche than the last Short (niches not used in the
    last 3 Shorts first). Once >=4 Shorts have stats, favour the niches whose Shorts are
    watched the longest (75% best niche, 25% explore untested/other niches)."""
    import random, analytics
    topics = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
    by_slug = {t["slug"]: t for t in topics}
    used = state["used_topics"]
    fresh = [t for t in topics if t["slug"] not in used]
    if not fresh:  # all used: restart the cycle, skipping the most recent 10
        recent = set(used[-10:])
        fresh = [t for t in topics if t["slug"] not in recent] or topics
    recent_cats = [by_slug.get(s, {}).get("category") for s in used[-3:]]
    last_cat = recent_cats[-1] if recent_cats else None
    pool = ([t for t in fresh if t["category"] not in recent_cats]
            or [t for t in fresh if t["category"] != last_cat] or fresh)
    scores, metric, n = analytics.mood_scores(state)
    if n >= 4 and scores:
        rng = random.Random(dt.date.today().toordinal())
        avail = {t["category"] for t in pool}
        ranked = sorted((c for c in scores if c in avail), key=lambda c: -scores[c])
        untested = sorted(c for c in avail if c not in scores)
        if rng.random() < 0.75 and ranked:
            want = ranked[0]
        else:
            want = rng.choice(untested or sorted(avail))
        log(f"niche scores ({metric}): {scores} -> want {want}")
        pool = [t for t in pool if t["category"] == want] or pool
    return pool[0]


def gather(topic, proj, state):
    import source
    exclude = set(state["used_clips"])
    rows = []
    for min_views in (2_000_000, 500_000, 100_000):
        rows = [r for r in source.search(topic["queries"], min_views=min_views, max_sec=90) if r["id"] not in exclude]
        if len(rows) >= 10:
            break
    log(f"found {len(rows)} candidate clips")
    (proj / "raw").mkdir(parents=True, exist_ok=True)
    (proj / "sheets").mkdir(exist_ok=True)
    got = []
    for r in rows[: N_CANDIDATES + 4]:
        if len(got) >= N_CANDIDATES:
            break
        out = proj / "raw" / f"{r['id']}.mp4"
        res = sh([PY, "-m", "yt_dlp", "-q", "--no-progress", "--no-playlist", "--js-runtimes", "node", "-f",
                  "bv*[height<=1920][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b", "--merge-output-format", "mp4",
                  "--write-info-json", "-o", str(proj / "raw" / "%(id)s.%(ext)s"), r["url"]], timeout=300)
        if not out.exists():
            log(f"  download failed {r['id']}: {res.stderr.strip()[-200:]}")
            continue
        info = json.loads((proj / "raw" / f"{r['id']}.info.json").read_text(encoding="utf-8"))
        r["w"], r["h"] = info.get("width"), info.get("height")
        r["uploader"] = info.get("uploader") or r["channel"]
        sh(["ffmpeg", "-v", "error", "-y", "-i", out, "-vf",
            "fps=1,scale=170:-1,drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='%{eif\\:t\\:d}s':"
            "x=5:y=5:fontsize=26:fontcolor=yellow:borderw=3,tile=8x12", "-frames:v", "1",
            proj / "sheets" / f"{r['id']}.jpg"], timeout=300)
        got.append(r)
    lines = [f"# Candidates for: {topic['name']}\n", "| id | views | length | WxH | title | channel |", "|---|---|---|---|---|---|"]
    for r in got:
        lines.append(f"| {r['id']} | {r['views']:,} | {r['sec']}s | {r['w']}x{r['h']} | {r['title'][:70]} | {r['channel']} |")
    (proj / "candidates.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (proj / "candidates.json").write_text(json.dumps(got, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"downloaded {len(got)} clips")
    return got


def run_agent(topic, proj, extra="", feedback=""):
    prompt = (ROOT / "agent_prompt.md").read_text(encoding="utf-8")
    for k, v in {"{TOPIC}": topic["name"], "{CATEGORY}": topic.get("category", "general"), "{PROJ}": proj.as_posix(),
                 "{PY}": PY.as_posix(), "{ROOT}": ROOT.as_posix(), "{FEEDBACK}": feedback}.items():
        prompt = prompt.replace(k, v)
    if extra:
        prompt += "\n\n## Fix this\n" + extra
    pf = proj / "agent_prompt.md"
    pf.write_text(prompt, encoding="utf-8")
    log("running clip-editor agent ...")
    r = sh([HERMES, "chat", "--query-file", pf, "-Q", "--yolo", "-s", "youtube-ranking-shorts",
            "--in", proj, "--run-budget", "2700", "--source", "yt-daily"], timeout=3300, cwd=proj)
    (proj / "agent_output.txt").write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""), encoding="utf-8")
    log(f"agent exit={r.returncode}: {(r.stdout or '').strip()[-200:]}")


def build_meta(proj, spec, cands):
    by_id = {c["id"]: c for c in cands}
    credits = []
    for it in sorted(spec["items"], key=lambda i: i["rank"]):
        c = by_id.get(it["src"], {})
        credits.append(f"#{it['rank']} {c.get('uploader') or c.get('channel', '')} – https://youtube.com/shorts/{it['src']}")
    subject = "".join(t for t, _ in spec["title"] if t != "\n").strip()
    tags = ["ranking", "shorts", "viral", "funny", "best moments"] + [w.lower() for w in subject.split() if w.isalpha()][1:]
    return {
        "title": spec["yt_title"][:100],
        "description": f"Which one is your #1? Comment below 👇\n\n{subject}\n\nCredits:\n" + "\n".join(credits)
                       + "\n\n#shorts #ranking #viral",
        "tags": list(dict.fromkeys(tags))[:15],
        "category_id": "24",
        "made_for_kids": False,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if not acquire_lock():
        print("another run is in progress"); return
    try:
        state = load_state()
        # maintenance that runs on every trigger: comment on Shorts that just went public,
        # and move the publish time to wherever the channel's real audience is
        if wait_for_internet(max_wait=300):
            try:
                import analytics
                analytics.post_comments(state, log=log); save_state(state)
                analytics.retarget_schedule(log=log)
            except Exception as e:
                log(f"maintenance skipped: {str(e)[:150]}")
        now = dt.datetime.now().astimezone()
        slot = target_slot(now)
        key = slot.strftime("%Y-%m-%d %H:%M")
        if key in state["slots"] and not a.force and not a.dry_run:
            log(f"slot {key} already filled ({state['slots'][key].get('video_id')}) - nothing to do")
            return
        log(f"=== building Short for slot {key} ===")
        if not wait_for_internet():
            log("no internet - giving up for now"); return

        import yt
        yt.get_creds()  # fails fast if the YouTube login expired
        import analytics
        try:
            analytics.refresh(state, log=log); save_state(state)
        except Exception as e:
            log(f"stats refresh failed: {e}")
        feedback = analytics.feedback_text(state)
        topic = pick_topic(state)
        proj = ROOT / "projects" / f"{slot:%Y%m%d}-{topic['slug']}"
        if proj.exists() and not (proj / "project.json").exists():
            shutil.rmtree(proj, ignore_errors=True)
        proj.mkdir(parents=True, exist_ok=True)
        log(f"topic: {topic['name']}  ->  {proj}")

        cands = json.loads((proj / "candidates.json").read_text(encoding="utf-8")) if (proj / "candidates.json").exists() else gather(topic, proj, state)
        if len(cands) < 7:
            log("not enough clips for this topic - marking it used and stopping; next trigger tries another topic")
            state["used_topics"].append(topic["slug"]); save_state(state); return

        import validate
        spec_path = proj / "project.json"
        errs = ["project.json not written"]
        for attempt in range(2):
            if spec_path.exists():
                errs, _ = validate.validate(spec_path)
                if not errs:
                    break
            run_agent(topic, proj, extra="" if attempt == 0 else "Previous attempt failed validation:\n- " + "\n- ".join(errs),
                      feedback=feedback)
            errs, info = validate.validate(spec_path) if spec_path.exists() else (["project.json not written"], None)
            if not errs:
                break
        if errs:
            log("agent could not produce a valid project: " + "; ".join(errs))
            notify("YT Shorts: failed", "Clip selection failed - will retry next time you log in.")
            return

        import render
        final = render.render(spec_path)
        d = render.duration(final)
        if not 30 <= d <= 60:
            log(f"rendered length {d:.1f}s is off - aborting"); return
        sh(["ffmpeg", "-v", "error", "-y", "-i", final, "-vf", "fps=1,scale=180:-1,tile=10x6", "-frames:v", "1",
            proj / "out" / "qa_sheet.jpg"])
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        meta = build_meta(proj, spec, cands)
        (proj / "out" / "final.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")

        if a.dry_run:
            log(f"dry run - video at {final}"); return

        now = dt.datetime.now().astimezone()
        if slot - now > dt.timedelta(minutes=10):
            meta["publish_at"] = slot.isoformat()
            when = f"scheduled for {key}"
        else:
            meta["privacy"] = "public"
            when = "published now (slot time already reached)"
        vid = yt.upload(final, meta)
        time.sleep(60)
        st = yt.yt().videos().list(part="status", id=vid).execute()["items"][0]["status"]
        log(f"uploaded https://youtu.be/{vid}  {when}  status={st}")
        state["slots"][key] = {"video_id": vid, "topic": topic["slug"], "title": meta["title"],
                               "status": st, "project": proj.name, "created": now.isoformat()}
        state["used_topics"].append(topic["slug"])
        state["used_clips"] += [it["src"] for it in spec["items"]]
        save_state(state)
        notify("YT Short ready ✅", f"{meta['title']} – {when}")

        # weekly trend research: add fresh topics from what's going viral right now
        topics_all = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
        n_fresh = sum(1 for t in topics_all if t["slug"] not in state["used_topics"])
        last = state.get("last_discover")
        if n_fresh < 10 or not last or (dt.date.today() - dt.date.fromisoformat(last)).days >= 7:
            try:
                import discover
                rows = discover.research()
                added = discover.run_agent()
                state["last_discover"] = dt.date.today().isoformat(); save_state(state)
                log(f"trend research: {len(rows)} viral ranking Shorts -> {added} new topics")
            except Exception as e:
                log(f"trend research failed: {e}")

        # housekeeping: drop raw downloads of projects older than 10 days
        for p in (ROOT / "projects").iterdir():
            if p.is_dir() and (p / "raw").exists() and time.time() - p.stat().st_mtime > 10 * 86400:
                shutil.rmtree(p / "raw", ignore_errors=True)
    except SystemExit as e:
        log(f"stopped: {e}")
        notify("YT Shorts: action needed", str(e)[:120])
    except Exception:
        log("ERROR\n" + traceback.format_exc())
        notify("YT Shorts: error", "See yt-automation/logs for details.")
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
