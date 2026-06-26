"""
Local YouTube downloader — paste a link, pick quality, watch live progress.

Run:  ./.venv/bin/python app.py   then open http://127.0.0.1:5001
Saves to ~/Downloads/yt by default (override with YT_DOWNLOAD_DIR).

Quality is preserved exactly: yt-dlp pulls the best video-only + audio-only
streams and ffmpeg *remuxes* (never re-encodes) them, so resolution, bitrate
and fps (incl. 60fps) match the source bit-for-bit.
"""
import concurrent.futures
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from urllib.parse import quote_plus

from flask import Flask, Response, jsonify, render_template_string, request

# macOS python.org builds ship without a CA bundle, so TLS verification fails.
# Point Python's SSL at certifi's bundle before yt-dlp opens any connection.
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import yt_dlp

DOWNLOAD_DIR = os.path.expanduser(os.environ.get("YT_DOWNLOAD_DIR", "~/Downloads/yt"))
PORT = int(os.environ.get("YT_PORT", "5001"))


def _resolve_ffmpeg():
    """System ffmpeg if available, else the binary bundled with imageio-ffmpeg —
    so a `pipx install` works with no manual ffmpeg setup."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = _resolve_ffmpeg()

# Prefer H.264 (avc1) video + AAC (mp4a) audio so the merged .mp4 plays natively in
# QuickTime / Apple players. YouTube only offers H.264 up to 1080p; 1440p/4K are VP9/AV1
# only (the "max" option) and need a player like VLC. Fall back gracefully per tier.
_COMPAT = (
    "bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
    "bestvideo[height<={h}]+bestaudio/best[height<={h}]"
)
QUALITY_FORMATS = {
    "compat": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1]/best",
    "1080": _COMPAT.format(h=1080),
    "720": _COMPAT.format(h=720),
    "480": _COMPAT.format(h=480),
    "max": "bestvideo+bestaudio/best",
    "audio": "bestaudio[ext=m4a]/bestaudio",
}

# Duration heuristic: search hits at or under this are treated as Shorts. Flat search
# exposes no aspect ratio, and probing /shorts/<id> would add ~15 requests per query.
SHORT_MAX_SECONDS = 60

# YouTube search filter "Duration: under 4 minutes". Plain ytsearch returns almost no
# Shorts, so a second, parallel query with this filter is what actually surfaces them.
YT_UNDER_4MIN_SP = "EgIYAQ%253D%253D"

app = Flask(__name__)
jobs = {}  # job_id -> queue.Queue of progress dicts


def run_download(url, quality, q):
    """Download in a worker thread, pushing progress dicts into the job queue."""
    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            idict = d.get("info_dict") or {}
            phase = "audio" if idict.get("vcodec") in (None, "none") else "video"
            q.put({
                "status": "downloading",
                "phase": phase,
                "pct": round(done / total * 100, 1) if total else None,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
            })
        elif d["status"] == "finished":
            q.put({"status": "processing"})

    def pp_hook(d):
        if d.get("status") == "started" and d.get("postprocessor") == "Merger":
            q.put({"status": "merging"})

    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["compat"])
    ydl_opts = {
        "format": fmt,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s"),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if FFMPEG:
        ydl_opts["ffmpeg_location"] = FFMPEG
    if quality != "audio":
        ydl_opts["merge_output_format"] = "mp4"

    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            req = (info.get("requested_downloads") or [{}])[0]
            path = req.get("filepath") or ydl.prepare_filename(info)
        q.put({
            "status": "done",
            "file": os.path.basename(path),
            "path": path,
            "title": info.get("title"),
            "height": info.get("height"),
            "fps": info.get("fps"),
        })
    except Exception as e:  # noqa: BLE001 - surface any yt-dlp/ffmpeg error to UI
        q.put({"status": "error", "error": str(e)})


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    quality = data.get("quality", "best")
    job_id = uuid.uuid4().hex
    q = queue.Queue()
    jobs[job_id] = q
    threading.Thread(target=run_download, args=(url, quality, q), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    q = jobs.get(job_id)
    if q is None:
        return jsonify({"error": "unknown job"}), 404

    def stream():
        while True:
            try:
                msg = q.get(timeout=20)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("status") in ("done", "error"):
                jobs.pop(job_id, None)
                break

    return Response(stream(), mimetype="text/event-stream")


def _flat_search(target, limit):
    """Flat-extract a search target (ytsearch or a results URL); [] on any failure."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        return [e for e in (info.get("entries") or []) if e and e.get("id")]
    except Exception as e:  # noqa: BLE001
        print(f"search failed for {target!r}: {e}", file=sys.stderr)
        return []


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    qp = quote_plus(q)
    # Run both queries in parallel: normal videos + a Shorts-surfacing filtered query.
    targets = [
        (f"ytsearch12:{q}", 12),
        (f"https://www.youtube.com/results?search_query={qp}&sp={YT_UNDER_4MIN_SP}", 15),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        entry_lists = list(ex.map(lambda t: _flat_search(*t), targets))

    seen, results = set(), []
    for entries in entry_lists:
        for e in entries:
            vid = e.get("id")
            if vid in seen:
                continue
            seen.add(vid)
            dur = e.get("duration")
            results.append(
                {
                    "id": vid,
                    "title": e.get("title"),
                    "uploader": e.get("uploader") or e.get("channel"),
                    "duration": dur,
                    "is_short": dur is not None and dur <= SHORT_MAX_SECONDS,
                }
            )
    return jsonify({"results": results[:24]})


@app.route("/reveal", methods=["POST"])
def reveal():
    """macOS: reveal the finished file (or the folder) in Finder."""
    path = (request.get_json(force=True).get("path") or "").strip()
    target = path if path and os.path.exists(path) else DOWNLOAD_DIR
    if sys.platform == "darwin":
        args = ["open", "-R", target] if path else ["open", target]
        subprocess.Popen(args)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template_string(PAGE, dir=DOWNLOAD_DIR, has_ffmpeg=bool(FFMPEG))


PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yt — local downloader</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#0d0f12; color:#e8eaed; display:flex; min-height:100vh;
         justify-content:center; padding:24px; }
  .card { width:100%; max-width:600px; background:#16191e; border:1px solid #262a31;
          border-radius:16px; padding:28px; height:fit-content; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:#8b929e; font-size:13px; margin-bottom:18px; }
  .sub code { background:#0d0f12; padding:1px 6px; border-radius:5px; color:#aeb4bf; }
  label { display:block; font-size:12px; color:#8b929e; margin:14px 0 6px; text-transform:uppercase; letter-spacing:.04em; }
  input, select { width:100%; padding:11px 13px; background:#0d0f12; color:#e8eaed;
                  border:1px solid #2b3038; border-radius:10px; font-size:14px; }
  input:focus, select:focus { outline:none; border-color:#4c8bf5; }
  .inline { display:flex; gap:10px; }
  .inline input { flex:1; }
  button { padding:11px 16px; background:#4c8bf5; color:#fff; border:0; border-radius:10px;
           font-size:14px; font-weight:600; cursor:pointer; white-space:nowrap; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button.full { width:100%; margin-top:16px; padding:12px; font-size:15px; }
  button.ghost { background:#262a31; }
  .results { margin-top:14px; display:flex; flex-direction:column; gap:8px;
             max-height:360px; overflow-y:auto; }
  .result { display:flex; gap:11px; padding:8px; background:#0d0f12; border:1px solid #20242b;
            border-radius:10px; align-items:center; }
  .result .thumb { position:relative; width:128px; height:72px; flex:0 0 auto; border-radius:6px; overflow:hidden; background:#000; }
  .result .thumb img, .result .thumb iframe { width:100%; height:100%; border:0; display:block; object-fit:cover; }
  .result .thumb .dur { position:absolute; bottom:4px; right:4px; background:rgba(0,0,0,.82); color:#fff;
                        font-size:11px; font-weight:600; padding:1px 5px; border-radius:4px; pointer-events:none; }
  .result .thumb .dur.short { background:#e0143c; }
  .filter { display:flex; gap:6px; margin-top:14px; }
  .seg { background:#0d0f12; color:#aeb4bf; border:1px solid #2b3038; padding:6px 13px;
         font-size:13px; font-weight:500; border-radius:8px; cursor:pointer; }
  .seg.active { background:#4c8bf5; color:#fff; border-color:#4c8bf5; }
  .result .meta { flex:1; min-width:0; }
  .result .t { font-size:13px; font-weight:600; line-height:1.3; display:-webkit-box;
               -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .result .m { font-size:12px; color:#8b929e; margin-top:3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .result button { padding:8px 12px; font-size:13px; flex:0 0 auto; }
  .divider { display:flex; align-items:center; gap:12px; color:#5a616b; font-size:12px; margin:22px 0 0; }
  .divider::before, .divider::after { content:""; flex:1; height:1px; background:#262a31; }
  .bar { margin-top:18px; height:8px; background:#0d0f12; border-radius:99px; overflow:hidden; display:none; }
  .bar > i { display:block; height:100%; width:0; background:#4c8bf5; transition:width .2s; }
  .bar.indet > i { width:35% !important; animation:slide 1.1s ease-in-out infinite; }
  @keyframes slide { 0%{margin-left:-35%} 100%{margin-left:100%} }
  .status { margin-top:12px; font-size:13px; color:#aeb4bf; min-height:18px; }
  .status.err { color:#ff6b6b; word-break:break-word; }
  .status.ok { color:#5fd28a; }
  .reveal { margin-top:10px; display:none; }
  .warn { margin-top:14px; font-size:12px; color:#f5b14c; }
  .hint { color:#5a616b; font-size:12px; margin-top:8px; }
</style>
</head>
<body>
  <div class="card">
    <h1>YouTube downloader</h1>
    <div class="sub">Saves to <code>{{ dir }}</code> · original quality &amp; fps preserved</div>
    {% if not has_ffmpeg %}<div class="warn">⚠ ffmpeg not found — high-res merging will fail.</div>{% endif %}

    <label>Quality</label>
    <select id="quality">
      <option value="compat">Best compatible — H.264, plays everywhere</option>
      <option value="1080">1080p (H.264)</option>
      <option value="720">720p (H.264)</option>
      <option value="480">480p (H.264)</option>
      <option value="max">Max quality — up to 4K (AV1/VP9, needs VLC)</option>
      <option value="audio">Audio only (m4a)</option>
    </select>

    <label>Search YouTube</label>
    <div class="inline">
      <input id="q" type="text" placeholder="Search for a video…" autofocus>
      <button id="searchBtn">Search</button>
    </div>
    <div class="filter" id="filter" style="display:none">
      <button class="seg active" data-f="all">All</button>
      <button class="seg" data-f="video">Videos</button>
      <button class="seg" data-f="short">Shorts</button>
    </div>
    <div class="results" id="results"></div>

    <div class="divider">or paste a link</div>
    <div class="inline" style="margin-top:12px">
      <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=…">
      <button id="go">Download</button>
    </div>

    <div class="bar" id="bar"><i></i></div>
    <div class="status" id="status"></div>
    <button class="ghost reveal" id="reveal">Reveal in Finder</button>
  </div>

<script>
const $ = id => document.getElementById(id);
const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtSpeed = b => b ? (b/1048576).toFixed(1) + " MB/s" : "";
const fmtEta = s => s == null ? "" : "· " + Math.floor(s/60) + ":" + String(s%60).padStart(2,"0") + " left";
const fmtDur = s => { if (s == null) return "LIVE"; s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h ? h + ":" + String(m).padStart(2,"0") : m) + ":" + String(x).padStart(2,"0"); };
let lastPath = null, busy = false;

$("reveal").onclick = () =>
  fetch("/reveal", {method:"POST", headers:{"Content-Type":"application/json"},
                    body: JSON.stringify({path:lastPath})});

// ---- Search + filter (Shorts vs normal) ----
let allResults = [], curFilter = "all";

async function runSearch() {
  const q = $("q").value.trim();
  if (!q) { $("q").focus(); return; }
  $("results").innerHTML = '<div class="hint">Searching…</div>';
  $("filter").style.display = "none";
  let data;
  try { data = await (await fetch("/search?q=" + encodeURIComponent(q))).json(); }
  catch { $("results").innerHTML = '<div class="hint">Search failed.</div>'; return; }
  if (data.error) { $("results").innerHTML = '<div class="hint">' + esc(data.error) + '</div>'; return; }
  allResults = data.results || [];
  if (!allResults.length) { $("results").innerHTML = '<div class="hint">No results.</div>'; return; }
  curFilter = "all";
  $("filter").style.display = "flex";
  updateSegs();
  render();
}

function updateSegs() {
  const nShort = allResults.filter(r => r.is_short).length;
  const c = { all: allResults.length, video: allResults.length - nShort, short: nShort };
  const names = { all: "All", video: "Videos", short: "Shorts" };
  document.querySelectorAll(".seg").forEach(b => {
    b.textContent = names[b.dataset.f] + " (" + c[b.dataset.f] + ")";
    b.classList.toggle("active", b.dataset.f === curFilter);
  });
}

function thumbInner(r) {
  const cls = r.is_short ? "dur short" : "dur";
  return '<img loading="lazy" src="https://i.ytimg.com/vi/' + r.id + '/mqdefault.jpg" alt="">' +
         '<span class="' + cls + '">' + fmtDur(r.duration) + '</span>';
}

function attachHover(thumb, r) {
  let t = null;
  thumb.addEventListener("mouseenter", () => {
    t = setTimeout(() => {
      thumb.innerHTML = '<iframe allow="autoplay" src="https://www.youtube.com/embed/' + r.id +
        '?autoplay=1&mute=1&controls=0&modestbranding=1&playsinline=1&rel=0"></iframe>';
    }, 400);
  });
  thumb.addEventListener("mouseleave", () => { clearTimeout(t); thumb.innerHTML = thumbInner(r); });
}

function render() {
  const list = allResults.filter(r =>
    curFilter === "all" ? true : curFilter === "short" ? r.is_short : !r.is_short);
  $("results").innerHTML = "";
  if (!list.length) { $("results").innerHTML = '<div class="hint">None in this category.</div>'; return; }
  for (const r of list) {
    const el = document.createElement("div");
    el.className = "result";
    el.innerHTML =
      '<div class="thumb">' + thumbInner(r) + '</div>' +
      '<div class="meta"><div class="t">' + esc(r.title) + '</div>' +
      '<div class="m">' + esc(r.uploader || "") + '</div></div>' +
      '<button>Download</button>';
    el.querySelector("button").onclick = () => startDownload("https://www.youtube.com/watch?v=" + r.id);
    attachHover(el.querySelector(".thumb"), r);
    $("results").appendChild(el);
  }
}

$("searchBtn").onclick = runSearch;
$("q").addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
document.querySelectorAll(".seg").forEach(b =>
  b.onclick = () => { curFilter = b.dataset.f; updateSegs(); render(); });

// ---- Download (shared by search results + pasted link) ----
$("go").onclick = () => startDownload($("url").value.trim());
$("url").addEventListener("keydown", e => { if (e.key === "Enter") startDownload($("url").value.trim()); });

async function startDownload(url) {
  if (!url) { $("url").focus(); return; }
  if (busy) { return; }
  busy = true;
  $("go").disabled = true; $("searchBtn").disabled = true;
  $("reveal").style.display = "none";
  $("status").className = "status";
  $("status").textContent = "Starting…";
  $("bar").style.display = "block";
  $("bar").className = "bar indet";
  $("bar").querySelector("i").style.width = "";

  let res;
  try {
    res = await (await fetch("/download", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({url, quality: $("quality").value})})).json();
  } catch { fail("Network error"); return; }
  if (res.error) { fail(res.error); return; }

  const ev = new EventSource("/progress/" + res.job_id);
  ev.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.status === "downloading") {
      const label = d.phase === "audio" ? "Downloading audio" : "Downloading video";
      if (d.pct != null) {
        $("bar").className = "bar";
        $("bar").querySelector("i").style.width = d.pct + "%";
        $("status").textContent = `${label} — ${d.pct}%  ${fmtSpeed(d.speed)} ${fmtEta(d.eta)}`;
      } else {
        $("status").textContent = label + "…";
      }
    } else if (d.status === "processing") {
      $("bar").className = "bar indet"; $("status").textContent = "Processing…";
    } else if (d.status === "merging") {
      $("bar").className = "bar indet"; $("status").textContent = "Merging video + audio…";
    } else if (d.status === "done") {
      ev.close();
      $("bar").className = "bar"; $("bar").querySelector("i").style.width = "100%";
      $("status").className = "status ok";
      const meta = d.height ? ` · ${d.height}p${d.fps ? "@" + Math.round(d.fps) + "fps" : ""}` : "";
      $("status").textContent = `✓ ${d.file}${meta}`;
      lastPath = d.path; $("reveal").style.display = "block";
      done();
    } else if (d.status === "error") {
      ev.close(); fail(d.error);
    }
  };
  ev.onerror = () => { ev.close(); fail("Lost connection to server"); };
}

function done() { busy = false; $("go").disabled = false; $("searchBtn").disabled = false; }
function fail(msg) {
  $("bar").style.display = "none";
  $("status").className = "status err";
  $("status").textContent = "✗ " + msg;
  done();
}
</script>
</body>
</html>
"""

def _pick_port(preferred):
    """Return the preferred port if free, otherwise an OS-assigned free one."""
    import socket

    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def _open_browser(url):
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


def main():
    """Console entry point (the `yoink` command): launch the app, open a browser."""
    global PORT
    PORT = _pick_port(PORT)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}"
    if not FFMPEG:
        print("⚠ ffmpeg not found and the bundled copy is unavailable — merging may fail.")
    print(f"\n  🧲 Yoink is running at {url}")
    print(f"     Saving downloads to {DOWNLOAD_DIR}")
    print("     Press Ctrl+C to stop.\n")
    threading.Timer(1.2, lambda: _open_browser(url)).start()
    try:
        app.run(port=PORT, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\n  Stopped. 👋")


if __name__ == "__main__":
    main()
