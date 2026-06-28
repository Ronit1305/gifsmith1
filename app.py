import os
import uuid
import json
import time
import threading
import subprocess
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Dirs & config ──────────────────────────────────────────────────────────────
UPLOAD_DIR  = Path("/tmp/gifsmith/uploads")
OUTPUT_DIR  = Path("/tmp/gifsmith/outputs")
JOBS_FILE   = Path("/tmp/gifsmith/jobs.json")
MAX_UPLOAD  = 200 * 1024 * 1024
JOB_TTL     = 3600
ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v", ".wmv", ".3gp", ".ts"}

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD

# ── In-memory job store (disk-backed) ─────────────────────────────────────────
_jobs_lock = threading.Lock()
jobs: dict = {}


def _save_jobs():
    tmp = JOBS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(jobs, f)
    tmp.replace(JOBS_FILE)


def _load_jobs():
    global jobs
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE) as f:
                jobs = json.load(f)
        except Exception:
            jobs = {}


def _get_job(job_id: str):
    if job_id in jobs:
        return jobs[job_id]
    _load_jobs()
    return jobs.get(job_id)


def _upd(job_id: str, updates: dict):
    """Update a job safely — always reloads from disk first to handle multi-worker setups."""
    with _jobs_lock:
        _load_jobs()
        if job_id not in jobs:
            jobs[job_id] = {}
        jobs[job_id].update(updates)
        _save_jobs()


# ── Background cleanup ─────────────────────────────────────────────────────────
def _cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        with _jobs_lock:
            _load_jobs()
            dead = [jid for jid, j in list(jobs.items())
                    if now - j.get("created", now) > JOB_TTL]
            for jid in dead:
                for key in ("input", "gif", "mp4"):
                    p = jobs[jid].get(key)
                    if p and os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                pal = OUTPUT_DIR / f"{jid}_palette.png"
                pal.unlink(missing_ok=True)
                del jobs[jid]
            _save_jobs()
        log.info("Cleanup pass: removed %d expired jobs", len(dead))


threading.Thread(target=_cleanup_loop, daemon=True).start()

# ── FFmpeg helpers ─────────────────────────────────────────────────────────────
def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        return r.returncode, (r.stderr or b"").decode(errors="replace")[-3000:]
    except subprocess.TimeoutExpired:
        return -1, "FFmpeg timed out"
    except FileNotFoundError:
        return -1, "ffmpeg not found"


def _build_scale(width: int, height: int) -> str:
    if width <= 0 and height <= 0:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if width <= 0:
        return f"scale=-2:{height}"
    if height <= 0:
        return f"scale={width}:-2"
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"


_COLOR_FIX = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"


def _convert_gif(job_id: str, params: dict):
    inp     = _get_job(job_id)["input"]
    palette = str(OUTPUT_DIR / f"{job_id}_palette.png")
    gif_out = str(OUTPUT_DIR / f"{job_id}.gif")

    fps   = params["fps"]
    scale = _build_scale(params["width"], params["height"])
    ss    = params.get("start", 0)
    dur   = params.get("duration")

    seek  = ["-ss", str(ss)] if ss else []
    limit = ["-t",  str(dur)] if dur else []

    # ── Pass 1: palette ───────────────────────────────────────────────────────
    _upd(job_id, {"stage": "Generating palette…", "progress": 10})

    vf1 = f"fps={fps},{scale},{_COLOR_FIX},format=rgb24,palettegen=stats_mode=diff:max_colors=256"
    rc, err = _run(
        ["ffmpeg", "-y", *seek, *limit, "-i", inp,
         "-vf", vf1, "-frames:v", "1", palette]
    )
    if rc != 0:
        log.error("Pass1 failed:\n%s", err)
        _upd(job_id, {"status": "error", "error": f"Palette step failed: {err[-500:]}"})
        return

    # ── Pass 2: GIF conversion ────────────────────────────────────────────────
    _upd(job_id, {"stage": "Converting…", "progress": 50})

    vf2 = (f"fps={fps},{scale},{_COLOR_FIX},format=rgb24 [x];"
           f"[x][1:v] paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle")
    rc, err = _run(
        ["ffmpeg", "-y", *seek, *limit, "-i", inp, "-i", palette,
         "-lavfi", vf2, gif_out]
    )

    Path(palette).unlink(missing_ok=True)

    if rc != 0:
        log.error("Pass2 failed:\n%s", err)
        _upd(job_id, {"status": "error", "error": f"GIF step failed: {err[-500:]}"})
        return

    # ── Done ──────────────────────────────────────────────────────────────────
    size = Path(gif_out).stat().st_size
    _upd(job_id, {
        "status": "done", "stage": "Done", "progress": 100,
        "gif": gif_out, "gif_size": size,
    })
    log.info("GIF done: %s (%.1f MB)", job_id, size / 1e6)


def _convert_mp4(job_id: str, gif_job_id: str):
    gif_job = _get_job(gif_job_id)
    if not gif_job:
        return
    inp    = gif_job["input"]
    params = gif_job["params"]
    out    = str(OUTPUT_DIR / f"{job_id}.mp4")

    fps   = params["fps"]
    scale = _build_scale(params["width"], params["height"])
    ss    = params.get("start", 0)
    dur   = params.get("duration")

    seek  = ["-ss", str(ss)] if ss else []
    limit = ["-t",  str(dur)] if dur else []

    vf = f"fps={fps},{scale},{_COLOR_FIX},format=yuv420p"
    rc, err = _run(
        ["ffmpeg", "-y", *seek, *limit, "-i", inp,
         "-vf", vf, "-an",
         "-c:v", "libx264", "-crf", "22", "-preset", "fast",
         "-movflags", "+faststart", out]
    )
    if rc != 0:
        _upd(job_id, {"status": "error", "error": err[-400:]})
        return

    size = Path(out).stat().st_size
    _upd(job_id, {"status": "done", "mp4": out, "mp4_size": size})
    log.info("MP4 done: %s (%.1f MB)", job_id, size / 1e6)


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/convert", methods=["POST"])
def convert():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="No file"), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"Unsupported format: {ext}"), 400

    job_id = uuid.uuid4().hex
    inp    = str(UPLOAD_DIR / f"{job_id}{ext}")
    f.save(inp)

    try:
        fps    = int(request.form.get("fps", 15))
        width  = int(request.form.get("width", 480))
        height = int(request.form.get("height", -1))
        start  = float(request.form.get("start", 0))
        end    = request.form.get("end", "").strip()
        dur    = (float(end) - start) if end else None
        fps    = max(6, min(30, fps))
    except ValueError:
        return jsonify(error="Bad parameters"), 400

    params = {"fps": fps, "width": width, "height": height,
              "start": start, "duration": dur}

    with _jobs_lock:
        jobs[job_id] = {
            "status": "running", "stage": "Uploading…", "progress": 0,
            "input": inp, "params": params, "created": time.time(),
        }
        _save_jobs()

    threading.Thread(target=_convert_gif, args=(job_id, params), daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/convert-mp4", methods=["POST"])
def convert_mp4():
    gif_job_id = request.json.get("gif_job_id") if request.is_json else request.form.get("gif_job_id")
    if not gif_job_id or not _get_job(gif_job_id):
        return jsonify(error="GIF job not found"), 404

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        jobs[job_id] = {"status": "running", "created": time.time()}
        _save_jobs()

    threading.Thread(target=_convert_mp4, args=(job_id, gif_job_id), daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/status/<job_id>")
def status(job_id):
    j = _get_job(job_id)
    if not j:
        return jsonify(error="Job not found"), 404
    return jsonify({k: v for k, v in j.items() if k not in ("input", "gif", "mp4")})


@app.route("/api/preview/<job_id>")
def preview(job_id):
    j = _get_job(job_id)
    if not j or not j.get("gif"):
        return jsonify(error="Not ready"), 404
    return send_file(j["gif"], mimetype="image/gif")


@app.route("/api/download/<job_id>")
def download(job_id):
    j = _get_job(job_id)
    if not j or not j.get("gif"):
        return jsonify(error="Not ready"), 404
    return send_file(j["gif"], mimetype="image/gif",
                     as_attachment=True, download_name="gifsmith_output.gif")


@app.route("/api/download-mp4/<job_id>")
def download_mp4(job_id):
    j = _get_job(job_id)
    if not j or not j.get("mp4"):
        return jsonify(error="Not ready"), 404
    return send_file(j["mp4"], mimetype="video/mp4",
                     as_attachment=True, download_name="gifsmith_output.mp4")


# ── Bootstrap ──────────────────────────────────────────────────────────────────
_load_jobs()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)