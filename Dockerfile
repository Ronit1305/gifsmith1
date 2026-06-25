FROM python:3.11-slim

# Install FFmpeg (and nothing else that isn't needed)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer-cached if requirements don't change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Pre-create temp dirs so they exist even on a cold start
RUN mkdir -p /tmp/gifsmith/uploads /tmp/gifsmith/outputs

EXPOSE 8080

# Port hardcoded to 8080 — avoids $PORT shell-expansion issues on Railway
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "2", \
     "--timeout", "300", \
     "--worker-class", "sync", \
     "--log-level", "info"]
