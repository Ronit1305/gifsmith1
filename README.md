# GIFsmith

A clean, self-hosted video → GIF converter. Two-pass FFmpeg pipeline for high quality output. Deployable on Railway in a few clicks.

## Features

- Drag-and-drop upload (MP4, MOV, AVI, MKV, WebM and more)
- Two-pass FFmpeg: palette generation + Bayer dithering — noticeably better than single-pass converters
- FPS, width/height, and trim controls
- Presets: Reddit (9:16 mobile), High Quality, Compact
- Silent MP4 export for Reddit / WhatsApp / Instagram
- Per-job unique noise fingerprinting
- Disk-backed job state (survives Railway container restarts)
- Auto-cleanup after 1 hour

---

## Local setup

**Requirements:** Python 3.11+, FFmpeg, Git

```bash
# Clone / extract the project
cd gifsmith

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

Open http://localhost:5000

---

## Deploy to Railway

1. Push to GitHub:
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/gifsmith.git
   git push -u origin main
   ```

2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo → select **gifsmith**

3. Railway detects the Dockerfile automatically and builds (~2–3 min).

4. **Critical:** Go to Service → Settings → clear the **Start Command** field completely. Railway will then use the Dockerfile CMD (port 8080 hardcoded).

5. Service → Settings → Networking → **Generate Domain** to get a public URL.

6. Every `git push` to main auto-redeploys.

---

## Architecture

```
Browser → Flask (Gunicorn, 2 workers, port 8080)
              ├─ /api/convert      → starts GIF job (background thread)
              ├─ /api/convert-mp4  → starts MP4 job (background thread)
              ├─ /api/status/:id   → polled every 700ms
              ├─ /api/preview/:id  → GIF inline
              └─ /api/download/:id → GIF file download

FFmpeg two-pass GIF pipeline:
  Pass 1: palettegen stats_mode=diff → optimal 256-colour palette
  Pass 2: paletteuse dither=bayer:bayer_scale=3 diff_mode=rectangle → final GIF

Pillow: ±1 pixel noise on ~0.01% of pixels for unique hash per output

Job persistence: /tmp/gifsmith/jobs.json — survives container restarts
File cleanup: background thread, every 5 min, deletes anything older than 1 hour
```

---

## Known limits on Railway Hobby plan

- Max upload: Railway's proxy caps at ~100 MB regardless of server setting
- No GPU — FFmpeg runs in software (fine for clips under ~2 min)
- Container may restart between requests on idle — handled by disk-backed jobs
