# YouTube & YouTube Music Account Migrator

Migrate subscriptions, liked videos, playlists, and YouTube Music library (liked songs, artists, playlists) from one Google account to another.

---

## What gets migrated

| Data | Supported |
|---|---|
| YouTube subscriptions | ✅ |
| YouTube liked videos | ✅ |
| YouTube playlists | ✅ |
| YT Music liked songs | ✅ |
| YT Music followed artists | ✅ |
| YT Music liked albums | ✅ |
| YT Music playlists | ✅ |
| Watch history | ❌ (not accessible via any API) |
| Watch Later | ❌ (not accessible via API) |

---

## Prerequisites

### 1. Python 3.10+
```bash
python3 --version
```

### 2. Create a virtual environment and install dependencies
```bash
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows

pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client ytmusicapi
```

> **Always activate the venv before running the script** (`source venv/bin/activate`).  
> Run `deactivate` when you're done.

---

## Setup

### Step 1 — Google Cloud OAuth credentials (one-time)

This gives the script permission to talk to the YouTube API. Use **any** Google account — it's just a developer credential, not tied to your source or destination account.

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. **New Project** → name it anything (e.g. `youtube-migrator`) → **Create**
3. **APIs & Services** → **Library** → search `YouTube Data API v3` → **Enable**
4. **APIs & Services** → **OAuth consent screen**
   - User type: **External** → Create
   - App name: `youtube-migrator`, fill in your email → **Save and Continue** × 3
   - Scroll to **Test users** → **Add Users** → add both your old and new Gmail addresses → **Save**
5. **APIs & Services** → **Credentials** → **+ Create Credentials** → **OAuth client ID**
   - Application type: **Desktop app** → **Create**
   - Click **Download JSON** → save as `client_secrets.json` in this folder

### Step 2 — YouTube Music authentication

YT Music uses browser session cookies. You need to capture these **separately for each account** using an **Incognito window** (critical — regular windows mix multiple accounts and break auth).

#### For your SOURCE (old) account:

1. Open Chrome → **New Incognito Window** (`Cmd+Shift+N`)
2. Go to [https://music.youtube.com](https://music.youtube.com) → log in with your **old account**
3. Press `F12` → **Network** tab → filter by `browse`
4. Click any `browse` POST request → **Headers** tab → **Request Headers**
5. Right-click the request → **Copy** → **Copy as cURL (bash)**

Now create the headers file using the copied cURL:

```bash
# Option A — let ytmusicapi parse the cURL for you (easier):
python3 -c "from ytmusicapi import YTMusic; YTMusic.setup(filepath='ytmusic_source_headers.json')"
# Paste the cURL output → press Ctrl+D

# Option B — create the JSON file manually:
# Copy ytmusic_headers.example.json → ytmusic_source_headers.json
# Fill in the real values from your browser headers
# See ytmusic_headers.example.json for the required fields
# IMPORTANT: Remove these headers if present (they break auth):
#   content-encoding, content-length, content-type, accept-encoding
```

#### For your DESTINATION (new) account:

Repeat the exact same steps in a **new Incognito window**, log in with your **new account**, save as `ytmusic_dest_headers.json`.

#### Verify both files work:
```bash
python3 -c "
from ytmusicapi import YTMusic
src = YTMusic('ytmusic_source_headers.json')
dst = YTMusic('ytmusic_dest_headers.json')
songs = src.get_liked_songs(limit=3)
print('Source liked songs:', [t['title'] for t in songs.get('tracks', [])])
pls = dst.get_library_playlists(limit=3)
print('Dest playlists:', [p['title'] for p in pls])
"
```

---

## Running the migration

```bash
source venv/bin/activate   # if not already active
./migrate.py
```

Choose a mode:
```
1) Export from SOURCE account   ← reads your old account, saves JSON files
2) Import to DESTINATION account ← writes to your new account from saved JSON files  
3) Both (export then import)    ← does everything in one go
```

**Recommended flow:**
1. Run with **option 1** first — exports everything to `yt_migration_export/`
2. Verify the JSON files look correct
3. Run with **option 2** to import

---

## YouTube API quota

The YouTube Data API has a **10,000 unit daily quota**. Each write operation (subscribe, like, add to playlist) costs **50 units** → ~200 operations per day.

| If you have | Days needed |
|---|---|
| ≤200 subscriptions + ≤200 liked videos | 1 day |
| 270 subscriptions + 58 liked videos | 2 days |
| 500 subscriptions | 3 days |

**The script auto-resumes.** It saves progress to `yt_migration_export/progress.json` after every successful write. If the quota runs out mid-run, just re-run the next day — already-completed items are skipped automatically.

Quota resets daily at **midnight Pacific Time** (≈ 09:00 Berlin / 08:00 London).

---

## File structure

```
youtube-migration/
├── migrate.py                        ← the migration script
├── client_secrets.json               ← Google Cloud OAuth (download from Cloud Console)
├── ytmusic_source_headers.json       ← YOUR SOURCE account YT Music cookies  ← keep private
├── ytmusic_dest_headers.json         ← YOUR DEST account YT Music cookies    ← keep private
├── ytmusic_headers.example.json      ← template showing required JSON fields
├── token_source.pkl                  ← cached YouTube OAuth token (auto-created)
├── token_dest.pkl                    ← cached YouTube OAuth token (auto-created)
├── .gitignore                        ← keeps all secrets out of git
└── yt_migration_export/
    ├── subscriptions.json            ← exported subscriptions
    ├── liked_videos.json             ← exported liked videos
    ├── playlists.json                ← exported playlists
    ├── ytmusic_liked_songs.json      ← exported YT Music liked songs
    ├── ytmusic_liked_albums.json     ← exported YT Music liked albums
    ├── ytmusic_liked_artists.json    ← exported YT Music followed artists
    ├── ytmusic_playlists.json        ← exported YT Music playlists
    └── progress.json                 ← auto-created; tracks import progress for resume
```

> ⚠️ `client_secrets.json`, `token_*.pkl`, `ytmusic_*_headers.json`, and `yt_migration_export/` are all in `.gitignore` — they will **never be committed** to git.

---

## Common issues

### `JSONDecodeError: Expecting value` when loading headers file
The file is empty or contains non-JSON text. Re-create it — copy only the JSON block, nothing else.

### `KeyError: twoColumnBrowseResultsRenderer` / "Sign in to listen to your liked tracks"
Your headers file is authenticating as the wrong account (shared cookies from a multi-account Chrome session). Fix: capture headers from an **Incognito window** with only one account signed in.

### `403 quotaExceeded`
Daily quota exhausted. Re-run tomorrow — progress is saved automatically.

### `409 Conflict` on subscriptions
Already subscribed on destination account — this is normal and expected on re-runs. The script skips these silently.

### YT Music library shows empty
See the `twoColumnBrowseResultsRenderer` fix above — same root cause (wrong account cookies).

---

## Security notes

- **Never commit** `client_secrets.json`, `token_*.pkl`, or `ytmusic_*_headers.json` — they grant full access to your YouTube accounts
- The `.gitignore` blocks all of these automatically
- YT Music header cookies expire after a few weeks — if auth stops working, re-capture headers from the browser
- YouTube OAuth tokens auto-refresh via `token_*.pkl` — these last much longer

---

## License

MIT
