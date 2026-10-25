"""One-clip download diagnostic for the cloud yt-dlp block (2026-10-25).

Tries one known public clip with several yt-dlp strategies and logs
which (if any) succeeds. No upload, no state change, no secrets logged.
Usage in the runner: python test_download.py  (uses cookies.txt if present)

Strategies (in order):
  A. android client + cookies (if present)
  B. android client, no cookies
  C. web client + cookies, skipping webpage extraction
  D. tv_embedded client + cookies
  E. ios client + cookies
  F. web client + cookies (baseline, the one that gave 'page needs to be reloaded')
"""
import subprocess, sys, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEO_ID = "hnfI6qnTC6E"  # a Buzzer Beaters candidate that failed in runs #6-#8
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
COOKIES = ROOT / "cookies.txt"


def run(name, args):
    out = ROOT / f"test-{name}.mp4"
    if out.exists():
        out.unlink()
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-progress", "--no-playlist",
           "--js-runtimes", "node", "--socket-timeout", "30", "--retries", "1"] + args + [
           "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b", "--merge-output-format", "mp4",
           "-o", str(out), URL]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    ok = out.exists() and out.stat().st_size > 1000
    print(f"STRATEGY {name}: {'SUCCESS' if ok else 'FAIL'} size={out.stat().st_size if out.exists() else 0}", flush=True)
    if not ok and r.stderr:
        print(f"  err: {r.stderr.strip()[-1000:]}", flush=True)
    if out.exists():
        out.unlink()
    return ok


def main():
    print(f"cookies present: {COOKIES.exists()}", flush=True)
    ck = ["--cookies", str(COOKIES)] if COOKIES.exists() else []
    strategies = [
        ("A-android-cookies", ["--extractor-args", "youtube:player_client=android"] + ck),
        ("B-android-nocookies", ["--extractor-args", "youtube:player_client=android"]),
        ("C-web-skipwebpage-cookies", ["--extractor-args", "youtube:player_client=web;player_skip=webpage"] + ck),
        ("D-tv_embedded-cookies", ["--extractor-args", "youtube:player_client=tv_embedded"] + ck),
        ("E-ios-cookies", ["--extractor-args", "youtube:player_client=ios"] + ck),
        ("F-web-cookies", ["--extractor-args", "youtube:player_client=web"] + ck),
    ]
    for name, args in strategies:
        try:
            if run(name, args):
                print(f"WINNER: {name}", flush=True)
                return
        except Exception as e:
            print(f"STRATEGY {name}: EXCEPTION {e}", flush=True)
    print("NO STRATEGY SUCCEEDED", flush=True)


if __name__ == "__main__":
    main()
