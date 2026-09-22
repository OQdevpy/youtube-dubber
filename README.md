# YouTube → Uzbek Dubbing

Paste a YouTube link, press **Tarjima qilish** ("Translate"), and watch the video in YouTube's own player with Uzbek voice-over and subtitles. The video is never downloaded.

For each video, a local server:
1. takes its captions, or transcribes its speech with ElevenLabs,
2. translates them to natural spoken Uzbek with **Gemini**,
3. voices them with free Microsoft Edge voices or **ElevenLabs**,
4. streams the dub to the browser as it's generated.

You can start watching after a few seconds and jump to any point; the server works on that point first.

```
docker-compose.yml   One container: API and web player on http://127.0.0.1:9988/
backend/             FastAPI server: main.py, requirements.txt, Dockerfile, .env.example
backend/web/         Web player (index.html, app.js, style.css)
extension/           Optional Chrome extension that dubs directly on youtube.com
```

## Run with Docker (recommended)

1. Create `backend/.env`: copy `backend/.env.example` and fill in the keys (see the table below).
2. From the project folder, start it:

   ```powershell
   docker compose up -d --build
   ```

3. Open **<http://127.0.0.1:9988/>**.

To stop it, run `docker compose down`. Generated audio is kept in the `dub-audio` Docker volume. After editing `.env`, run `docker compose up -d` again to apply it.

### On a server (VPS)

The same `docker compose up -d --build` works. The port is published on all interfaces, so the site is at `http://<server-ip>:9988/`. Two settings in `backend/.env` matter on a public server:

- **`DUB_PASSWORD`**: without it, anyone who finds the address can spend your Gemini and ElevenLabs credits. With it set, the browser asks for a login (user `DUB_USER`, default `dublyaj`).
- **`YOUTUBE_PROXY`**: YouTube blocks many datacenter IPs. If dubbing fails with `YOUTUBE_BLOCKED`, set a proxy such as `http://user:pass@host:port` (residential proxies work best).

After editing `.env`, run `docker compose up -d` to apply it. If the server has a firewall, open the port, for example with `ufw allow 9988/tcp`.

## Run without Docker

Requires Python 3.11+ and Node.js. yt-dlp needs a JavaScript runtime for speech-to-text. ffmpeg is not needed.

```powershell
cd backend
py -m venv .venv
.venv\Scripts\activate              # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env              # macOS/Linux: cp .env.example .env
python main.py                      # http://127.0.0.1:9988/
```

## Settings

Fill in `backend/.env`:

| Variable | Required | What it is |
| --- | --- | --- |
| `GEMINI_API_KEY` | yes | Key from <https://aistudio.google.com/apikey>. The Gemini project needs credits or billing. |
| `GEMINI_MODEL` | no | Defaults to `gemini-3.6-flash`. |
| `GEMINI_THINKING` | no | Defaults to `low`, about 4x faster than the model's default with near-identical translations, so the dub starts sooner. |
| `ELEVENLABS_API_KEY` | no | Adds ElevenLabs voices to the voice menu. The key needs **Text to Speech: Access**. With **Voices: Read** as well, your own voices are listed; without it you get ElevenLabs' standard voices. |
| `ELEVENLABS_MODEL` | no | Defaults to `eleven_v3`, the only ElevenLabs model that speaks Uzbek text. It detects the language itself; every ElevenLabs model rejects `language_code: "uz"`. |
| `ELEVENLABS_CONCURRENCY` | no | Parallel ElevenLabs requests. Default 2; keep it at or below your plan's limit. |
| `ELEVENLABS_STT_MODEL` | no | Speech-to-text model. Default `scribe_v2`. The key needs **Speech to Text: Access**. |

## Using the web player

1. Paste a YouTube link. Any form works: `youtube.com/watch?v=…`, `youtu.be/…`, `/shorts/…`, or a bare video ID.
2. Pick where the text comes from:
   - **YouTube subtitrlari**: the video's captions. Fast and free.
   - **Ovozdan matn (ElevenLabs)**: ElevenLabs transcribes the video's speech. Use it for videos without captions or with poor auto-captions. The source can be in any language. The audio is held in memory only, never saved, and it takes longer to start, because the whole video is transcribed first.

   If a video has no captions and an ElevenLabs key is set, speech-to-text is used automatically.
3. Pick a voice: **Madina** or **Sardor** (free), or one of your ElevenLabs voices.
4. Press **Tarjima qilish**.

The right-hand panel shows live progress:
- captions fetched,
- lines translated by Gemini,
- lines voiced,
- an estimate of the time left,
- the full transcript, Uzbek with the English underneath.

Click any line to jump there.

Under the player, a timeline shows every part of the video: grey means queued, pale turquoise means translated, and bright turquoise means voiced. Click anywhere on it to jump. If you reach a part that isn't voiced yet, the video pauses for a moment and continues once the voice is ready. You can turn this off with the checkbox in the panel.

The control strip has:

| Control | What it does |
| --- | --- |
| **YouTube ovozi** | Turns the original sound on or off, with a volume slider (default 20% while dubbing). |
| **O'zbekcha ovoz** | Turns the Uzbek dub on or off, with a volume slider. |
| **Inglizcha subtitr** | English subtitles. |
| **O'zbekcha subtitr** | Uzbek subtitles. |
| **To'liq ekran** (or the `F` key) | Fullscreen with subtitles. YouTube's own fullscreen button hides them, so use this one. |

Only audio is stored on disk, as MP3 files in `backend/cache/`. Captions and translations live in the server's memory, and the page stores nothing in the browser. After a server restart, a video is re-captioned and re-translated, but audio that already exists is reused rather than generated again.

## How it works

1. **Text.** Either `youtube-transcript-api` fetches English captions (manual preferred over auto-generated), or yt-dlp streams the audio into memory and ElevenLabs Scribe transcribes it with word timestamps.
2. **Segmentation.** Caption fragments are merged into sentence-sized segments of at most 12 s.
3. **Translation.** Gemini translates segments in batches and returns structured JSON. Each segment comes with its time budget, so Gemini shortens lines that wouldn't fit, and three neighbouring lines on each side for context. The first batch is small (8 lines) so playback can start quickly.
4. **Speech.** Each segment is voiced as soon as it's translated and saved as its own MP3 clip. Speech is kept at a natural pace:
   - Gemini writes lines for about 85% of the available time.
   - If a voiced line still runs over, Gemini rewrites it shorter and it's voiced again.
   - Only then is it sped up, and only a little: at most +20% on the server (edge voices) and 10% in the player.
   - A line that runs up to 1.5 s past the next line's start is allowed to finish. The next line starts slightly late and catches up in the following pause.
5. **Priority.** Translation and voicing always start at the viewer's playhead and move forward. Seeking changes the order.
6. **Streaming.** The browser receives segment updates over Server-Sent Events (`/dub/stream`) and fetches each clip when it's ready. Each clip plays when the video reaches its timestamp, kept within 0.3 s of the video's time.
7. **Full track.** When everything is voiced, a single synced MP3 is also assembled (from MP3 frames, no ffmpeg). The Chrome extension uses that file.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/dub/jobs?video_id=&voice=&source=&t=` | Starts or joins a job, prioritising time `t`. `source` is `captions` (default) or `stt`. |
| GET | `/dub/stream?video_id=&voice=` | SSE: `snapshot`, then `segment` and `job` events, ending with `done` or `error`. |
| POST | `/dub/focus?video_id=&voice=&t=` | The viewer jumped to `t`; generate from there first. |
| GET | `/dub/clip?video_id=&voice=&i=` | MP3 for segment `i`. Returns 404 `NOT_READY` until it's voiced. |
| GET | `/dub/segments?video_id=&voice=` | Every segment's text and status. |
| GET | `/dub/jobs?video_id=&voice=` | Job status and counts. |
| GET | `/dub?video_id=&voice=` | The full MP3. Waits until the job finishes. |
| GET | `/voices` | The available voices: edge-tts, plus your ElevenLabs voices if a key is set. |
| GET | `/health` | Liveness check. |

`voice` is `uz-UZ-MadinaNeural`, `uz-UZ-SardorNeural`, or `eleven:<voice_id>`. Every `/dub…` endpoint also takes `source` (`captions` or `stt`).

## Chrome extension (optional)

To dub directly on youtube.com: open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and select `extension/`. It uses the free voices and the full MP3, so it starts only after the whole video is generated. The web player is the streaming experience.

## Troubleshooting

- **"Gemini credits are used up" (`GEMINI_BILLING`).** Add credits or billing for the key's project at <https://ai.studio/projects>, then press **Qayta urinish** ("Retry").
- **`GEMINI_ERROR` about the model.** Set `GEMINI_MODEL` in `.env` to a model your key can use.
- **The video doesn't play in the page.** The video's owner has blocked embedding on other sites. It can only be watched on youtube.com.
- **"Bu videoda subtitr yo'q" ("this video has no captions").** Choose **Ovozdan matn (ElevenLabs)**, which needs an ElevenLabs key with Speech to Text access.
- **`ELEVENLABS_STT_PERMISSION`.** In ElevenLabs, give the API key **Speech to Text: Access**, then restart the server (`docker compose up -d`).
- **ElevenLabs errors.** Check your key, quota and voice. Uzbek isn't on ElevenLabs' official language list. If a voice sounds wrong, try another voice or model (`ELEVENLABS_MODEL`), or use the free Madina and Sardor voices, which are native Uzbek.
- **"O'zbekcha ovozni yoqish" ("turn on Uzbek audio") button appears.** The browser blocked autoplay. Click the button once.
- **Use `127.0.0.1`, not `localhost`.** On this machine, `localhost` can reach a different program over IPv6.
- **Opened `index.html` as a file.** The page moves itself to `http://127.0.0.1:9988/`, because YouTube's player refuses file pages (error 153).
