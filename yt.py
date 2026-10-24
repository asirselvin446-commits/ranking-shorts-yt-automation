"""YouTube automation CLI.

Commands:
  python yt.py auth                      # one-time browser sign-in, saves token.json
  python yt.py whoami                    # show the connected channel
  python yt.py upload VIDEO [options]    # upload one video
  python yt.py watch                     # upload everything in ready/ (sidecar .json = metadata)
  python yt.py stats [--n 10]            # recent uploads with views/likes/comments

Sidecar metadata (ready/myvideo.json next to ready/myvideo.mp4), all fields optional:
  {
    "title": "...", "description": "...", "tags": ["a","b"],
    "category_id": "22", "privacy": "private|unlisted|public",
    "publish_at": "2026-10-20T18:30:00+05:30",   # schedules (forces private until then)
    "thumbnail": "myvideo.jpg", "playlist_id": "PL...",
    "made_for_kids": false
  }
"""
import argparse
import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
SECRET = ROOT / "client_secret.json"
TOKEN = ROOT / "token.json"
READY, UPLOADED, FAILED = ROOT / "ready", ROOT / "uploaded", ROOT / "failed"
LOG = ROOT / "upload_log.jsonl"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",   # playlists, comments, metadata edits
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
THUMB_EXT = [".jpg", ".jpeg", ".png"]


def get_creds(interactive=False):
    creds = None
    if TOKEN.exists():
        raw = TOKEN.read_text(encoding="utf-8-sig")
        TOKEN.write_text(raw, encoding="utf-8")
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
            return creds
        except Exception as e:
            print(f"Token refresh failed ({e}); re-auth needed.", file=sys.stderr)
    if not interactive:
        sys.exit("Not authorized. Run: python yt.py auth")
    flow = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline", open_browser=True)
    TOKEN.write_text(creds.to_json())
    return creds


def yt():
    return build("youtube", "v3", credentials=get_creds(), cache_discovery=False)


def whoami():
    r = yt().channels().list(part="snippet,statistics,status", mine=True).execute()
    for c in r.get("items", []):
        s = c["statistics"]
        print(json.dumps({
            "channel_id": c["id"], "title": c["snippet"]["title"],
            "subscribers": s.get("subscriberCount"), "videos": s.get("videoCount"),
            "views": s.get("viewCount"),
        }, indent=2))
    if not r.get("items"):
        print("Signed in, but this Google account has no YouTube channel.")


def upload(video, meta):
    video = Path(video)
    client = yt()
    privacy = meta.get("privacy", "private")
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": bool(meta.get("made_for_kids", False))}
    if meta.get("publish_at"):
        status["privacyStatus"] = "private"  # required for scheduled publish
        status["publishAt"] = dt.datetime.fromisoformat(meta["publish_at"]).astimezone(dt.timezone.utc).isoformat()
    body = {
        "snippet": {
            "title": meta.get("title", video.stem)[:100],
            "description": meta.get("description", "")[:5000],
            "tags": meta.get("tags", []),
            "categoryId": str(meta.get("category_id", "22")),
            "defaultLanguage": meta.get("language", "en"),
            "defaultAudioLanguage": meta.get("language", "en"),
        },
        "status": status,
    }
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
    req = client.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, retries = None, 0
    print(f"Uploading {video.name} ...")
    while resp is None:
        try:
            st, resp = req.next_chunk()
            if st:
                print(f"  {int(st.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 8:
                retries += 1; time.sleep(2 ** retries); continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if retries < 8:
                retries += 1; time.sleep(2 ** retries); continue
            raise
    vid = resp["id"]
    print(f"  done: https://youtu.be/{vid}  (status: {resp['status'].get('privacyStatus')}"
          f"{', uploadStatus: ' + resp['status'].get('uploadStatus', '') })")

    thumb = meta.get("thumbnail")
    thumb = (video.parent / thumb) if thumb else next((video.with_suffix(x) for x in THUMB_EXT if video.with_suffix(x).exists()), None)
    if thumb and Path(thumb).exists():
        try:
            client.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
            print(f"  thumbnail set: {Path(thumb).name}")
        except HttpError as e:
            print(f"  thumbnail FAILED (channel phone-verified?): {e}")
    if meta.get("playlist_id"):
        client.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": meta["playlist_id"], "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute()
        print(f"  added to playlist {meta['playlist_id']}")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": dt.datetime.now().isoformat(), "file": video.name, "video_id": vid,
                            "title": body["snippet"]["title"], "status": status}) + "\n")
    return vid


def watch():
    vids = sorted(p for p in READY.iterdir() if p.suffix.lower() in VIDEO_EXT)
    if not vids:
        print("ready/ is empty."); return
    for v in vids:
        side = v.with_suffix(".json")
        meta = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        companions = [side] + [v.with_suffix(x) for x in THUMB_EXT]
        try:
            upload(v, meta)
            dest = UPLOADED
        except Exception as e:
            print(f"  FAILED {v.name}: {e}")
            dest = FAILED
        for p in [v] + companions:
            if p.exists():
                shutil.move(str(p), str(dest / p.name))


def stats(n):
    client = yt()
    ch = client.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = client.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=n).execute()["items"]
    ids = [i["contentDetails"]["videoId"] for i in items]
    if not ids:
        print("No uploads yet."); return
    vs = client.videos().list(part="snippet,statistics,status", id=",".join(ids)).execute()["items"]
    missing = len(ids) - len(vs)
    if missing:
        print(f"({missing} deleted/unavailable video(s) skipped)")
    if not vs:
        print("No live uploads yet.")
    for v in vs:
        s = v["statistics"]
        print(f"{v['snippet']['publishedAt'][:10]}  {v['status']['privacyStatus']:8}  "
              f"views={s.get('viewCount', 0):>7}  likes={s.get('likeCount', 0):>5}  "
              f"comments={s.get('commentCount', 0):>4}  {v['snippet']['title']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth"); sub.add_parser("whoami"); sub.add_parser("watch")
    s = sub.add_parser("stats"); s.add_argument("--n", type=int, default=10)
    u = sub.add_parser("upload")
    u.add_argument("video"); u.add_argument("--title"); u.add_argument("--description", default="")
    u.add_argument("--tags", default=""); u.add_argument("--privacy", default="private")
    u.add_argument("--publish-at"); u.add_argument("--thumbnail"); u.add_argument("--playlist-id")
    a = ap.parse_args()
    if a.cmd == "auth":
        get_creds(interactive=True); print("Authorized. token.json saved."); whoami()
    elif a.cmd == "whoami":
        whoami()
    elif a.cmd == "watch":
        watch()
    elif a.cmd == "stats":
        stats(a.n)
    elif a.cmd == "upload":
        meta = {k: v for k, v in {
            "title": a.title, "description": a.description,
            "tags": [t.strip() for t in a.tags.split(",") if t.strip()],
            "privacy": a.privacy, "publish_at": a.publish_at,
            "thumbnail": a.thumbnail and str(Path(a.thumbnail).resolve()),
            "playlist_id": a.playlist_id}.items() if v}
        upload(a.video, meta)
