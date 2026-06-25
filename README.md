# yt — local YouTube downloader

Tiny local web app: paste a link, pick quality, watch a live progress bar.
Downloads at **original quality and fps** (yt-dlp grabs best video+audio streams,
ffmpeg remuxes them with no re-encode).

## Setup (once)

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Requires `ffmpeg` on PATH (you have it via Homebrew).

## Run

```bash
./.venv/bin/python app.py
```

Open http://127.0.0.1:5001 and paste a link.

Files save to `~/Downloads/yt`. Override:

```bash
YT_DOWNLOAD_DIR=~/Movies YT_PORT=8080 ./.venv/bin/python app.py
```

## Maintenance

If YouTube changes break downloads, update yt-dlp:

```bash
./.venv/bin/pip install -U yt-dlp
```

## Notes

- For personal/archival use. Downloading may conflict with YouTube's ToS.
- Default "Best compatible" = H.264/AAC mp4, plays natively in QuickTime/Apple. "Max quality" reaches 4K but uses AV1/VP9 — play those in VLC. Both remux (no re-encode), so fps is preserved.
- Playlists: only the single video is downloaded (`noplaylist`).
