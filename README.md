# 🧲 Yoink

**A tiny, local, open-source YouTube downloader.** Search, hover to preview, and download in full quality — straight from your own machine. No ads, no limits, no servers watching.

🌐 **[Landing page](https://yt-ashen-phi.vercel.app)** · ⭐ [Star this repo](https://github.com/robinandrejohansen/yoink) · MIT licensed

---

## Why local, not a website?

A hosted YouTube downloader gets IP-blocked by YouTube within minutes and is a legal piñata — that's why every "online downloader" is broken or buried in ads. Running on your own machine means it **actually works**, it's **private**, and it's **yours**.

## Features

- 🎞️ **Full quality & fps** — up to 4K, 60fps preserved. Defaults to H.264/AAC so files play in QuickTime/iOS anywhere; a max-quality AV1 mode for the purists.
- 🔎 **Search + Shorts filter** — find videos in-app and sort Shorts from long-form in one click.
- 👁️ **Hover to preview** — muted autoplay preview on hover, so you grab the right video.
- ⚡ **Live progress** — real-time download + merge progress bar.
- 🔒 **100% local & private** — runs on your machine; nothing leaves it.

## Setup (once)

```bash
git clone https://github.com/robinandrejohansen/yoink
cd yoink
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Requires `ffmpeg` (`brew install ffmpeg` on macOS).

## Run

```bash
./.venv/bin/python app.py
```

Open <http://127.0.0.1:5001>. Files save to `~/Downloads/yt` (override with `YT_DOWNLOAD_DIR`).

## Maintenance

If YouTube changes break downloads, update yt-dlp:

```bash
./.venv/bin/pip install -U yt-dlp
```

## Notes

- For personal/archival use. Downloading may conflict with YouTube's ToS — only download content you have the right to.
- **"Best compatible"** = H.264/AAC mp4 (plays everywhere). **"Max quality"** reaches 4K but uses AV1/VP9 — play those in [VLC](https://www.videolan.org/). Both remux (no re-encode), so fps is preserved.
- Repo layout: `app.py` is the local tool; `site/` is the landing page (deployed to Vercel). The downloader runs **locally only** — never as a hosted service (see "Why local?").

## License

MIT © Robin Johansen
