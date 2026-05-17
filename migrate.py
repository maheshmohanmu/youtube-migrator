#!/usr/bin/env python3
"""
YouTube + YouTube Music Account Migrator
Migrates: subscriptions, liked videos, playlists, YT Music liked songs/albums/artists
"""
import os
import json
import time
import pickle
import logging
from pathlib import Path
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.errors
from google.auth.transport.requests import Request
from ytmusicapi import YTMusic
# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
CLIENT_SECRETS_FILE = "client_secrets.json"   # your OAuth credentials file
SCOPES_READ  = ["https://www.googleapis.com/auth/youtube.readonly"]
SCOPES_WRITE = ["https://www.googleapis.com/auth/youtube.force-ssl"]
API_SERVICE   = "youtube"
API_VERSION   = "v3"
EXPORT_DIR  = Path("yt_migration_export")
IMPORT_DELAY = 0.5   # seconds between write calls (avoid quota bursts)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
# ─────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────
def get_youtube_client(token_file: str, scopes: list, account_label: str):
    """Authenticate and return a YouTube API client, caching the token."""
    creds = None
    if os.path.exists(token_file):
        with open(token_file, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            log.info(f"Opening browser for [{account_label}] OAuth login...")
            flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, scopes
            )
            creds = flow.run_local_server(port=0)
        with open(token_file, "wb") as f:
            pickle.dump(creds, f)
    return googleapiclient.discovery.build(API_SERVICE, API_VERSION, credentials=creds)
def get_ytmusic_client(headers_file: str, account_label: str) -> YTMusic:
    """Get a ytmusicapi client. Run setup if headers file doesn't exist."""
    if not os.path.exists(headers_file):
        log.info(f"\n[{account_label}] ytmusicapi setup required.")
        print(f"\n--- YouTube Music Auth ({account_label}) ---")
        print("Open music.youtube.com in Chrome, press F12 → Network tab,")
        print("click any request → right-click → 'Copy as cURL', paste below:")
        YTMusic.setup(filepath=headers_file)
    return YTMusic(headers_file)
# ─────────────────────────────────────────
# YOUTUBE DATA API — EXPORT
# ─────────────────────────────────────────
def export_subscriptions(yt) -> list[dict]:
    """Export all subscribed channel IDs and titles."""
    log.info("Exporting subscriptions...")
    subs, token = [], None
    while True:
        resp = yt.subscriptions().list(
            part="snippet", mine=True, maxResults=50, pageToken=token
        ).execute()
        for item in resp.get("items", []):
            subs.append({
                "channelId": item["snippet"]["resourceId"]["channelId"],
                "title":     item["snippet"]["title"],
            })
        token = resp.get("nextPageToken")
        if not token:
            break
    log.info(f"  → {len(subs)} subscriptions")
    return subs
def export_liked_videos(yt) -> list[dict]:
    """Export all liked video IDs via the special 'likes' playlist."""
    log.info("Exporting liked videos...")
    # Get the 'likes' playlist ID for the authenticated user
    ch_resp = yt.channels().list(part="contentDetails", mine=True).execute()
    likes_pl = ch_resp["items"][0]["contentDetails"]["relatedPlaylists"]["likes"]
    videos, token = [], None
    while True:
        resp = yt.playlistItems().list(
            part="snippet", playlistId=likes_pl, maxResults=50, pageToken=token
        ).execute()
        for item in resp.get("items", []):
            vid = item["snippet"]["resourceId"]
            if vid.get("kind") == "youtube#video":
                videos.append({
                    "videoId": vid["videoId"],
                    "title":   item["snippet"]["title"],
                })
        token = resp.get("nextPageToken")
        if not token:
            break
    log.info(f"  → {len(videos)} liked videos")
    return videos
def export_playlists(yt) -> list[dict]:
    """Export all user-created playlists with their video IDs."""
    log.info("Exporting playlists...")
    playlists, token = [], None
    while True:
        resp = yt.playlists().list(
            part="snippet", mine=True, maxResults=50, pageToken=token
        ).execute()
        for pl in resp.get("items", []):
            pl_id    = pl["id"]
            pl_title = pl["snippet"]["title"]
            videos   = _get_playlist_items(yt, pl_id)
            playlists.append({"id": pl_id, "title": pl_title, "videos": videos})
            log.info(f"    playlist '{pl_title}' — {len(videos)} videos")
        token = resp.get("nextPageToken")
        if not token:
            break
    log.info(f"  → {len(playlists)} playlists")
    return playlists
def _get_playlist_items(yt, playlist_id: str) -> list[dict]:
    items, token = [], None
    while True:
        resp = yt.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50, pageToken=token
        ).execute()
        for item in resp.get("items", []):
            vid = item["snippet"]["resourceId"]
            if vid.get("kind") == "youtube#video":
                items.append({
                    "videoId": vid["videoId"],
                    "title":   item["snippet"].get("title", ""),
                })
        token = resp.get("nextPageToken")
        if not token:
            break
    return items
# ─────────────────────────────────────────
# QUOTA-AWARE IMPORT (with resume)
# ─────────────────────────────────────────
PROGRESS_FILE = EXPORT_DIR / "progress.json"

def load_progress() -> dict:
    """Load resume state from disk."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"subs_done": [], "videos_done": []}

def save_progress(progress: dict):
    """Persist resume state to disk."""
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)

def is_quota_error(e) -> bool:
    return isinstance(e, googleapiclient.errors.HttpError) and e.resp.status == 403 and "quotaExceeded" in str(e)

# ─────────────────────────────────────────
# YOUTUBE DATA API — IMPORT
# ─────────────────────────────────────────
def import_subscriptions(yt, subs: list[dict]):
    """Subscribe to all channels. Skips already-done, stops on quota."""
    progress = load_progress()
    done_ids = set(progress.get("subs_done", []))

    pending = [s for s in subs if s["channelId"] not in done_ids]
    log.info(f"Importing subscriptions: {len(pending)} remaining of {len(subs)} total")

    ok, skipped, failed = 0, 0, 0
    for sub in pending:
        try:
            yt.subscriptions().insert(
                part="snippet",
                body={"snippet": {"resourceId": {
                    "kind":      "youtube#channel",
                    "channelId": sub["channelId"],
                }}}
            ).execute()
            ok += 1
            done_ids.add(sub["channelId"])
            progress["subs_done"] = list(done_ids)
            save_progress(progress)
            log.info(f"  ✓ Subscribed: {sub['title']}")
        except googleapiclient.errors.HttpError as e:
            if e.resp.status == 409:   # already subscribed
                skipped += 1
                done_ids.add(sub["channelId"])
                progress["subs_done"] = list(done_ids)
                save_progress(progress)
            elif is_quota_error(e):
                log.warning(f"  ⚠ Quota exhausted after {ok} subscriptions. Resume tomorrow.")
                log.info(f"  → Subscriptions so far: {ok} added, {skipped} skipped, {failed} failed")
                log.info(f"  → Progress saved. Re-run tomorrow to continue from where you left off.")
                raise SystemExit(1)
            else:
                log.warning(f"  ✗ Failed ({sub['title']}): {e}")
                failed += 1
        time.sleep(IMPORT_DELAY)

    log.info(f"  → Subscriptions: {ok} added, {skipped} already existed, {failed} failed")

def import_liked_videos(yt, videos: list[dict]):
    """Like all videos. Skips already-done, stops on quota."""
    progress = load_progress()
    done_ids = set(progress.get("videos_done", []))

    pending = [v for v in videos if v["videoId"] not in done_ids]
    log.info(f"Importing liked videos: {len(pending)} remaining of {len(videos)} total")

    ok, failed = 0, 0
    for v in pending:
        try:
            yt.videos().rate(id=v["videoId"], rating="like").execute()
            ok += 1
            done_ids.add(v["videoId"])
            progress["videos_done"] = list(done_ids)
            save_progress(progress)
            log.info(f"  ✓ Liked: {v['title']}")
        except googleapiclient.errors.HttpError as e:
            if is_quota_error(e):
                log.warning(f"  ⚠ Quota exhausted after {ok} liked videos. Resume tomorrow.")
                log.info(f"  → Progress saved. Re-run tomorrow to continue from where you left off.")
                raise SystemExit(1)
            else:
                log.warning(f"  ✗ Failed ({v['title']}): {e}")
                failed += 1
        time.sleep(IMPORT_DELAY)

    log.info(f"  → Liked videos: {ok} liked, {failed} failed")
def import_playlists(yt, playlists: list[dict]):
    """Recreate all playlists on the destination account."""
    log.info(f"Importing {len(playlists)} playlists...")
    for pl in playlists:
        try:
            new_pl = yt.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {"title": pl["title"], "description": "(migrated)"},
                    "status":  {"privacyStatus": "private"},   # safe default
                }
            ).execute()
            new_id = new_pl["id"]
            log.info(f"  ✓ Created playlist '{pl['title']}' — adding {len(pl['videos'])} videos")
            for v in pl["videos"]:
                try:
                    yt.playlistItems().insert(
                        part="snippet",
                        body={"snippet": {
                            "playlistId": new_id,
                            "resourceId": {
                                "kind":    "youtube#video",
                                "videoId": v["videoId"],
                            },
                        }}
                    ).execute()
                    time.sleep(IMPORT_DELAY)
                except googleapiclient.errors.HttpError as e:
                    log.warning(f"    ✗ Couldn't add '{v['title']}': {e}")
        except googleapiclient.errors.HttpError as e:
            log.warning(f"  ✗ Failed to create playlist '{pl['title']}': {e}")
# ─────────────────────────────────────────
# YOUTUBE MUSIC — EXPORT
# ─────────────────────────────────────────
def export_ytmusic_liked_songs(ytm: YTMusic) -> list[dict]:
    """Export all liked songs from YouTube Music library."""
    log.info("Exporting YT Music liked songs...")
    try:
        songs = ytm.get_liked_songs(limit=5000)
        items = [
            {"videoId": t["videoId"], "title": t["title"],
             "artist": t["artists"][0]["name"] if t.get("artists") else ""}
            for t in songs.get("tracks", [])
        ]
    except KeyError:
        log.info("  → YT Music liked songs playlist not initialized (empty) — skipping")
        items = []
    log.info(f"  → {len(items)} liked songs")
    return items
def export_ytmusic_library_albums(ytm: YTMusic) -> list[dict]:
    """Export all saved albums."""
    log.info("Exporting YT Music library albums...")
    albums = ytm.get_library_albums(limit=1000)
    items = [
        {"browseId": a["browseId"], "title": a["title"],
         "artist":   a["artists"][0]["name"] if a.get("artists") else ""}
        for a in albums
    ]
    log.info(f"  → {len(items)} albums")
    return items
def export_ytmusic_library_artists(ytm: YTMusic) -> list[dict]:
    """Export all followed artists."""
    log.info("Exporting YT Music library artists...")
    artists = ytm.get_library_artists(limit=1000)
    items = [{"channelId": a["browseId"], "name": a["artist"]} for a in artists]
    log.info(f"  → {len(items)} artists")
    return items
def export_ytmusic_playlists(ytm: YTMusic) -> list[dict]:
    """Export all YT Music playlists with tracks."""
    log.info("Exporting YT Music playlists...")
    playlists = ytm.get_library_playlists(limit=1000)
    result = []
    for pl in playlists:
        if pl["playlistId"] == "LM":   # skip the built-in 'Liked Songs' playlist
            continue
        detail = ytm.get_playlist(pl["playlistId"], limit=5000)
        tracks = [
            {"videoId": t["videoId"], "title": t["title"]}
            for t in (detail.get("tracks") or [])
            if t.get("videoId")
        ]
        result.append({"playlistId": pl["playlistId"], "title": pl["title"], "tracks": tracks})
        log.info(f"    '{pl['title']}' — {len(tracks)} tracks")
    log.info(f"  → {len(result)} playlists")
    return result
# ─────────────────────────────────────────
# YOUTUBE MUSIC — IMPORT
# ─────────────────────────────────────────
def import_ytmusic_liked_songs(ytm: YTMusic, songs: list[dict]):
    """Like all songs in the destination YT Music account."""
    log.info(f"Importing {len(songs)} YT Music liked songs...")
    ok, failed = 0, 0
    video_ids = [s["videoId"] for s in songs if s.get("videoId")]
    # ytmusicapi can batch-rate; do in chunks of 200
    for i in range(0, len(video_ids), 200):
        chunk = video_ids[i:i+200]
        try:
            ytm.rate_songs(chunk, "LIKE")
            ok += len(chunk)
            log.info(f"  ✓ Liked songs {i+1}–{i+len(chunk)}")
        except Exception as e:
            log.warning(f"  ✗ Batch {i}–{i+len(chunk)} failed: {e}")
            failed += len(chunk)
        time.sleep(IMPORT_DELAY)
    log.info(f"  → YT Music songs: {ok} liked, {failed} failed")
def import_ytmusic_library_albums(ytm: YTMusic, albums: list[dict]):
    """Save all albums to the destination YT Music library."""
    log.info(f"Importing {len(albums)} YT Music albums...")
    ok, failed = 0, 0
    for a in albums:
        try:
            ytm.rate_playlist(a["browseId"], "LIKE")
            ok += 1
        except Exception as e:
            log.warning(f"  ✗ Album '{a['title']}': {e}")
            failed += 1
        time.sleep(IMPORT_DELAY)
    log.info(f"  → Albums: {ok} saved, {failed} failed")
def import_ytmusic_library_artists(ytm: YTMusic, artists: list[dict]):
    """Subscribe to all artists in the destination YT Music library."""
    log.info(f"Importing {len(artists)} YT Music artists...")
    ok, failed = 0, 0
    for a in artists:
        try:
            ytm.subscribe_artists([a["channelId"]])
            ok += 1
        except Exception as e:
            log.warning(f"  ✗ Artist '{a['name']}': {e}")
            failed += 1
        time.sleep(IMPORT_DELAY)
    log.info(f"  → Artists: {ok} subscribed, {failed} failed")
def import_ytmusic_playlists(ytm: YTMusic, playlists: list[dict]):
    """Recreate all YT Music playlists in the destination account."""
    log.info(f"Importing {len(playlists)} YT Music playlists...")
    for pl in playlists:
        try:
            new_id = ytm.create_playlist(pl["title"], description="(migrated)")
            video_ids = [t["videoId"] for t in pl["tracks"] if t.get("videoId")]
            if video_ids:
                ytm.add_playlist_items(new_id, video_ids)
            log.info(f"  ✓ Playlist '{pl['title']}' — {len(video_ids)} tracks")
        except Exception as e:
            log.warning(f"  ✗ Playlist '{pl['title']}': {e}")
        time.sleep(IMPORT_DELAY)
# ─────────────────────────────────────────
# SAVE / LOAD JSON SNAPSHOTS
# ─────────────────────────────────────────
def save(data, filename: str):
    EXPORT_DIR.mkdir(exist_ok=True)
    path = EXPORT_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"  Saved → {path}")
def load(filename: str):
    path = EXPORT_DIR / filename
    with open(path) as f:
        return json.load(f)
# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    EXPORT_DIR.mkdir(exist_ok=True)
    print("\n╔══════════════════════════════════════════╗")
    print("║  YouTube / YT Music Account Migrator     ║")
    print("╚══════════════════════════════════════════╝\n")
    print("Choose mode:")
    print("  1) Export from SOURCE account")
    print("  2) Import to DESTINATION account")
    print("  3) Both (export then import)\n")
    mode = input("Enter 1/2/3: ").strip()
    # ── EXPORT ─────────────────────────────
    if mode in ("1", "3"):
        print("\n─── EXPORT (sign in with SOURCE account) ───\n")
        # YouTube Data API — source (read-only)
        yt_src = get_youtube_client("token_source.pkl", SCOPES_READ, "SOURCE")
        subs    = export_subscriptions(yt_src)
        liked   = export_liked_videos(yt_src)
        plists  = export_playlists(yt_src)
        save(subs,   "subscriptions.json")
        save(liked,  "liked_videos.json")
        save(plists, "playlists.json")
        # YouTube Music — source
        ytm_src = get_ytmusic_client("ytmusic_source_headers.json", "SOURCE YT Music")
        ym_songs   = export_ytmusic_liked_songs(ytm_src)
        ym_albums  = export_ytmusic_library_albums(ytm_src)
        ym_artists = export_ytmusic_library_artists(ytm_src)
        ym_plists  = export_ytmusic_playlists(ytm_src)
        save(ym_songs,   "ytmusic_liked_songs.json")
        save(ym_albums,  "ytmusic_liked_albums.json")
        save(ym_artists, "ytmusic_liked_artists.json")
        save(ym_plists,  "ytmusic_playlists.json")
        print(f"\n✅ Export complete. Files saved in '{EXPORT_DIR}/'")
    # ── IMPORT ─────────────────────────────
    if mode in ("2", "3"):
        print("\n─── IMPORT (sign in with DESTINATION account) ───\n")
        # YouTube Data API — destination (write)
        yt_dst = get_youtube_client("token_dest.pkl", SCOPES_WRITE, "DESTINATION")
        import_subscriptions(yt_dst, load("subscriptions.json"))
        import_liked_videos(yt_dst,  load("liked_videos.json"))
        import_playlists(yt_dst,     load("playlists.json"))
        # YouTube Music — destination
        ytm_dst = get_ytmusic_client("ytmusic_dest_headers.json", "DESTINATION YT Music")
        import_ytmusic_liked_songs(ytm_dst,    load("ytmusic_liked_songs.json"))
        import_ytmusic_library_albums(ytm_dst, load("ytmusic_liked_albums.json"))
        import_ytmusic_library_artists(ytm_dst, load("ytmusic_liked_artists.json"))
        import_ytmusic_playlists(ytm_dst,      load("ytmusic_playlists.json"))
        print(f"\n✅ Import complete!")
    print("\nDone.")
if __name__ == "__main__":
    main()
