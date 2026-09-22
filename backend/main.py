"""
YouTube -> Uzbek dubbing server.

Pipeline for GET /dub?video_id=...:
  1. Fetch English captions (with timestamps) via youtube-transcript-api.
  2. Merge caption fragments into sentence-sized segments.
  3. Translate EN -> UZ with Gemini, which also sees each segment's time
     budget so the Uzbek stays short enough to be spoken in sync.
  4. Synthesize each segment with edge-tts, speeding up speech that would
     overrun its slot, then stitch everything into ONE MP3 whose timeline
     matches the video (segment i starts at caption i's timestamp).

The MP3 is assembled at the frame level (edge-tts emits CBR MP3), so no
ffmpeg install is needed. Silence between segments is made of silent MP3
frames with the same header as the speech frames.

Segments are voiced in order from the viewer's playhead and saved as separate
clips as soon as they are ready, so the web player (GET /) can stream the dub
while the rest is still being generated.

Run:  python main.py      then open http://127.0.0.1:8000/
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import edge_tts
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeTranscriptApi,
)
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

load_dotenv(Path(__file__).parent / ".env")

# --------------------------------------------------------------------------- #
# Configuration (override with environment variables)
# --------------------------------------------------------------------------- #

CACHE_DIR = Path(os.getenv("DUB_CACHE_DIR", Path(__file__).parent / "cache"))
VOICES = ("uz-UZ-MadinaNeural", "uz-UZ-SardorNeural")
DEFAULT_VOICE = os.getenv("DUB_DEFAULT_VOICE", VOICES[0])
TTS_CONCURRENCY = int(os.getenv("DUB_TTS_CONCURRENCY", "6"))
TRANSLATE_CONCURRENCY = int(os.getenv("DUB_TRANSLATE_CONCURRENCY", "3"))
BASE_RATE = int(os.getenv("DUB_BASE_RATE", "5"))  # % speed-up applied to all speech
MAX_RATE = int(os.getenv("DUB_MAX_RATE", "20"))  # % cap; long lines are shortened by Gemini first
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
# "low" thinking: ~4x faster than the default with near-identical translations,
# which matters because the first batch gates when the dub starts playing.
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low").strip()
GEMINI_BATCH = int(os.getenv("DUB_GEMINI_BATCH", "20"))  # segments per Gemini request
CACHE_VERSION = "v4"  # bump to invalidate cached audio after pipeline changes

# Where the source text comes from: YouTube's captions, or ElevenLabs
# speech-to-text on the video's audio (also used automatically when a video has
# no usable captions and an ElevenLabs key is configured).
SOURCES = ("captions", "stt")
ELEVEN_STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")

EN_CODES = ["en", "en-US", "en-GB", "en-CA", "en-AU", "en-IN", "en-IE", "en-NZ"]
VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dubber")


class DubError(Exception):
    """An expected failure that is reported to the client as {code, message}."""

    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------------- #
# 1. Transcript
# --------------------------------------------------------------------------- #


def _map_youtube_error(exc: Exception) -> DubError:
    if isinstance(exc, TranscriptsDisabled):
        return DubError("TRANSCRIPTS_DISABLED", "Captions are disabled for this video.", 404)
    if isinstance(exc, (VideoUnavailable, InvalidVideoId, VideoUnplayable)):
        return DubError("VIDEO_UNAVAILABLE", "This video is unavailable.", 404)
    if isinstance(exc, AgeRestricted):
        return DubError("AGE_RESTRICTED", "Age-restricted videos are not supported.", 403)
    if isinstance(exc, (RequestBlocked, IpBlocked)):
        return DubError(
            "YOUTUBE_BLOCKED",
            "YouTube is blocking caption requests from this IP. Try again later or use a proxy.",
            503,
        )
    return DubError("TRANSCRIPT_ERROR", f"Could not retrieve captions: {type(exc).__name__}", 502)


def fetch_english_transcript(video_id: str) -> list[dict]:
    """Blocking. Returns [{text, start, duration}, ...] of English captions."""
    api = YouTubeTranscriptApi()
    try:
        transcripts = api.list(video_id)
        try:
            transcript = transcripts.find_manually_created_transcript(EN_CODES)
        except NoTranscriptFound:
            try:
                transcript = transcripts.find_generated_transcript(EN_CODES)
            except NoTranscriptFound:
                # Last resort: let YouTube machine-translate another track to English.
                transcript = next(
                    (
                        t
                        for t in transcripts
                        if t.is_translatable
                        and any(lang.language_code == "en" for lang in t.translation_languages)
                    ),
                    None,
                )
                if transcript is None:
                    raise DubError(
                        "NO_ENGLISH_TRANSCRIPT",
                        "This video has no English captions (manual or auto-generated).",
                        404,
                    )
                transcript = transcript.translate("en")
        log.info("%s: using %s captions (%s)", video_id, transcript.language_code,
                 "auto" if transcript.is_generated else "manual")
        return transcript.fetch().to_raw_data()
    except CouldNotRetrieveTranscript as exc:
        raise _map_youtube_error(exc) from exc


async def fetch_youtube_audio(video_id: str) -> bytes:
    """The video's audio track, held in memory only (yt-dlp writes to stdout;
    nothing touches the disk). Used for speech-to-text, never played back."""
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-cache-dir", "--no-part",
           "-f", "bestaudio[abr<=96]/bestaudio", "-o", "-"]
    if not shutil.which("deno") and shutil.which("node"):
        cmd += ["--js-runtimes", "node"]  # yt-dlp needs a JS runtime for YouTube
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        audio, err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except TimeoutError:
        proc.kill()
        raise DubError("AUDIO_FETCH_FAILED", "Timed out fetching the video's audio from YouTube.", 504)
    if proc.returncode != 0 or not audio:
        detail = err.decode(errors="ignore").strip().splitlines()[-1:] or ["unknown error"]
        raise DubError("AUDIO_FETCH_FAILED", f"Could not fetch the video's audio: {detail[0][:200]}", 502)
    return audio


async def transcribe_elevenlabs(audio: bytes) -> tuple[list[dict], str]:
    """ElevenLabs Scribe -> (caption-like [{text, start, duration}] per word, language)."""
    form = aiohttp.FormData()
    form.add_field("model_id", ELEVEN_STT_MODEL)
    form.add_field("timestamps_granularity", "word")
    form.add_field("tag_audio_events", "false")
    form.add_field("file", audio, filename="audio.webm", content_type="application/octet-stream")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as session:
        async with session.post(f"{ELEVEN_API}/speech-to-text", headers={"xi-api-key": elevenlabs_key()}, data=form) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:300]
                if "missing_permissions" in detail:
                    raise DubError("ELEVENLABS_STT_PERMISSION",
                                   "The ElevenLabs API key lacks the Speech to Text permission.", 502)
                if resp.status == 401:
                    raise DubError("ELEVENLABS_AUTH", "The ElevenLabs API key is invalid.", 502)
                if resp.status == 402 or "quota" in detail:
                    raise DubError("ELEVENLABS_QUOTA", "Your ElevenLabs quota is used up.", 502)
                raise DubError("STT_FAILED", f"ElevenLabs speech-to-text error {resp.status}: {detail}", 502)
            data = await resp.json()
    words = [
        {"text": w["text"], "start": float(w["start"]), "duration": max(float(w["end"]) - float(w["start"]), 0.05)}
        for w in data.get("words", [])
        if w.get("type") == "word" and w.get("text", "").strip()
    ]
    return words, data.get("language_code") or "unknown"


# --------------------------------------------------------------------------- #
# 2. Segmentation
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    start: float
    end: float
    text: str
    uz: str = ""
    status: str = "pending"
    duration: float = 0.0


_TAG = re.compile(r"<[^>]+>")
_NOISE = re.compile(r"\[[^\]]*\]|\((?:music|applause|laughter|laughs|inaudible)[^)]*\)|[♪♫]+|>>", re.I)
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")


def clean_caption(text: str) -> str:
    text = html.unescape(text)
    text = _TAG.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def group_segments(
    raw: list[dict], max_seconds: float = 12.0, max_chars: int = 240, max_gap: float = 1.2
) -> list[Segment]:
    """Merge YouTube's short caption fragments into sentence-sized segments.

    Translating whole sentences gives far better Uzbek than translating
    3-word fragments, and gives TTS natural prosody.
    """
    segments: list[Segment] = []
    cur: Segment | None = None
    for item in raw:
        text = clean_caption(item.get("text", ""))
        if not text:
            continue
        start = float(item["start"])
        end = start + max(float(item.get("duration", 0.0)), 0.2)
        if cur is not None and (
            _SENTENCE_END.search(cur.text)
            or start - cur.end > max_gap
            or end - cur.start > max_seconds
            or len(cur.text) + len(text) > max_chars
        ):
            segments.append(cur)
            cur = None
        if cur is None:
            cur = Segment(start=start, end=end, text=text)
        else:
            cur.text = f"{cur.text} {text}"
            cur.end = max(cur.end, end)
    if cur is not None:
        segments.append(cur)
    return segments


def segment_slots(segments: list[Segment]) -> list[float]:
    """Seconds each segment may speak for: until the next segment starts."""
    return [
        (segments[i + 1].start if i + 1 < len(segments) else seg.end + 1.5) - seg.start
        for i, seg in enumerate(segments)
    ]


# --------------------------------------------------------------------------- #
# 3. Translation (Gemini)
# --------------------------------------------------------------------------- #

# Uzbek TTS voices speak ~12-14 characters/second; plan for the slow end so
# lines fit without being sped up.
UZ_CHARS_PER_SECOND = 12
CONTEXT_LINES = 3  # neighbouring English lines sent for context, not translated

TRANSLATION_PROMPT = """\
You are a professional dubbing translator. You translate YouTube captions \
(usually English; the "text" field may be in any language) into natural, spoken Uzbek (Latin script, modern standard orthography: \
o', g', sh, ch, ng) that will be read aloud by a text-to-speech voice.

Rules:
- Translate the MEANING, not word by word. Sound like a native Uzbek narrator \
talking to a viewer, not like a written document.
- Each segment has "seconds": the time available to speak it. The translation \
MUST be speakable in that time at a calm pace: aim for about 85% of the \
seconds, at most {cps} characters per second. If a literal translation is too \
long, shorten or paraphrase it while keeping the key meaning. Drop filler words (um, uh, you know, like, so) and false starts.
- Segments are consecutive pieces of one speech; a sentence may continue into \
the next segment. Translate each segment so that, read in order, they form \
fluent Uzbek. Never move content from one segment into another id.
- Keep names of people, brands, products and code identifiers as they are; \
use established Uzbek forms for well-known places and terms.
- Write numbers as digits. No quotes, notes, brackets, emoji or explanations.
- Return one item for EVERY input id, in the same order."""

RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "uz": {"type": "string"}},
        "required": ["id", "uz"],
    },
}

_gemini_client: genai.Client | None = None


def gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise DubError(
                "GEMINI_KEY_MISSING",
                "GEMINI_API_KEY is not set. Put it in backend/.env and restart the server.",
                500,
            )
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


async def _gemini_translate(items: list[dict], context_before: list[str], context_after: list[str]) -> dict[int, str]:
    payload = {
        "context_before": context_before,
        "segments": items,
        "context_after": context_after,
    }
    global _gemini_thinking
    extra = {}
    if _gemini_thinking:
        extra["thinking_config"] = genai_types.ThinkingConfig(thinking_level=_gemini_thinking)
    try:
        response = await _gemini_generate(payload, extra)
    except genai_errors.ClientError as exc:
        if exc.code == 400 and extra and "think" in str(exc.message).lower():
            log.warning("Gemini model %s rejected thinking_level=%s; using its default", GEMINI_MODEL, _gemini_thinking)
            _gemini_thinking = ""
            response = await _gemini_generate(payload, {})
        else:
            raise
    result = json.loads(response.text or "[]")
    return {int(r["id"]): str(r["uz"]).strip() for r in result if isinstance(r, dict) and "id" in r and "uz" in r}


_gemini_thinking = GEMINI_THINKING


async def _gemini_generate(payload: dict, extra: dict):
    return await gemini_client().aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=(
            "Translate the segments below. context_before/context_after are only "
            "for understanding; do not translate them.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        ),
        config=genai_types.GenerateContentConfig(
            system_instruction=TRANSLATION_PROMPT.format(cps=UZ_CHARS_PER_SECOND),
            response_mime_type="application/json",
            response_json_schema=RESPONSE_SCHEMA,
            temperature=0.3,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
            **extra,
        ),
    )


async def translate_batch(lines: list[str], slots: list[float], idx: list[int]) -> dict[int, str]:
    """Translate the segments in `idx`, retrying until every id has a result."""
    first, last = idx[0], idx[-1]
    context_before = lines[max(0, first - CONTEXT_LINES) : first]
    context_after = lines[last + 1 : last + 1 + CONTEXT_LINES]
    out: dict[int, str] = {}
    last_exc: Exception | None = None

    for attempt in range(5):
        missing = [i for i in idx if not out.get(i)]
        if not missing:
            return out
        items = [{"id": i, "seconds": round(slots[i], 1), "text": lines[i]} for i in missing]
        try:
            got = await _gemini_translate(items, context_before, context_after)
            out.update({i: text for i, text in got.items() if i in missing and text})
            if len(got) < len(missing):
                log.info("Gemini returned %d/%d segments; retrying the rest", len(got), len(missing))
            continue
        except genai_errors.ClientError as exc:
            if exc.code == 402:
                raise DubError("GEMINI_BILLING", f"Gemini credits are used up: {exc.message}", 502) from exc
            if exc.code in (400, 401, 403, 404):
                message = {
                    400: f"Gemini rejected the request: {exc.message}",
                    401: "The Gemini API key is invalid.",
                    403: "The Gemini API key is not allowed to use this model.",
                    404: f"Gemini model '{GEMINI_MODEL}' is not available: {exc.message} Set GEMINI_MODEL in backend/.env.",
                }[exc.code]
                raise DubError("GEMINI_ERROR", message, 502) from exc
            last_exc = exc  # 429 quota / rate limit: back off and retry
        except (genai_errors.APIError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            last_exc = exc  # 5xx or malformed output
        except Exception as exc:  # network errors from httpx
            last_exc = exc
        log.warning("Gemini translate failed (attempt %d): %s", attempt + 1, last_exc)
        await asyncio.sleep(2 * 2**attempt)

    missing = [i for i in idx if not out.get(i)]
    if missing:
        detail = f": {last_exc}" if last_exc else f" ({len(missing)} segments left untranslated)"
        raise DubError("TRANSLATION_FAILED", f"Gemini translation failed{detail}", 502)
    return out


SHORTEN_PROMPT = """\
You edit Uzbek dubbing lines (Latin script) that are too long to be spoken in \
the time available. Rewrite the line so it can be said calmly in the given \
seconds (at most {max_chars} characters): keep the essential meaning, drop \
secondary details and filler, stay natural spoken Uzbek. Return JSON \
{{"uz": "<the shorter line>"}} and nothing else."""


async def shorten_line(source: str, uz: str, seconds: float) -> str | None:
    """Ask Gemini for a shorter version of a line whose speech overran its
    slot. Returns None when that fails; the caller then speeds speech up."""
    max_chars = max(8, int(seconds * UZ_CHARS_PER_SECOND * 0.85))
    extra = {"thinking_config": genai_types.ThinkingConfig(thinking_level=_gemini_thinking)} if _gemini_thinking else {}
    try:
        response = await gemini_client().aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=json.dumps({"seconds": round(seconds, 1), "original": source, "uzbek_line": uz}, ensure_ascii=False),
            config=genai_types.GenerateContentConfig(
                system_instruction=SHORTEN_PROMPT.format(max_chars=max_chars),
                response_mime_type="application/json",
                response_json_schema={"type": "object", "properties": {"uz": {"type": "string"}}, "required": ["uz"]},
                temperature=0.3,
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
                **extra,
            ),
        )
        shorter = str(json.loads(response.text or "{}").get("uz", "")).strip()
        return shorter if 0 < len(shorter) < len(uz) else None
    except Exception as exc:
        log.warning("shortening failed, will speed up instead: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# 4. MP3 frame handling (no ffmpeg required)
# --------------------------------------------------------------------------- #

_BITRATES = {
    "mpeg1": [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    "mpeg2": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _frame_info(h: bytes) -> tuple[int, float] | None:
    """Parse a Layer III frame header. Returns (frame_size, duration) or None."""
    if h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    version = (h[1] >> 3) & 0b11  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer = (h[1] >> 1) & 0b11  # 1=Layer III
    br_idx = h[2] >> 4
    sr_idx = (h[2] >> 2) & 0b11
    padding = (h[2] >> 1) & 1
    if version == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
        return None
    mpeg1 = version == 3
    bitrate = _BITRATES["mpeg1" if mpeg1 else "mpeg2"][br_idx] * 1000
    sample_rate = _SAMPLE_RATES[version][sr_idx]
    size = (144 if mpeg1 else 72) * bitrate // sample_rate + padding
    samples = 1152 if mpeg1 else 576
    return size, samples / sample_rate


def _format_key(h: bytes) -> tuple[int, int, int]:
    """Bits that must match for frames to be concatenated into one stream."""
    return (h[1] & 0b11111110, h[2] & 0b11111100, h[3] & 0b11000000)


@dataclass
class Mp3Clip:
    frames: list[bytes]
    frame_duration: float

    @property
    def duration(self) -> float:
        return len(self.frames) * self.frame_duration


def parse_mp3(data: bytes) -> Mp3Clip:
    """Split an MP3 byte stream into audio frames, dropping ID3/Xing metadata."""
    i = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        i = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])
    frames: list[bytes] = []
    frame_duration = 0.0
    n = len(data)
    while i + 4 <= n:
        info = _frame_info(data[i : i + 4])
        if info is None:
            i += 1  # resync
            continue
        size, frame_duration = info
        if i + size > n:
            break
        frame = data[i : i + size]
        if not any(tag in frame[:64] for tag in (b"Xing", b"Info", b"VBRI")):
            frames.append(frame)
        i += size
    return Mp3Clip(frames, frame_duration)


def silent_frame(template: bytes) -> bytes:
    """A frame with the template's format whose side info / main data are all
    zero: it decodes to pure silence."""
    h = bytearray(template[:4])
    h[1] |= 0x01  # protection bit = 1 -> no CRC
    h[2] &= ~0x02 & 0xFF  # no padding
    size, _ = _frame_info(bytes(h))
    return bytes(h) + bytes(size - 4)


def assemble_timeline(placed: list[tuple[float, Mp3Clip]]) -> tuple[bytes, float]:
    """Lay clips on a timeline starting at their timestamps, filling gaps with
    silence. A clip that would overlap the previous one starts right after it
    (the drift is absorbed by the next gap). Returns (mp3_bytes, drift_max)."""
    clips = [(t, c) for t, c in placed if c.frames]
    if not clips:
        raise DubError("TTS_FAILED", "No speech could be generated for this video.", 502)
    template = clips[0][1].frames[0]
    fmt = _format_key(template)
    fd = clips[0][1].frame_duration
    silence = silent_frame(template)

    out = bytearray()
    cursor = 0  # in frames
    max_drift = 0.0
    for start, clip in sorted(clips, key=lambda p: p[0]):
        if _format_key(clip.frames[0]) != fmt:
            log.warning("skipping clip at %.2fs: incompatible MP3 format", start)
            continue
        want = round(start / fd)
        if want > cursor:
            out += silence * (want - cursor)
            cursor = want
        max_drift = max(max_drift, (cursor - want) * fd)
        for frame in clip.frames:
            out += frame
        cursor += len(clip.frames)
    return bytes(out), max_drift


# --------------------------------------------------------------------------- #
# 5. Text-to-speech
# --------------------------------------------------------------------------- #


async def synthesize(text: str, voice: str, rate: int) -> bytes:
    """edge-tts -> MP3 bytes, with retries on transient network errors."""
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%")
            buf = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf += chunk["data"]
            return bytes(buf)
        except edge_tts.exceptions.NoAudioReceived:
            log.warning("no audio for text %r; skipping", text[:60])
            return b""
        except (aiohttp.ClientError, asyncio.TimeoutError, edge_tts.exceptions.EdgeTTSException) as exc:
            last_exc = exc
            log.warning("edge-tts failed (attempt %d): %s", attempt + 1, exc)
            await asyncio.sleep(1.0 * 2**attempt)
    raise DubError("TTS_FAILED", f"Text-to-speech service failed: {last_exc}", 502)


ELEVEN_API = "https://api.elevenlabs.io/v1"
ELEVEN_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_v3")
ELEVEN_FORMAT = "mp3_44100_128"  # CBR MP3: stitchable like edge-tts output
ELEVEN_VOICE_PATTERN = re.compile(r"^eleven:([A-Za-z0-9]{8,40})$")
_eleven_sem = asyncio.Semaphore(int(os.getenv("ELEVENLABS_CONCURRENCY", "2")))  # plan concurrency limit
# ElevenLabs' models reject language_code "uz" (eleven_v3 auto-detects Uzbek from
# the text), so it is only sent when ELEVENLABS_LANGUAGE is set explicitly.
ELEVEN_LANGUAGE = os.getenv("ELEVENLABS_LANGUAGE", "").strip()
_eleven_send_language = bool(ELEVEN_LANGUAGE)  # dropped automatically if the model rejects it

# ElevenLabs' shared premade voices: offered when the key can't list the
# account's voices (the key lacks the "Voices: Read" permission).
ELEVEN_PREMADE = {
    "JBFqnCBsd6RMkjVDRZzb": "George", "nPczCjzI2devNBz1zQrb": "Brian", "onwK4e9ZLuTAKqWW03F9": "Daniel",
    "pNInz6obpgDQGcFmaJgB": "Adam", "21m00Tcm4TlvDq8ikWAM": "Rachel", "EXAVITQu4vr4xnSDxMaL": "Sarah",
    "9BWtsMINqrJLrRacOk9x": "Aria", "XB0fDUnXU5powFXDhCwa": "Charlotte",
}


def elevenlabs_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        raise DubError(
            "ELEVENLABS_KEY_MISSING",
            "ELEVENLABS_API_KEY is not set. Put it in backend/.env and restart the server.",
            400,
        )
    return key


async def synthesize_elevenlabs(text: str, voice_id: str) -> bytes:
    """ElevenLabs streaming TTS -> MP3 bytes (chunks are read as they arrive)."""
    global _eleven_send_language
    key = elevenlabs_key()
    last_error = ""
    for attempt in range(5):
        body = {
            "text": text,
            "model_id": ELEVEN_MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0},
        }
        if _eleven_send_language:
            body["language_code"] = ELEVEN_LANGUAGE
        try:
            async with _eleven_sem, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(
                    f"{ELEVEN_API}/text-to-speech/{voice_id}/stream",
                    params={"output_format": ELEVEN_FORMAT},
                    headers={"xi-api-key": key, "Content-Type": "application/json"},
                    json=body,
                ) as resp:
                    if resp.status == 200:
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(16384):
                            buf += chunk
                        return bytes(buf)
                    status, detail = resp.status, await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = str(exc) or type(exc).__name__
            log.warning("ElevenLabs network error (attempt %d): %s", attempt + 1, last_error)
            await asyncio.sleep(1.5 * 2**attempt)
            continue

        lowered = detail.lower()
        if "missing_permissions" in lowered:
            raise DubError("ELEVENLABS_PERMISSION",
                           "The ElevenLabs API key lacks the Text to Speech permission.", 502)
        if status == 401 and "quota" not in lowered:
            raise DubError("ELEVENLABS_AUTH", "The ElevenLabs API key is invalid.", 502)
        if status == 402 or "quota_exceeded" in lowered:
            raise DubError("ELEVENLABS_QUOTA", "Your ElevenLabs character quota is used up.", 502)
        if status == 404 or "voice_not_found" in lowered:
            raise DubError("ELEVENLABS_VOICE", "This ElevenLabs voice was not found in your account.", 404)
        if status in (400, 422) and _eleven_send_language and "language" in lowered:
            log.warning("ElevenLabs model %s rejected language_code=%s; using auto-detect", ELEVEN_MODEL, ELEVEN_LANGUAGE)
            _eleven_send_language = False
            continue
        if status in (429, 500, 502, 503, 504):
            last_error = f"HTTP {status}: {detail[:200]}"
            log.warning("ElevenLabs busy (attempt %d): %s", attempt + 1, last_error)
            await asyncio.sleep(2 * 2**attempt)
            continue
        raise DubError("ELEVENLABS_ERROR", f"ElevenLabs error {status}: {detail[:300]}", 502)
    raise DubError("TTS_FAILED", f"ElevenLabs text-to-speech failed: {last_error}", 502)


async def synthesize_raw(text: str, voice: str, rate: int = BASE_RATE) -> Mp3Clip:
    """Voice text with the provider the voice id belongs to."""
    if match := ELEVEN_VOICE_PATTERN.match(voice):
        return parse_mp3(await synthesize_elevenlabs(text, match.group(1)))
    return parse_mp3(await synthesize(text, voice, rate))


async def synthesize_fitted(seg: "Segment", voice: str, slot: float, on_shortened=None) -> Mp3Clip:
    """Voice a segment so it fits its slot without sounding rushed:
    1. speak it at a normal pace;
    2. if it runs over, have Gemini shorten the line and speak that;
    3. only if it still runs over, speed up a little (edge voices, <= MAX_RATE)."""
    clip = await synthesize_raw(seg.uz, voice)
    if slot <= 0 or clip.duration <= slot * 1.08:
        return clip
    shorter = await shorten_line(seg.text, seg.uz, slot)
    if shorter:
        shorter_clip = await synthesize_raw(shorter, voice)
        if shorter_clip.frames and shorter_clip.duration < clip.duration:
            log.info("shortened a line: %.1fs -> %.1fs (slot %.1fs)", clip.duration, shorter_clip.duration, slot)
            seg.uz, clip = shorter, shorter_clip
            if on_shortened:
                on_shortened()
    if clip.duration > slot * 1.05 and not ELEVEN_VOICE_PATTERN.match(voice):
        natural = clip.duration * (1 + BASE_RATE / 100)
        rate = min(MAX_RATE, math.ceil((natural / slot - 1) * 100) + 3)
        if rate > BASE_RATE:
            faster = await synthesize_raw(seg.uz, voice, rate)
            if faster.frames:
                clip = faster
    return clip


# --------------------------------------------------------------------------- #
# Jobs: streaming generation with a movable focus
# --------------------------------------------------------------------------- #
#
# Every segment moves through
#     pending -> translating -> translated -> voicing -> ready | empty
# and is saved as its own MP3 clip the moment it is voiced, so a client can
# start playing long before the whole video is done. Work is always picked
# closest-first from the job's `focus` (the viewer's playhead): segments at or
# after the focus in order, then the ones before it. Seeking moves the focus.
# Changes are pushed to subscribers as Server-Sent Events.

FIRST_BATCH = 8  # small first Gemini batch so playback can start within seconds
TRANSLATE_WORKERS = TRANSLATE_CONCURRENCY
FINAL_STATES = ("ready", "empty")


def job_dir(video_id: str, voice: str, source: str = "captions") -> Path:
    if match := ELEVEN_VOICE_PATTERN.match(voice):
        voice = f"eleven_{match.group(1)}_{ELEVEN_MODEL}"
    suffix = ".stt" if source == "stt" else ""
    return CACHE_DIR / f"{video_id}.{voice}{suffix}.{CACHE_VERSION}"


def clip_path(video_id: str, voice: str, source: str, i: int) -> Path:
    return job_dir(video_id, voice, source) / "clips" / f"{i}.mp3"


@dataclass
class Job:
    video_id: str
    voice: str
    source: str = "captions"  # captions | stt (what the job was asked for)
    used_source: str = ""  # what it actually used (captions can fall back to stt)
    language: str = ""  # detected source language when using stt
    state: str = "queued"  # queued|transcript|working|assembling|done|error
    progress: float = 0.0
    message: str = "Queued"
    error_code: str | None = None
    error: str | None = None
    error_status: int = 502
    segments: list[Segment] = field(default_factory=list)
    slots: list[float] = field(default_factory=list)
    focus: float = 0.0
    refocused: bool = False
    started_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, repr=False)
    subscribers: set[asyncio.Queue] = field(default_factory=set, repr=False)

    # ----- state + events

    def counts(self) -> dict:
        n = len(self.segments)
        translated = sum(s.status in ("translated", "voicing", *FINAL_STATES) for s in self.segments)
        voiced = sum(s.status in FINAL_STATES for s in self.segments)
        return {"total": n, "translated": translated, "voiced": voiced}

    def public(self) -> dict:
        return {
            "video_id": self.video_id,
            "voice": self.voice,
            "source": self.source,
            "used_source": self.used_source,
            "language": self.language,
            "state": self.state,
            "progress": self.progress,
            "message": self.message,
            "counts": self.counts(),
            "error": {"code": self.error_code, "message": self.error} if self.error_code else None,
        }

    def snapshot(self) -> dict:
        return {"job": self.public(), "segments": [self.segment_public(i) for i in range(len(self.segments))]}

    def segment_public(self, i: int) -> dict:
        s = self.segments[i]
        return {"i": i, "start": s.start, "end": s.end, "en": s.text, "uz": s.uz,
                "status": s.status, "duration": round(s.duration, 3)}

    def emit(self, event: str, data: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait((event, data))

    def update(self, state: str, message: str) -> None:
        c = self.counts()
        if state == "done":
            self.progress = 1.0
        elif c["total"]:
            self.progress = round(0.05 + 0.25 * c["translated"] / c["total"] + 0.68 * c["voiced"] / c["total"], 3)
        self.state, self.message = state, message
        self.emit("job", self.public())

    def set_status(self, i: int, status: str) -> None:
        self.segments[i].status = status
        self.emit("segment", self.segment_public(i))

    # ----- scheduling

    def _priority(self, i: int) -> tuple[int, float]:
        seg = self.segments[i]
        ahead = seg.start + self.slots[i] > self.focus
        return (0, seg.start) if ahead else (1, -seg.start)

    def pick_translation(self) -> list[int] | None:
        pending = [i for i, s in enumerate(self.segments) if s.status == "pending"]
        if not pending:
            return None
        size = FIRST_BATCH if self.refocused or len(pending) == len(self.segments) else GEMINI_BATCH
        self.refocused = False
        i = min(pending, key=self._priority)
        run = []
        while i < len(self.segments) and self.segments[i].status == "pending" and len(run) < size:
            run.append(i)
            i += 1
        for j in run:
            self.segments[j].status = "translating"
        return run

    def pick_tts(self) -> int | None:
        ready = [i for i, s in enumerate(self.segments) if s.status == "translated"]
        if not ready:
            return None
        i = min(ready, key=self._priority)
        self.segments[i].status = "voicing"
        return i

    def set_focus(self, t: float) -> None:
        self.focus = max(0.0, t)
        self.refocused = True


JOBS: dict[tuple[str, str, str], Job] = {}


async def run_pipeline(job: Job) -> None:
    vid, voice = job.video_id, job.voice
    t0 = time.perf_counter()
    clips: dict[int, Mp3Clip] = {}
    try:
        gemini_client()  # fail fast on missing API keys
        if ELEVEN_VOICE_PATTERN.match(voice) or job.source == "stt":
            elevenlabs_key()
        raw = await load_source_text(job)
        segments = group_segments(raw)
        if not segments:
            raise DubError("EMPTY_TRANSCRIPT", "The video has no speakable text.", 404)
        log.info("%s: %d source lines (%s) -> %d segments", vid, len(raw), job.used_source, len(segments))
        job.segments, job.slots = segments, segment_slots(segments)
        lines = [s.text for s in segments]
        out_dir = job_dir(vid, voice, job.source) / "clips"
        out_dir.mkdir(parents=True, exist_ok=True)
        job.emit("snapshot", job.snapshot())
        job.update("working", "Translating and voicing…")

        work = asyncio.Condition()

        async def translator() -> None:
            while (run := job.pick_translation()) is not None:
                for i in run:
                    job.emit("segment", job.segment_public(i))
                result = await translate_batch(lines, job.slots, run)
                for i in run:
                    job.segments[i].uz = result[i]
                    job.set_status(i, "translated")
                job.update("working", "Translating and voicing…")
                async with work:
                    work.notify_all()
            async with work:
                work.notify_all()

        def translation_pending() -> bool:
            return any(s.status in ("pending", "translating") for s in job.segments)

        async def voicer() -> None:
            while True:
                async with work:
                    while (i := job.pick_tts()) is None and translation_pending():
                        await work.wait()
                if i is None:
                    return
                job.emit("segment", job.segment_public(i))
                seg = job.segments[i]
                path = out_dir / f"{i}.mp3"
                if path.exists():  # voiced in an earlier run: audio is the only thing kept on disk
                    clip = parse_mp3(path.read_bytes())
                else:
                    clip = (
                        await synthesize_fitted(seg, voice, job.slots[i],
                                                on_shortened=lambda i=i: job.emit("segment", job.segment_public(i)))
                        if seg.uz else Mp3Clip([], 0)
                    )
                    if clip.frames:
                        path.with_suffix(".tmp").write_bytes(b"".join(clip.frames))
                        path.with_suffix(".tmp").replace(path)
                if clip.frames:
                    clips[i] = clip
                    seg.duration = clip.duration
                job.set_status(i, "ready" if clip.frames else "empty")
                job.update("working", "Translating and voicing…")

        try:
            async with asyncio.TaskGroup() as tg:
                for _ in range(TRANSLATE_WORKERS):
                    tg.create_task(translator())
                for _ in range(TTS_CONCURRENCY):
                    tg.create_task(voicer())
        except* DubError as eg:
            raise eg.exceptions[0] from None

        job.update("assembling", "Assembling the full audio track…")
        audio, drift = assemble_timeline([(job.segments[i].start, c) for i, c in clips.items()])
        full = job_dir(vid, voice, job.source) / "full.mp3"
        full.with_suffix(".tmp").write_bytes(audio)
        full.with_suffix(".tmp").replace(full)
        job.progress = 1.0
        job.update("done", "Ready")
        job.emit("done", job.public())
        log.info("%s: done in %.1fs, %.1f KB, max drift %.2fs",
                 vid, time.perf_counter() - t0, len(audio) / 1024, drift)
    except Exception as exc:  # report every failure to clients; never die silently
        if isinstance(exc, DubError):
            log.warning("%s: %s - %s", vid, exc.code, exc.message)
            job.error_code, job.error, job.error_status = exc.code, exc.message, exc.status
        else:
            log.exception("%s: unexpected failure", vid)
            job.error_code, job.error, job.error_status = "INTERNAL", f"Unexpected server error: {exc}", 500
        job.update("error", job.error)
        job.emit("error", job.public())


async def load_source_text(job: Job) -> list[dict]:
    """Caption-like [{text, start, duration}] from the job's source. Asking for
    captions falls back to speech-to-text when the video has none usable and an
    ElevenLabs key is configured."""
    if job.source == "captions":
        job.update("transcript", "Fetching captions…")
        try:
            raw = await asyncio.to_thread(fetch_english_transcript, job.video_id)
            job.used_source = "captions"
            return raw
        except DubError as exc:
            if exc.code not in ("TRANSCRIPTS_DISABLED", "NO_ENGLISH_TRANSCRIPT") or not os.getenv("ELEVENLABS_API_KEY"):
                raise
            log.info("%s: no usable captions (%s); using speech-to-text", job.video_id, exc.code)
    job.used_source = "stt"
    job.update("transcript", "Fetching the video's audio for speech-to-text…")
    audio = await fetch_youtube_audio(job.video_id)
    job.update("transcript", "Transcribing speech (ElevenLabs)…")
    words, job.language = await transcribe_elevenlabs(audio)
    del audio  # held in memory only, never written
    return words


def get_or_start_job(video_id: str, voice: str, force: bool = False, focus: float = 0.0,
                     source: str = "captions") -> Job:
    """Only audio is stored on disk. Captions and translations live in memory,
    so after a server restart a video is re-captioned and re-translated, but
    segments already voiced are read back from their MP3s instead of re-voiced."""
    key = (video_id, voice, source)
    job = JOBS.get(key)
    if job and job.state not in ("done", "error"):
        return job  # already running: never start a duplicate
    if job and job.state == "done" and not force:
        return job
    if force:
        shutil.rmtree(job_dir(video_id, voice, source), ignore_errors=True)
    job = Job(video_id, voice, source=source, focus=focus)
    job.task = asyncio.create_task(run_pipeline(job))
    JOBS[key] = job
    return job


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

app = FastAPI(title="YouTube Uzbek Dubber", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    # the extension, youtube.com, and local pages (editor previews, file://)
    allow_origin_regex=r"^(chrome-extension://[a-p]{32}|https://(www|m)\.youtube\.com|http://(127\.0\.0\.1|localhost)(:\d+)?|null)$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

VideoIdQuery = Query(..., pattern=VIDEO_ID_PATTERN, description="11-character YouTube video id")


def _check_voice(voice: str) -> str:
    if voice in VOICES or ELEVEN_VOICE_PATTERN.match(voice):
        return voice
    raise HTTPException(422, {"code": "BAD_VOICE",
                              "message": f"voice must be one of {VOICES} or 'eleven:<voice_id>'"})


_eleven_voices_cache: tuple[float, list[dict]] | None = None


async def _elevenlabs_voices() -> list[dict]:
    global _eleven_voices_cache
    if _eleven_voices_cache and time.time() - _eleven_voices_cache[0] < 300:
        return _eleven_voices_cache[1]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(f"{ELEVEN_API}/voices", headers={"xi-api-key": elevenlabs_key()}) as resp:
            if resp.status == 401 and "missing_permissions" in (await resp.text()):
                raise DubError("ELEVENLABS_NO_VOICES_READ", "The key lacks the Voices: Read permission.", 502)
            if resp.status == 401:
                raise DubError("ELEVENLABS_AUTH", "The ElevenLabs API key is invalid.", 502)
            if resp.status != 200:
                raise DubError("ELEVENLABS_ERROR", f"ElevenLabs voices: HTTP {resp.status}", 502)
            data = await resp.json()
    voices = [
        {"id": f"eleven:{v['voice_id']}", "name": v.get("name") or v["voice_id"], "provider": "elevenlabs",
         "category": v.get("category")}
        for v in data.get("voices", [])
    ]
    _eleven_voices_cache = (time.time(), voices)
    return voices


def _check_source(source: str) -> str:
    if source not in SOURCES:
        raise HTTPException(422, {"code": "BAD_SOURCE", "message": f"source must be one of {SOURCES}"})
    return source


def _existing_job(video_id: str, voice: str, source: str) -> Job:
    job = JOBS.get((video_id, _check_voice(voice), _check_source(source)))
    if job is None:
        raise HTTPException(404, {"code": "NO_JOB", "message": "No dubbing job for this video."})
    return job


@app.exception_handler(DubError)
async def dub_error_handler(_: Request, exc: DubError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "voices": VOICES, "default_voice": DEFAULT_VOICE,
            "stt_available": bool(os.getenv("ELEVENLABS_API_KEY"))}


@app.get("/voices")
async def voices() -> dict:
    """All selectable voices: free edge-tts ones, plus the ElevenLabs voices
    in your account when ELEVENLABS_API_KEY is set."""
    edge = [{"id": v, "name": v.split("-")[2].removesuffix("Neural"), "provider": "edge"} for v in VOICES]
    eleven: list[dict] = []
    status = {"configured": bool(os.getenv("ELEVENLABS_API_KEY")), "model": ELEVEN_MODEL, "error": None, "note": None}
    if status["configured"]:
        try:
            eleven = await _elevenlabs_voices()
        except DubError as exc:
            if exc.code == "ELEVENLABS_NO_VOICES_READ":
                # TTS still works: offer the shared premade voices instead of the account's list.
                eleven = [{"id": f"eleven:{vid}", "name": name, "provider": "elevenlabs", "category": "premade"}
                          for vid, name in ELEVEN_PREMADE.items()]
                status["note"] = "premade_only"
            else:
                status["error"] = exc.message
        except Exception as exc:
            status["error"] = f"Could not load ElevenLabs voices: {exc}"
    return {"voices": edge + eleven, "default_voice": DEFAULT_VOICE, "elevenlabs": status}


@app.get("/dub", response_class=FileResponse)
async def dub(video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE, source: str = "captions"):
    """Blocking endpoint: returns the complete dubbed MP3, generating it if
    needed. (Used by the Chrome extension; the web player streams clips.)"""
    voice, source = _check_voice(voice), _check_source(source)
    job = get_or_start_job(video_id, voice, source=source)
    if job.task is not None and not job.task.done():
        await asyncio.shield(job.task)  # client disconnect must not cancel the job
    if job.state == "error":
        raise DubError(job.error_code or "INTERNAL", job.error or "Dubbing failed", job.error_status)
    full = job_dir(video_id, voice, source) / "full.mp3"
    if not full.exists():
        raise DubError("INTERNAL", "Audio file missing after generation.", 500)
    return FileResponse(full, media_type="audio/mpeg", filename=f"{video_id}.uz.mp3",
                        headers={"Cache-Control": "no-store"})


@app.post("/dub/jobs", status_code=202)
async def start_job(video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE, source: str = "captions",
                    force: bool = False, t: float = 0.0) -> dict:
    """Start (or join) dubbing in the background, prioritising time `t`."""
    job = get_or_start_job(video_id, _check_voice(voice), force, focus=t, source=_check_source(source))
    job.set_focus(t)
    return job.public()


@app.get("/dub/jobs")
async def job_status(video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE, source: str = "captions") -> dict:
    return _existing_job(video_id, voice, source).public()


@app.post("/dub/focus")
async def focus(video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE, source: str = "captions",
                t: float = Query(..., ge=0)) -> dict:
    """The viewer jumped to `t` seconds: generate from there first."""
    job = _existing_job(video_id, voice, source)
    job.set_focus(t)
    return {"focus": job.focus}


@app.get("/dub/stream")
async def stream(request: Request, video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE,
                 source: str = "captions"):
    """Server-Sent Events: a `snapshot` first, then `segment` / `job` updates,
    ending with `done` or `error`."""
    job = _existing_job(video_id, voice, source)
    queue: asyncio.Queue = asyncio.Queue()
    job.subscribers.add(queue)

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def events():
        try:
            yield "retry: 2000\n\n"
            yield sse("snapshot", job.snapshot())
            if job.state in ("done", "error"):
                yield sse(job.state, job.public())
                return
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keep-alive\n\n"
                    continue
                yield sse(event, data)
                if event in ("done", "error"):
                    return
        finally:
            job.subscribers.discard(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/dub/clip", response_class=FileResponse)
async def clip(video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE, source: str = "captions",
               i: int = Query(..., ge=0)):
    """One voiced segment as MP3 (404 NOT_READY until it has been generated)."""
    path = clip_path(video_id, _check_voice(voice), _check_source(source), i)
    if not path.exists():
        raise HTTPException(404, {"code": "NOT_READY", "message": "This segment is not voiced yet."})
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/dub/segments")
async def segments(video_id: str = VideoIdQuery, voice: str = DEFAULT_VOICE, source: str = "captions") -> dict:
    """Current segments with their source/Uzbek text and status."""
    return _existing_job(video_id, voice, source).snapshot()


# The web player (backend/web) at http://127.0.0.1:8000/ — mounted last so the
# API routes above take precedence.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "9988")))
