"""Daily Ranking-Short pipeline. Triggered at Windows logon / unlock / wake (Task Scheduler).

Keeps the next 2 publish slots filled (schedule.json: daily times in the audience time zone):
  - builds + schedules a Short for each empty upcoming slot
  - exits immediately if they are already filled (safe to trigger many times a day)
  - if the slot is <10 min away when the upload finishes, it publishes immediately instead

  python daily.py             # normal run
  python daily.py --dry-run   # build the video but don't upload
  python daily.py --force     # ignore 'slot already filled'
"""
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys, time, traceback, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")  # slot keys / log times are IST on every runner


class DownloadError(Exception):
    """Candidate clips were found but none could be downloaded.

    This is an infrastructure failure (e.g. YouTube blocking this runner),
    not a bad topic, so the topic must NOT be marked used."""


class TopicFitError(Exception):
    """Too few candidates actually show the topic (2026-10-25 style bar).

    The Gaming Setups Short shipped secret rooms, a cycling bike, a
    bathroom and a door tunnel under a "Gaming Setups" title because the
    title came from the topic name alone. A topic whose candidates do
    not fit must be skipped, never published as a mismatched set."""


# Words in a topic name that carry no subject meaning (dropped when
# deriving the topic-fit keywords). Everything left is a content word.
GENERIC_TOPIC_WORDS = {
    "ranking", "craziest", "crazy", "best", "most", "unreal", "funniest",
    "funny", "satisfying", "amazing", "coolest", "insane", "top", "ever",
    "moments", "moment", "clips", "videos", "video", "of", "the", "and",
    # Filler words that appear in topic names / queries but carry no
    # subject meaning (added 2026-10-25 after run #9): "items" in
    # "3D Printed Items", "hits" in "Punching Machine Hits",
    # "removal" in "Snow Removal", "strongest" as a pure adjective,
    # "shorts" in every "#shorts" query.
    "shorts", "strongest", "items", "hits", "removal",
}

# Generic leading words that must never survive into a rank label.
GENERIC_LABEL_WORDS = {"i", "built", "a", "an", "the", "this", "is", "my"}


def _stem(w):
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    return w


def topic_fit_keywords(topic):
    """Distinctive subject words for a topic, from its name AND its
    search queries, minus generic/filler words.

    Run #9 proved the old rule (every word of the topic NAME must
    appear in the title) is wrong for this pipeline: the search
    queries are deliberately broader than the name - "snow blower
    satisfying" never says "removal", "boxing machine record" never
    says "punching", "game winning shot" never says "buzzer" - so
    good topics scored 0-2/14 and were burned. The query words ARE
    the topic's intent, so they belong in the keyword set."""
    texts = [topic.get("name", "")] + list(topic.get("queries") or [])
    words = []
    for t in texts:
        words += re.sub(r"[^\w\s]", "", (t or "")).lower().split()
    return {_stem(w) for w in words if w not in GENERIC_TOPIC_WORDS}


def candidate_fits_topic(title, keywords):
    """True if the candidate's title shares the topic's subject.

    A title fits when it shares at least TWO distinctive keywords
    (stemmed) with the topic's name+queries - e.g. "game winning
    shot" fits Buzzer Beaters (game + winning + shot), "snow blower"
    fits Snow Removal (snow + blower), "boxing machine record" fits
    Punching Machine (boxing + machine + record). One shared word
    alone is too weak; requiring every word was run #9's failure.
    This gate is a COARSE filter only: the agent's visual check of
    the clips is the final topic-fit judge before any publish, so a
    loosely matching title can still be rejected on sight."""
    if not keywords:
        return True
    title_words = re.sub(r"[^\w\s]", "", (title or "")).lower().split()
    title_stems = {_stem(w) for w in title_words}
    shared = keywords & title_stems
    return len(shared) >= min(2, len(keywords))


def filter_topic_fit(cands, topic):
    """Split candidates into (fitting, rejected) for the topic-fit gate."""
    keywords = topic_fit_keywords(topic)
    fitting = [c for c in cands if candidate_fits_topic(c.get("title", ""), keywords)]
    rejected = [c for c in cands if c not in fitting]
    return fitting, rejected, keywords


def level_label(rank):
    """RankZilla / MrPurifiedWater fallback: Level 1 is the lowest rank,
    Level 6 is #1, so Level = 7 - rank."""
    return f"Level {7 - rank}"

ROOT = Path(__file__).resolve().parent
PY = Path(sys.executable)
HERMES = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\asus\AppData\Local")) / "hermes/hermes-agent/venv/Scripts/hermes.exe"
STATE, LOCK, LOGS = ROOT / "state.json", ROOT / "daily.lock", ROOT / "logs"
SLOTS_AHEAD = 2          # keep this many upcoming publish slots filled
N_CANDIDATES = 14
NOWIN = 0x08000000 if os.name == "nt" else 0
EXTRA_PATH = [
    ROOT / ".venv/Scripts",
    ROOT / ".venv/bin",
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
    if os.name != "nt":
        return
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
    for r in rows:
        if len(got) >= N_CANDIDATES:
            break
        out = proj / "raw" / f"{r['id']}.mp4"
        downloaded = False
        last_err = ""
        cookies_file = ROOT / "cookies.txt"
        cookie_args = ["--cookies", str(cookies_file)] if cookies_file.exists() else []
        if cookie_args:
            log("  using cookies.txt for yt-dlp (file present, contents never logged)")
        if out.exists():
            out.unlink()
        res = sh([PY, "-m", "yt_dlp", "-q", "--no-progress", "--no-playlist",
                  "--js-runtimes", "node", "--remote-components", "ejs:github",
                  "--extractor-args", "youtube:player_client=mweb",
                  "--retries", "1", "--fragment-retries", "1", "--socket-timeout", "30",
                  ] + cookie_args + [
                  "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/best[ext=mp4]/best", "--merge-output-format", "mp4",
                  "--write-info-json", "-o", str(proj / "raw" / "%(id)s.%(ext)s"), r["url"]], timeout=300)
        if not out.exists():
            # Secondary fallback: web client with webpage skipped
            res = sh([PY, "-m", "yt_dlp", "-q", "--no-progress", "--no-playlist",
                      "--js-runtimes", "node", "--remote-components", "ejs:github",
                      "--extractor-args", "youtube:player_client=web;player_skip=webpage",
                      "--retries", "1", "--fragment-retries", "1", "--socket-timeout", "30",
                      ] + cookie_args + [
                      "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/best[ext=mp4]/best", "--merge-output-format", "mp4",
                      "--write-info-json", "-o", str(proj / "raw" / "%(id)s.%(ext)s"), r["url"]], timeout=300)
        if out.exists():
            downloaded = True
        else:
            # Log enough of the error to actually diagnose it: the old
            # last-200-chars truncation hid the whole yt-dlp error in
            # run #6, leaving only its cookies-advice tail visible.
            last_err = res.stderr.strip()[-2000:] if res.stderr else ""
        # Pacing: never hammer YouTube with back-to-back downloads.
        time.sleep(3)
        if not downloaded:
            log(f"  download failed {r['id']} (mweb+cookies+ejs): {last_err}")
            continue
        info = json.loads((proj / "raw" / f"{r['id']}.info.json").read_text(encoding="utf-8"))
        r["w"], r["h"] = info.get("width"), info.get("height")
        r["uploader"] = info.get("uploader") or r["channel"]
        font_opt = "fontfile='C\\:/Windows/Fonts/arialbd.ttf':" if os.name == "nt" else ""
        sh(["ffmpeg", "-v", "error", "-y", "-i", out, "-vf",
            f"fps=1,scale=170:-1,drawtext={font_opt}text='%{{eif\\:t\\:d}}s':"
            "x=5:y=5:fontsize=26:fontcolor=yellow:borderw=3,tile=8x12", "-frames:v", "1",
            proj / "sheets" / f"{r['id']}.jpg"], timeout=300)
        got.append(r)
    lines = [f"# Candidates for: {topic['name']}\n", "| id | views | length | WxH | title | channel |", "|---|---|---|---|---|---|"]
    for r in got:
        lines.append(f"| {r['id']} | {r['views']:,} | {r['sec']}s | {r['w']}x{r['h']} | {r['title'][:70]} | {r['channel']} |")
    (proj / "candidates.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (proj / "candidates.json").write_text(json.dumps(got, indent=1, ensure_ascii=False), encoding="utf-8")
    if not got and rows:
        # Many candidates found but zero downloaded = the download route is
        # broken (e.g. YouTube blocking this runner), NOT a bad topic.
        # Raise so the caller fails loudly instead of burning the topic.
        raise DownloadError(f"downloaded 0 of {len(rows)} candidate clips (last error: {last_err})")
    log(f"downloaded {len(got)} clips")
    return got


def generate_deterministic_spec(topic, proj, cands):
    import validate
    raw_dir = proj / "raw"
    valid_cands = []
    for c in cands:
        raw_p = raw_dir / f"{c['id']}.mp4"
        if raw_p.exists():
            try:
                d = validate.src_duration(raw_p)
                if d >= 5.5:
                    c_copy = dict(c)
                    c_copy["actual_dur"] = d
                    valid_cands.append(c_copy)
            except Exception:
                continue

    if len(valid_cands) < 6:
        for c in cands:
            raw_p = raw_dir / f"{c['id']}.mp4"
            if raw_p.exists() and not any(vc["id"] == c["id"] for vc in valid_cands):
                valid_cands.append(c)

    # Prioritize candidates whose title shares the core topic words (e.g. buzzer, snow)
    core_words = [w.lower() for w in topic.get("name", "").split() if len(w) > 3 and w.lower() not in GENERIC_TOPIC_WORDS]
    def cand_score(c):
        t = c.get("title", "").lower()
        core_hits = sum(1 for w in core_words if _stem(w) in t)
        return (core_hits, c.get("views", 0))

    chosen = sorted(valid_cands, key=cand_score, reverse=True)[:6]
    items = []
    play_order_cands = [chosen[1], chosen[2], chosen[3], chosen[4], chosen[5], chosen[0]] if len(chosen) >= 6 else chosen
    hook_captions = [
        "WAIT FOR #1 🤯",
        "NO WAY 😳",
        "WATCH THIS 👀",
        "HOW IS THIS REAL 💀",
        "NEXT LEVEL ⚡",
        "UNREAL 🏆"
    ]

    seen_labels = set()
    for idx, (rank, cand) in enumerate(zip([6, 5, 4, 3, 2, 1], play_order_cands)):
        dur = cand.get("actual_dur") or float(cand.get("sec", 15))
        # #1 is the payoff: hold it longest. Other ranks get a full 5s.
        item_len = 6.5 if rank == 1 else 5.0
        # Start later than the old 20% mark: most source clips open with an
        # intro, so 35% in is far more likely to land on the actual moment.
        start = min(max(1.0, dur * 0.35), max(0.0, dur - item_len - 0.2))
        end = round(start + item_len, 2)
        start = round(start, 2)

        # Label rule (2026-10-25 style bar: Polar / Xiro specific names,
        # RankZilla / MrPurifiedWater Level numbers as fallback):
        # drop generic leading words ("I built a ...") and extend the
        # label until it differs from earlier items, so ranks never
        # collapse to the same text (e.g. three "i built a"). If no
        # specific name survives, fall back to a Level number instead
        # of publishing a duplicate or generic label.
        clean_words = re.sub(r"[^\w\s]", "", cand.get("title", "")).strip().split()
        while clean_words and clean_words[0].lower() in GENERIC_LABEL_WORDS:
            clean_words.pop(0)
        label_words = clean_words[:3] if clean_words else []
        short_label = " ".join(label_words).lower()
        if len(short_label) > 22:
            short_label = short_label[:22].strip()
        label = f"{short_label} 🔥"[:26] if short_label else ""
        while label and label in seen_labels and len(label_words) < len(clean_words):
            label_words.append(clean_words[len(label_words)])
            short_label = " ".join(label_words).lower()
            if len(short_label) > 22:
                short_label = short_label[:22].strip()
            label = f"{short_label} 🔥"[:26]
        if not label or label in seen_labels:
            label = f"{level_label(rank)} 🔥"
        seen_labels.add(label)

        cap_text = hook_captions[idx % len(hook_captions)]
        captions = [[0.2, 2.2, cap_text]]
        if rank == 1:
            # Reference channels highlight #1 (yellow / crown), they do
            # not stamp a loud WINNER over it.
            captions.append([3.0, 5.0, "👑 #1"])

        items.append({
            "rank": rank,
            "src": cand["id"],
            "segments": [[start, end]],
            "speed": 1.0,
            "label": label,
            "captions": captions
        })

    t_name = topic.get("name", "Clips")
    words = t_name.split()
    first_word = words[0] if words else "Top"
    rest = " ".join(words[1:]) if len(words) > 1 else "Moments"

    return {
        "title": [["Ranking ", "white"], [f"{first_word} ", "yellow"], ["\n", ""], [f"{rest} ", "blue"], ["🏆", "white"]],
        "subtitle": "Wait for #1 🤯",
        "yt_title": f"Ranking {t_name} 🏆 #shorts"[:70],
        "items": items
    }


def run_agent_cloud(topic, proj, feedback=""):
    import validate
    cands_file = proj / "candidates.json"
    cands = json.loads(cands_file.read_text(encoding="utf-8")) if cands_file.exists() else []
    if not cands:
        log("cloud agent: no candidates found in candidates.json")
        return

    # 2026-10-25 run #10: "latin-1 codec can't encode '\ufeff' in
    # position 7" - position 7 is exactly where the API key starts
    # inside the "Bearer " header value, so the stored secret begins
    # with an invisible BOM character (pasted in with the key). HTTP
    # headers are latin-1, so urllib dies before the request is even
    # sent. Strip BOM / stray non-ASCII from the key here instead of
    # asking the user to re-paste the secret.
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip().replace("\ufeff", "").strip()
    spec = None
    if api_key:
        try:
            log("cloud agent: generating ranking spec via OpenRouter ...")
            cands_summary = []
            for c in cands[:10]:
                cands_summary.append({
                    "id": c["id"],
                    "title": c["title"],
                    "views": c.get("views", 0),
                    "sec": c.get("sec", 15)
                })
            t_name = topic.get("name", "Clips")
            words = t_name.split()
            first_word = words[0] if words else "Top"
            rest = " ".join(words[1:]) if len(words) > 1 else "Moments"
            schema_sample = {
                "title": [["Ranking ", "white"], [f"{first_word} ", "yellow"], ["\n", ""], [f"{rest} ", "blue"], ["🏆", "white"]],
                "subtitle": "Wait for #1 🤯",
                "yt_title": f"Ranking {t_name} 🏆 #shorts"[:70],
                "items": [
                    {
                        "rank": 6,
                        "src": cands[0]["id"] if cands else "cand_id",
                        "segments": [[2.0, 7.2]],
                        "speed": 1.0,
                        "label": "Moment Label 🔥",
                        "captions": [[0.2, 2.2, "WATCH THIS 👀"]]
                    }
                ]
            }
            system_msg = (
                "You are an expert viral YouTube Shorts ranking editor. "
                "Output ONLY a raw valid JSON object (no markdown, no conversation) matching this exact schema:\n"
                f"{json.dumps(schema_sample, indent=1)}\n\n"
                "CRITICAL RULES:\n"
                "1. Top-level keys MUST be: 'title', 'subtitle', 'yt_title', 'items'.\n"
                "2. 'title' MUST be a list of 5 [text, color] pairs as shown.\n"
                "3. 'items' MUST contain EXACTLY 6 ranking clips in play order: rank 6 first down to rank 1 last [6, 5, 4, 3, 2, 1].\n"
                "4. 'src' must be one of the candidate IDs provided. Each clip may be used only once.\n"
                "5. 'segments': [[start, end]] where length (end - start) is 5.0 to 6.5s (rank 1 must be 6.0 to 6.5s). Total video length 32 to 38s.\n"
                "6. 'label': unique descriptive name <= 26 chars ending with 🔥 (never generic like 'viral moment' or 'i built a').\n"
                "7. 'captions': [[0.2, 2.2, '<hook text <= 22 chars>']].\n"
                "8. 'yt_title' must end with '#shorts' (max 70 chars).\n"
                f"9. STRICT TOPIC RELEVANCE: All selected clips MUST genuinely show the topic ('{topic['name']}'). Do NOT select trick shots, warmups, reporter segments, or off-topic stunts."
            )
            user_msg = (
                f"Topic: {topic['name']} ({topic.get('category', 'general')})\n"
                f"Candidates:\n{json.dumps(cands_summary, indent=1)}\n"
            )
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps({
                    "model": "nvidia/nemotron-3.5-lightning:free",
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg}
                    ],
                    "temperature": 0.2,
                    "include_reasoning": False
                }).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/asirselvin446-commits/ranking-shorts-yt-automation"
                }
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                content = res_data["choices"][0]["message"]["content"].strip()
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                parsed = json.loads(content)

                # Unpack wrapped root if model returned {"project": ...} or similar
                if isinstance(parsed, dict):
                    if "project" in parsed and isinstance(parsed["project"], dict):
                        parsed = parsed["project"]
                    elif "ranking" in parsed and isinstance(parsed["ranking"], dict):
                        parsed = parsed["ranking"]

                # Auto-repair top-level keys if slightly off
                if isinstance(parsed, dict):
                    if "items" not in parsed and "clips" in parsed:
                        parsed["items"] = parsed["clips"]
                    if not isinstance(parsed.get("title"), list):
                        parsed["title"] = [["Ranking ", "white"], [f"{first_word} ", "yellow"], ["\n", ""], [f"{rest} ", "blue"], ["🏆", "white"]]
                    if not parsed.get("subtitle"):
                        parsed["subtitle"] = "Wait for #1 🤯"
                    if not parsed.get("yt_title") or not str(parsed.get("yt_title", "")).endswith("#shorts"):
                        parsed["yt_title"] = f"Ranking {t_name} 🏆 #shorts"[:70]

                (proj / "project.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
                errs, _ = validate.validate(proj / "project.json")
                if not errs:
                    spec = parsed
                    log("OpenRouter spec validated successfully")
                else:
                    log(f"OpenRouter spec failed validation ({errs}), switching to deterministic spec")
        except Exception as e:
            log(f"OpenRouter cloud agent failed: {e}")

    if not spec:
        log("generating deterministic ranking spec ...")
        spec = generate_deterministic_spec(topic, proj, cands)
        (proj / "project.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        errs, _ = validate.validate(proj / "project.json")
        if not errs:
            log("deterministic spec validated successfully")
        else:
            log(f"deterministic spec validation failed: {errs}")
            return


def run_agent(topic, proj, extra="", feedback="", allow_deterministic=True):
    spec_path = proj / "project.json"
    if HERMES.exists():
        prompt = (ROOT / "agent_prompt.md").read_text(encoding="utf-8")
        for k, v in {"{TOPIC}": topic["name"], "{CATEGORY}": topic.get("category", "general"), "{PROJ}": proj.as_posix(),
                     "{PY}": PY.as_posix(), "{ROOT}": ROOT.as_posix(), "{FEEDBACK}": feedback}.items():
            prompt = prompt.replace(k, v)
        if extra:
            prompt += "\n\n## Fix this\n" + extra
        pf = proj / "agent_prompt.md"
        pf.write_text(prompt, encoding="utf-8")
        log("running clip-editor agent via Hermes ...")
        r = sh([HERMES, "chat", "--provider", "copilot", "-m", "gpt-4o",
                "--query-file", pf, "-Q", "--yolo", "-s", "youtube-ranking-shorts",
                "--in", proj, "--run-budget", "2700", "--source", "yt-daily"], timeout=3300, cwd=proj)
        (proj / "agent_output.txt").write_text((r.stdout or "") + "\n--- stderr ---\n" + (r.stderr or ""), encoding="utf-8")
        log(f"agent exit={r.returncode}: {(r.stdout or '').strip()[-200:]}")

    if not spec_path.exists():
        log("Hermes not present or did not create spec, using cloud agent")
        run_agent_cloud(topic, proj, feedback=feedback)
    if not spec_path.exists() and allow_deterministic:
        log("generating deterministic ranking spec ...")
        _cands_file = proj / "candidates.json"
        _cands = json.loads(_cands_file.read_text(encoding="utf-8")) if _cands_file.exists() else []
        spec = generate_deterministic_spec(topic, proj, _cands)
        spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")


def build_meta(proj, spec, cands):
    by_id = {c["id"]: c for c in cands}
    credits = []
    ranked = []
    for it in sorted(spec["items"], key=lambda i: i["rank"]):
        c = by_id.get(it["src"], {})
        credits.append(f"#{it['rank']} {c.get('uploader') or c.get('channel', '')} – https://youtube.com/shorts/{it['src']}")
        # Description now names the actual ranked items (their on-screen
        # labels), so the caption matches the clips instead of repeating
        # only the generic topic title.
        ranked.append(f"#{it['rank']} {it.get('label', '')}")
    subject = "".join(t for t, _ in spec["title"] if t != "\n").strip()
    tags = ["ranking", "shorts", "viral", "funny", "best moments"] + [w.lower() for w in subject.split() if w.isalpha()][1:]
    return {
        "title": spec["yt_title"][:100],
        "description": f"Which one is your #1? Comment below 👇\n\n{subject}\n\nRanked:\n" + "\n".join(ranked)
                       + "\n\nCredits:\n" + "\n".join(credits)
                       + "\n\n#shorts #ranking #viral",
        "tags": list(dict.fromkeys(tags))[:15],
        "category_id": "24",
        "made_for_kids": False,
    }


def next_slots(now, n=2):
    """The next `n` publish slots (all daily times in schedule.json, in the audience time zone), as local times."""
    from zoneinfo import ZoneInfo
    sch = load_schedule()
    tz = ZoneInfo(sch.get("audience_tz", "America/New_York"))
    times = sch.get("slots") or [sch.get("local_time", "19:00")]
    base = now.astimezone(tz).date()
    out = []
    for off in range(0, 4):
        d = base + dt.timedelta(days=off)
        for t in times:
            hh, mm = map(int, t.split(":"))
            # Slot keys are IST wall-clock times on every runner (the
            # laptop wrote IST keys; a UTC GitHub runner must not invent
            # a second, shifted set of keys for the same slots).
            s = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(IST)
            if s > now:
                out.append(s)
    return sorted(out)[:n]


def build_one(slot, state, a):
    """Build, upload and schedule one Short for `slot`. Returns True on success."""
    key = slot.strftime("%Y-%m-%d %H:%M")
    log(f"=== building Short for slot {key} ===")
    import yt, analytics, validate, render
    yt.get_creds()  # fails fast if the YouTube login expired
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

    try:
        cands = (json.loads((proj / "candidates.json").read_text(encoding="utf-8"))
                 if (proj / "candidates.json").exists() else gather(topic, proj, state))
    except DownloadError as e:
        log(f"gather failed for '{topic['name']}': {e} - skipping topic for this cycle")
        state["used_topics"].append(topic["slug"])
        save_state(state)
        return False
    # TOPIC-FIT GATE (2026-10-25): the title is generated from the topic
    # name alone, so without this gate an off-topic clip set ships under
    # a title it does not match (Gaming Setups over secret rooms). If
    # fewer than 6 downloaded candidates actually show the topic, skip
    # the topic for this cycle instead of publishing a mismatched set.
    # This is a content skip, not a download failure, so the topic IS
    # marked used (it will return on a fresh cycle with new candidates).
    fitting, rejected, _kw = filter_topic_fit(cands, topic)
    if len(fitting) < 6:
        log(f"topic-fit gate: only {len(fitting)} of {len(cands)} candidates actually match "
            f"'{topic['name']}' - skipping topic; rejected titles: "
            + "; ".join(c.get("title", "")[:50] for c in rejected))
        state["used_topics"].append(topic["slug"]); save_state(state); return False
    if len(rejected):
        log(f"topic-fit gate: dropped {len(rejected)} off-topic candidates for '{topic['name']}'")
        cands = fitting
        # The agent reads candidates.json, so it must only see the
        # topic-fitting set, not the rejected off-topic clips.
        (proj / "candidates.json").write_text(json.dumps(cands, indent=1, ensure_ascii=False), encoding="utf-8")
    if len(cands) < 6:
        log("not enough clips for this topic - marking it used; next attempt tries another topic")
        state["used_topics"].append(topic["slug"]); save_state(state); return False

    spec_path = proj / "project.json"
    errs = ["project.json not written"]
    for attempt in range(2):
        if spec_path.exists():
            errs, _ = validate.validate(spec_path)
            if not errs:
                break
        run_agent(topic, proj, extra="" if attempt == 0 else "Previous attempt failed validation:\n- " + "\n- ".join(errs),
                  feedback=feedback, allow_deterministic=a.dry_run)
        errs, _ = validate.validate(spec_path) if spec_path.exists() else (["project.json not written"], None)
        if not errs:
            break
    if errs:
        log("agent could not produce a valid project: " + "; ".join(errs))
        notify("YT Shorts: failed", "Clip selection failed - will retry next time you log in.")
        return False

    final = render.render(spec_path)
    d = render.duration(final)
    if not 25 <= d <= 60:
        log(f"rendered length {d:.1f}s is off - aborting"); return False
    sh(["ffmpeg", "-v", "error", "-y", "-i", final, "-vf", "fps=1,scale=180:-1,tile=10x6", "-frames:v", "1",
        proj / "out" / "qa_sheet.jpg"])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    meta = build_meta(proj, spec, cands)
    (proj / "out" / "final.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    if a.dry_run:
        log(f"dry run - video at {final}"); return True

    now = dt.datetime.now().astimezone(IST)
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
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    pu = load_schedule().get("paused_until")
    if pu and not a.force and dt.datetime.now().astimezone() < dt.datetime.fromisoformat(pu):
        log(f"paused until {pu} - nothing to do"); return

    if not acquire_lock():
        print("another run is in progress"); return
    try:
        state = load_state()
        online = wait_for_internet(max_wait=300)
        # maintenance on every trigger: comment on Shorts that just went public, and move the
        # publish times to wherever the channel's real audience is
        if online:
            try:
                import analytics
                analytics.post_comments(state, log=log); save_state(state)
                import replies
                replies.run(state, log=log); save_state(state)
                analytics.retarget_schedule(log=log)
            except Exception as e:
                log(f"maintenance skipped: {str(e)[:150]}")
        # keep the next SLOTS_AHEAD slots filled (2 Shorts per day = always ~1 day queued)
        now = dt.datetime.now().astimezone(IST)
        upcoming = next_slots(now, SLOTS_AHEAD)
        todo = [s for s in upcoming
                if a.force or a.dry_run or s.strftime("%Y-%m-%d %H:%M") not in state["slots"]]
        if not todo:
            log("next slots already filled: " + ", ".join(s.strftime("%d %b %H:%M") for s in upcoming))
            return
        if not online and not wait_for_internet():
            log("no internet - giving up for now"); return
        built = 0
        for slot in (todo[:1] if a.dry_run else todo):
            for attempt in range(5):  # retry with the next topic if a topic has too few clips or download blocks
                if build_one(slot, state, a):
                    built += 1
                    break
        if a.dry_run:
            return
        if not built:
            # A run that builds nothing must FAIL the workflow. Silently
            # returning here is what made run #3 look like a Success in
            # GitHub while uploading nothing.
            log("ERROR: no Short built for any upcoming slot - failing the run so this is not reported as success")
            try:
                save_state(state)
            except Exception:
                pass
            sys.exit(1)

        # weekly trend research: add fresh topics from what's going viral right now
        topics_all = json.loads((ROOT / "topics.json").read_text(encoding="utf-8"))
        n_fresh = sum(1 for t in topics_all if t["slug"] not in state["used_topics"])
        last = state.get("last_discover")
        if n_fresh < 14 or not last or (dt.date.today() - dt.date.fromisoformat(last)).days >= 7:
            try:
                import discover
                rows = discover.research()
                added = discover.run_agent(n=16)
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
        raise
    except Exception:
        log("ERROR\n" + traceback.format_exc())
        notify("YT Shorts: error", "See yt-automation/logs for details.")
        try:
            save_state(state)
        except Exception:
            pass
        sys.exit(1)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
