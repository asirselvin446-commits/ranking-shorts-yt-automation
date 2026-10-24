You maintain the topic list for a YouTube Shorts "Ranking ..." channel (6 real viral clips ranked 6 -> 1, 40-50 s).
Goal of every Short: viewers watch to the end. Any niche is fine (technology, physique/fitness, cars, animals, sports, science, food, places, funny, talent, extreme sports, engineering ...).

1. Read `C:/Users/asus/yt-automation/research/trending.md`: ranking Shorts that went viral in the last 30 days, with views.
2. Read `C:/Users/asus/yt-automation/topics.json` (existing topics; do not duplicate or near-duplicate them).
3. Append 16 NEW topics to `C:/Users/asus/yt-automation/topics.json` (keep every existing entry unchanged, keep valid JSON). Prefer subjects that are trending or proven in the research, spread across several different niches.
   Each topic must be filmable with plenty of real, visual, viral short clips on YouTube (not talking, not reviews, not CGI) where the moment is understood in 1 second.
   Entry format: {"slug": "lowercase-hyphen-slug", "category": "<one-word niche>", "name": "Most Insane X" (the subject as it appears after "Ranking "), "queries": ["<search 1> #shorts", "<search 2>", "<search 3>"]}
   Queries must find single-moment clips, not compilations.
4. Check with: `C:/Users/asus/yt-automation/.venv/Scripts/python.exe -c "import sys; sys.path.insert(0, r'C:/Users/asus/yt-automation'); import discover; print(discover.check_topics(r'C:/Users/asus/yt-automation/topics.json'))"`
5. Reply with just DONE.
