FROM python:3.12-slim

# yt-dlp needs a JavaScript runtime to read YouTube's player (speech-to-text source)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=9988 \
    DUB_CACHE_DIR=/data/audio

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY web ./web

# Only generated MP3 audio is ever written, and only here.
VOLUME ["/data/audio"]
EXPOSE 9988

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9988/health', timeout=4)"

CMD ["python", "main.py"]
