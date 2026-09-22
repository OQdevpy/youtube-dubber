"use strict";

// Web player: YouTube's own IFrame player + the Uzbek dub streamed from the
// local server clip by clip. Nothing is downloaded from YouTube.
//
// Flow: paste link → "Tarjima qilish" → POST /dub/jobs → EventSource
// /dub/stream (live segment statuses) → each voiced segment's clip is fetched
// and played when the video reaches that segment.

const $ = (id) => document.getElementById(id);

// Session-only settings: nothing is written to the browser's storage.
const settings = { orig: true, origVol: 20, dub: true, dubVol: 100, subEn: false, subUz: true, wait: true };

const ERRORS = {
  TRANSCRIPTS_DISABLED: "Bu videoda subtitrlar o'chirilgan, shuning uchun uni tarjima qilib bo'lmaydi.",
  NO_ENGLISH_TRANSCRIPT: "Bu videoda subtitr yo'q. «Ovozdan matn (ElevenLabs)» manbasini tanlang yoki .env ga ElevenLabs kalitini qo'shing.",
  VIDEO_UNAVAILABLE: "Video topilmadi yoki yopiq.",
  AGE_RESTRICTED: "Yosh cheklovi bor videolarni tarjima qilib bo'lmaydi.",
  YOUTUBE_BLOCKED: "YouTube bu serverning IP manzilidan so'rovlarni bloklagan. .env ga YOUTUBE_PROXY (proxy manzili) qo'shing.",
  EMPTY_TRANSCRIPT: "Subtitrlarda o'qiladigan matn yo'q.",
  GEMINI_KEY_MISSING: "Gemini kaliti sozlanmagan. backend/.env fayliga GEMINI_API_KEY ni yozing va serverni qayta ishga tushiring.",
  GEMINI_BILLING: "Gemini hisobingizdagi kreditlar tugagan. ai.studio/projects sahifasida billingni to'ldiring, keyin «Qayta urinish»ni bosing.",
  TRANSLATION_FAILED: "Gemini tarjima qila olmadi (ko'pincha bepul limit tugaganda). Bir daqiqadan keyin qayta urining.",
  ELEVENLABS_KEY_MISSING: "ElevenLabs kaliti sozlanmagan. backend/.env fayliga ELEVENLABS_API_KEY ni yozing yoki bepul ovozni tanlang.",
  ELEVENLABS_AUTH: "ElevenLabs kaliti noto'g'ri. backend/.env dagi ELEVENLABS_API_KEY ni tekshiring.",
  ELEVENLABS_QUOTA: "ElevenLabs'dagi belgilar limiti tugadi. Bepul ovozni tanlang yoki tarifni oshiring.",
  ELEVENLABS_PERMISSION: "ElevenLabs kalitida «Text to Speech» ruxsati yo'q. ElevenLabs sozlamalarida kalitga shu ruxsatni bering.",
  ELEVENLABS_VOICE: "Tanlangan ElevenLabs ovozi hisobingizda topilmadi.",
  ELEVENLABS_STT_PERMISSION: "ElevenLabs kalitida «Speech to Text» ruxsati yo'q. ElevenLabs sozlamalarida kalitga shu ruxsatni bering.",
  YOUTUBE_BOT_CHECK: "YouTube bu serverni bot deb hisoblab, video ovozini bermayapti (data-markaz IP'lari ko'pincha shunday bloklanadi). .env ga YOUTUBE_PROXY yoki YOUTUBE_COOKIES qo'shing, yoki «YouTube subtitrlari» manbasini sinab ko'ring.",
  AUDIO_FETCH_FAILED: "Videoning ovozini YouTube'dan olib bo'lmadi (matnga aylantirish uchun kerak edi). Birozdan keyin qayta urining.",
  STT_FAILED: "ElevenLabs ovozni matnga aylantira olmadi. Qayta urining.",
  TTS_FAILED: "O'zbekcha ovoz yaratib bo'lmadi. Internet aloqasini tekshirib, qayta urining.",
  NETWORK: "Server bilan aloqa yo'q. Loyiha papkasida «docker compose up -d» ni (yoki backend papkasida «python main.py» ni) ishga tushiring.",
};

function errorText(err) {
  if (!err) return "Noma'lum xatolik.";
  if (ERRORS[err.code]) return ERRORS[err.code];
  if (err.code === "GEMINI_ERROR") return `Gemini xatosi: ${err.message}`;
  if (err.code === "ELEVENLABS_ERROR") return `ElevenLabs xatosi: ${err.message}`;
  return err.message || "Noma'lum xatolik.";
}

// Where the dubbing server is. Normally the page is served by the server itself
// (API = same origin), but if it was opened some other way (an editor's live
// preview, a file) we look for the server on its usual local ports.
let API = "";

async function detectApi() {
  const servedOverHttp = location.protocol === "http:" || location.protocol === "https:";
  const candidates = servedOverHttp ? [""] : [];
  for (const port of [9988, 8000]) {
    const base = `http://127.0.0.1:${port}`;
    if (location.origin !== base) candidates.push(base);
  }
  for (const base of candidates) {
    try {
      const r = await fetch(base + "/health", { signal: AbortSignal.timeout(2000) });
      const j = await r.json();
      if (j?.status === "ok" && Array.isArray(j.voices)) {
        API = base;
        const stt = $("source").querySelector('option[value="stt"]');
        stt.disabled = !j.stt_available;
        if (!j.stt_available) stt.textContent = "Ovozdan matn (ElevenLabs kaliti kerak)";
        return true;
      }
    } catch {
      /* not our server: try the next one */
    }
  }
  return false;
}

async function api(method, path) {
  try {
    const resp = await fetch(API + path, { method });
    const body = await resp.json().catch(() => null);
    if (!resp.ok) {
      const d = body?.detail;
      const error = d && typeof d === "object" && !Array.isArray(d) && d.code ? d : { code: `HTTP_${resp.status}`, message: `Server xatosi (HTTP ${resp.status}).` };
      return { ok: false, error };
    }
    return { ok: true, data: body };
  } catch {
    return { ok: false, error: { code: "NETWORK" } };
  }
}

// --------------------------------------------------------------- link parsing

function parseVideoId(input) {
  const text = input.trim();
  if (/^[\w-]{11}$/.test(text)) return text;
  let url;
  try {
    url = new URL(/^[a-z]+:\/\//i.test(text) ? text : "https://" + text);
  } catch {
    return null;
  }
  const host = url.hostname.replace(/^(www|m|music)\./, "");
  let id = null;
  if (host === "youtu.be") id = url.pathname.slice(1, 12);
  else if (host === "youtube.com" || host === "youtube-nocookie.com") {
    id = url.searchParams.get("v") || (url.pathname.match(/^\/(?:shorts|embed|live|v)\/([\w-]{11})/) || [])[1];
  }
  return id && /^[\w-]{11}$/.test(id) ? id : null;
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
}

// ------------------------------------------------------------ YouTube player

let player = null;
let playerReady = null;

const ytApi = new Promise((resolve) => {
  window.onYouTubeIframeAPIReady = resolve;
  const tag = document.createElement("script");
  tag.src = "https://www.youtube.com/iframe_api";
  tag.onerror = () => showFormError("YouTube pleerini yuklab bo'lmadi. Internet aloqasini tekshiring.");
  document.head.append(tag);
});

const PLAYER_ERRORS = {
  2: "Havolada xato bor: video identifikatori noto'g'ri.",
  5: "YouTube pleeri bu videoni ocha olmadi.",
  100: "Video topilmadi, o'chirilgan yoki yopiq.",
  101: "Video egasi uni boshqa saytlarda ko'rsatishni taqiqlagan, uni faqat youtube.com'da ko'rish mumkin.",
  150: "Video egasi uni boshqa saytlarda ko'rsatishni taqiqlagan, uni faqat youtube.com'da ko'rish mumkin.",
  152: "YouTube bu sahifada pleerni ishga tushira olmadi. Sahifani http://127.0.0.1:9988/ manzilida oddiy brauzerda oching.",
  153: "YouTube sahifa manzilini ko'ra olmadi (xato 153). Sahifani fayl yoki IDE ichidan emas, oddiy brauzerda http://127.0.0.1:9988/ manzilida oching.",
};

function loadPlayer(videoId) {
  $("empty").hidden = true;
  if (playerReady) {
    // The player may still be initialising: its methods exist only after onReady.
    return playerReady.then(() => {
      if (player.getVideoData?.().video_id !== videoId) player.cueVideoById(videoId);
    });
  }
  playerReady = ytApi.then(
    () =>
      new Promise((resolve) => {
        player = new YT.Player("player", {
          videoId,
          width: "100%",
          height: "100%",
          playerVars: { playsinline: 1, rel: 0, fs: 0, cc_load_policy: 0, iv_load_policy: 3, origin: location.origin },
          events: {
            onReady: () => {
              hideYouTubeCaptions();
              applyOriginalVolume();
              resolve();
            },
            onStateChange: onPlayerState,
            onError: (e) => {
              showFormError(PLAYER_ERRORS[e.data] ?? `YouTube pleeri xatosi (${e.data}).`);
              $("go").disabled = false;
              resolve();
            },
          },
        });
      })
  );
  return playerReady;
}

// getCurrentTime() only updates a few times a second; extrapolate between
// updates so the dub doesn't "hear" fake drift.
const clock = { raw: -1, at: 0 };
function videoTime() {
  if (!player?.getCurrentTime) return 0;
  const t = player.getCurrentTime() || 0;
  const now = performance.now();
  if (t !== clock.raw) {
    clock.raw = t;
    clock.at = now;
  }
  if (player.getPlayerState() === YT.PlayerState.PLAYING) {
    return t + (Math.min(now - clock.at, 1000) / 1000) * (player.getPlaybackRate() || 1);
  }
  return t;
}

// YouTube may switch its own captions on (viewer prefs, auto-captions) and
// they would collide with our subtitle layer; our EN/UZ switches own subtitles.
function hideYouTubeCaptions() {
  try {
    player.unloadModule?.("captions");
    player.unloadModule?.("cc");
  } catch {
    /* module API unavailable: nothing to hide */
  }
}

function applyOriginalVolume() {
  if (!player?.setVolume) return;
  if (!settings.orig) return player.mute();
  player.unMute();
  // Full volume until a dub exists; the chosen level while dubbing.
  player.setVolume(session && settings.dub ? settings.origVol : 100);
}

// ----------------------------------------------------------------- session

let session = null;

function newSession(videoId, voice, source) {
  return {
    videoId, voice, source,
    segments: [], job: null, es: null, failed: false, startedAt: Date.now(),
    clips: new Map(), // i -> blob URL
    want: new Set(), fetching: new Set(),
    audios: new Map(), active: -1, lag: 0,
    autoPaused: false, resuming: false, waitIdx: -1, noWait: new Set(),
    lastT: 0, lastFocusAt: 0,
  };
}

function query(s) {
  return new URLSearchParams({ video_id: s.videoId, voice: s.voice, source: s.source }).toString();
}

async function startSession(videoId, voice, source) {
  endSession();
  const s = (session = newSession(videoId, voice, source));
  renderAll(s);
  applyOriginalVolume();
  const res = await api("POST", `/dub/jobs?${query(s)}&t=${videoTime().toFixed(1)}`);
  if (s !== session) return;
  if (!res.ok) return fail(s, res.error);
  s.job = res.data;
  renderProgress(s);
  openStream(s);
}

function endSession() {
  const s = session;
  session = null;
  if (!s) return;
  s.es?.close();
  for (const a of s.audios.values()) a.pause();
  for (const url of s.clips.values()) URL.revokeObjectURL(url);
  if (s.autoPaused) player?.playVideo?.();
  showWait(false);
}

function openStream(s) {
  const es = (s.es = new EventSource(`${API}/dub/stream?${query(s)}`));
  const on = (name, fn) => es.addEventListener(name, (e) => s === session && fn(e));

  on("snapshot", (e) => {
    const d = JSON.parse(e.data);
    s.job = d.job;
    s.segments = d.segments;
    renderAll(s);
    d.segments.forEach((seg) => seg.status === "ready" && queueClip(s, seg.i));
  });
  on("segment", (e) => {
    const seg = JSON.parse(e.data);
    s.segments[seg.i] = seg;
    renderSegment(s, seg.i);
    if (seg.status === "ready") queueClip(s, seg.i);
  });
  on("job", (e) => {
    s.job = JSON.parse(e.data);
    renderProgress(s);
  });
  on("done", (e) => {
    s.job = JSON.parse(e.data);
    es.close();
    renderProgress(s);
  });
  on("error", (e) => {
    if (e.data) {
      // our server's "error" event (a failed job)
      s.job = JSON.parse(e.data);
      es.close();
      fail(s, s.job.error);
    } else if (es.readyState === EventSource.CLOSED) {
      fail(s, { code: "NETWORK" });
    } // otherwise the browser reconnects by itself and gets a fresh snapshot
  });
}

function fail(s, error) {
  s.failed = true;
  s.error = error;
  if (s.autoPaused) {
    s.autoPaused = false;
    s.resuming = true;
    player?.playVideo?.();
  }
  showWait(false);
  renderProgress(s);
}

function sendFocus(s, t) {
  if (s.failed || s.job?.state === "done") return;
  s.lastFocusAt = performance.now();
  api("POST", `/dub/focus?${query(s)}&t=${t.toFixed(1)}`);
}

// ------------------------------------------------------------ clip loading

function priority(s, i, t) {
  const seg = s.segments[i];
  return seg.start + slotLength(s, i) > t ? seg.start - t : 1e6 + (t - seg.start);
}

function queueClip(s, i) {
  if (s.clips.has(i) || s.fetching.has(i)) return;
  s.want.add(i);
  pumpClips(s);
}

function pumpClips(s) {
  while (s === session && s.fetching.size < 4 && s.want.size) {
    let best = -1;
    for (const i of s.want) if (best < 0 || priority(s, i, s.lastT) < priority(s, best, s.lastT)) best = i;
    s.want.delete(best);
    s.fetching.add(best);
    fetch(`${API}/dub/clip?${query(s)}&i=${best}`)
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((blob) => {
        if (s !== session) return;
        s.clips.set(best, URL.createObjectURL(blob));
      })
      .catch(() => s === session && setTimeout(() => queueClip(s, best), 2000))
      .finally(() => {
        s.fetching.delete(best);
        pumpClips(s);
      });
  }
}

// ---------------------------------------------------------------- playback

function slotLength(s, i) {
  const segs = s.segments;
  return (i + 1 < segs.length ? segs[i + 1].start : segs[i].end + 1.5) - segs[i].start;
}

function segmentAt(segs, t) {
  let lo = 0, hi = segs.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (segs[mid].start <= t) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

// How far (seconds) one dubbed line may run into the next line's time.
const SPILL = 1.5;

function pauseActive(s) {
  if (s.active >= 0) s.audios.get(s.active)?.pause();
  s.active = -1;
}

function audioFor(s, i) {
  let a = s.audios.get(i);
  if (!a) {
    a = new Audio(s.clips.get(i));
    a.preload = "auto";
    a.preservesPitch = true;
    s.audios.set(i, a);
    // keep only a handful of decoded elements around
    for (const [j, el] of s.audios) if (Math.abs(j - i) > 4) (el.pause(), s.audios.delete(j));
  }
  return a;
}

function syncDub(s, t, playing) {
  const i = segmentAt(s.segments, t);
  const seg = s.segments[i];
  const offset = seg ? t - seg.start : 0;
  const slot = seg ? slotLength(s, i) : 0;

  // 1. Streaming buffer: pause the video until this segment's voice exists.
  const missing = seg && seg.status !== "empty" && !(seg.status === "ready" && s.clips.has(i)) && offset < slot;
  if (settings.dub && settings.wait && !s.failed && missing && !s.noWait.has(i)) {
    if (playing && !s.autoPaused) {
      s.autoPaused = true;
      s.waitIdx = i;
      player.pauseVideo();
      if (performance.now() - s.lastFocusAt > 1500) sendFocus(s, t);
    }
    showWait(s.autoPaused);
    pauseActive(s);
    return;
  }
  if (s.autoPaused) {
    s.autoPaused = false;
    s.resuming = true;
    showWait(false);
    player.playVideo();
    return;
  }

  // 2. Play the clip for the current segment at the right offset.
  if (!playing || !settings.dub) return pauseActive(s);
  const videoRate = player.getPlaybackRate() || 1;

  // A line that runs a little past the next line's start is allowed to finish
  // (like a live dubber would) instead of being cut off mid-word; the next
  // line then starts slightly late and the lag is absorbed by later pauses.
  const prev = s.active >= 0 && s.active < i ? s.audios.get(s.active) : null;
  if (prev && !prev.paused && !prev.ended) {
    const remaining = (prev.duration - prev.currentTime) / (prev.playbackRate || 1);
    const lateBy = seg ? offset : 0; // how far into the new line the video already is
    if (remaining > 0.05 && lateBy + remaining <= SPILL) {
      prev.volume = settings.dubVol / 100;
      return;
    }
  }

  if (!seg || seg.status !== "ready" || !s.clips.has(i)) return pauseActive(s);
  // A clip longer than its slot is sped up (pitch kept) by at most 10%; the
  // server already shortened the line, so bigger speed-ups would sound rushed.
  const stretch = seg.duration > slot ? Math.min(seg.duration / slot, 1.1) : 1;

  const a = audioFor(s, i);
  if (s.active !== i) {
    pauseActive(s);
    s.active = i;
    // Starting late because the previous line spilled over: play this line
    // from its beginning (a short delay) rather than skipping its first words.
    s.lag = offset > 0.05 && offset <= SPILL && prev ? offset : 0;
    a.currentTime = Math.max(0, (offset - s.lag) * stretch);
  }
  const clipOffset = (offset - s.lag) * stretch;
  if (clipOffset >= seg.duration - 0.03) return pauseActive(s);
  a.volume = settings.dubVol / 100;
  const rate = videoRate * stretch;
  if (Math.abs(a.playbackRate - rate) > 0.01) a.playbackRate = rate;
  if (Math.abs(a.currentTime - clipOffset) > 0.35) a.currentTime = Math.max(0, clipOffset);
  if (a.paused) {
    a.play().then(
      () => ($("unlock").hidden = true),
      (err) => err.name === "NotAllowedError" && ($("unlock").hidden = false)
    );
  }
  if (s.clips.has(i + 1)) audioFor(s, i + 1); // warm up the next clip
}

function onPlayerState(e) {
  if (e.data === YT.PlayerState.PLAYING) hideYouTubeCaptions();
  const s = session;
  if (!s || e.data !== YT.PlayerState.PLAYING) return;
  if (s.resuming) s.resuming = false;
  else if (s.autoPaused) {
    // The viewer pressed play while we were waiting: respect it.
    s.autoPaused = false;
    s.noWait.add(s.waitIdx);
    showWait(false);
  }
}

function tick() {
  const s = session;
  if (!s || !player?.getPlayerState) return;
  const t = videoTime();
  const playing = player.getPlayerState() === YT.PlayerState.PLAYING;

  if (Math.abs(t - s.lastT) > 2) {
    // a seek: generate from here first, and resync the dub from scratch
    s.noWait.clear();
    pauseActive(s);
    s.lag = 0;
    sendFocus(s, t);
  }
  s.lastT = t;

  syncDub(s, t, playing);
  renderSubtitles(s, t);
  renderPlayhead(s, t);
  renderCurrentLine(s, t);
}
setInterval(tick, 100);

// ---------------------------------------------------------------- rendering

function showFormError(text) {
  $("form-error").textContent = text;
  $("form-error").hidden = !text;
}

function showWait(on) {
  $("wait-pill").hidden = !on;
}

function renderAll(s) {
  $("timeline").hidden = false;
  renderProgress(s);
  renderLines(s);
  renderTicks(s);
}

const STATE_TITLES = {
  queued: "Navbatga qo'yildi",
  transcript: "Matn olinmoqda",
  working: "Tarjima va ovoz yaratilmoqda. Tayyor qismlarni hozirdan tomosha qilishingiz mumkin.",
  assembling: "Yakunlanmoqda",
  done: "Tayyor. Butun video o'zbekcha.",
  error: "Xatolik yuz berdi",
};

function setStep(id, state, count, fraction) {
  const li = $(`step-${id}`);
  li.dataset.state = state;
  $(`c-${id}`).textContent = count;
  const bar = $(`b-${id}`);
  if (bar) bar.style.width = `${Math.round((fraction || 0) * 100)}%`;
}

function renderProgress(s) {
  const job = s?.job;
  const c = job?.counts ?? { total: 0, translated: 0, voiced: 0 };
  const state = s?.failed ? "error" : job?.state ?? "queued";
  $("progress").dataset.state = state;
  $("progress-title").textContent = s ? STATE_TITLES[state] ?? state : "Hali video tanlanmagan";

  const early = state === "queued" || state === "transcript";
  const stt = (job?.used_source || s?.source) === "stt";
  $("n-captions").textContent = !s ? "Matn (subtitrlar)" : stt ? "Ovozdan matn (ElevenLabs STT)" : "YouTube subtitrlari";
  if (s && state === "transcript") {
    $("progress-title").textContent = stt ? "Video ovozi matnga aylantirilmoqda (ElevenLabs)" : "Subtitrlar olinmoqda";
  }
  setStep("captions", !s ? "idle" : early ? (s.failed ? "error" : "active") : "done", c.total ? `${c.total} ta bo'lak` : "", 0);
  const tState = (n) => (!c.total ? "idle" : n >= c.total ? "done" : s.failed ? "error" : "active");
  setStep("translate", tState(c.translated), c.total ? `${c.translated}/${c.total}` : "", c.total ? c.translated / c.total : 0);
  setStep("voice", tState(c.voiced), c.total ? `${c.voiced}/${c.total}` : "", c.total ? c.voiced / c.total : 0);

  let meta = "";
  if (s && state === "working" && c.voiced >= 3) {
    const elapsed = (Date.now() - s.startedAt) / 1000;
    const left = (elapsed / c.voiced) * (c.total - c.voiced);
    if (left >= 5) meta = `Taxminan ${fmtTime(left)} qoldi`;
  }
  $("progress-meta").textContent = meta;

  $("progress-error").hidden = !s?.failed;
  $("progress-error").textContent = s?.failed ? errorText(s.error) : "";
  $("retry").hidden = !s?.failed;
  $("go").disabled = false;
}

function renderLines(s) {
  const list = $("lines");
  list.replaceChildren(
    ...s.segments.map((seg) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "line";
      btn.dataset.i = seg.i;
      btn.innerHTML = `<time></time><span><span class="uz"></span><span class="en"></span></span>`;
      li.append(btn);
      fillLine(btn, seg);
      return li;
    })
  );
}

function fillLine(btn, seg) {
  btn.dataset.s = seg.status;
  btn.querySelector("time").textContent = fmtTime(seg.start);
  const uz = btn.querySelector(".uz");
  const en = btn.querySelector(".en");
  if (seg.uz) {
    uz.textContent = seg.uz;
    en.textContent = seg.en;
  } else {
    uz.textContent = seg.status === "translating" ? "Tarjima qilinmoqda…" : "Navbatda";
    en.textContent = seg.en;
  }
}

function renderTicks(s) {
  const dur = videoDuration(s);
  const box = $("ticks");
  box.replaceChildren(
    ...s.segments.map((seg) => {
      const d = document.createElement("div");
      d.className = "tick";
      d.dataset.s = seg.status;
      d.style.left = `${(seg.start / dur) * 100}%`;
      d.style.width = `${(slotLength(s, seg.i) / dur) * 100}%`;
      return d;
    })
  );
  box.dataset.dur = dur;
}

function renderSegment(s, i) {
  const btn = $("lines").querySelector(`.line[data-i="${i}"]`);
  if (btn) fillLine(btn, s.segments[i]);
  const tick = $("ticks").children[i];
  if (tick) tick.dataset.s = s.segments[i].status;
}

function videoDuration(s) {
  const d = player?.getDuration?.() || 0;
  const last = s.segments.at(-1);
  return Math.max(d, last ? last.end + 1 : 1);
}

function renderPlayhead(s, t) {
  const dur = videoDuration(s);
  if (Math.abs(dur - Number($("ticks").dataset.dur || 0)) > 1) renderTicks(s); // real duration arrived
  $("playhead").style.left = `${Math.min(100, (t / dur) * 100)}%`;
}

function renderSubtitles(s, t) {
  const i = segmentAt(s.segments, t);
  const seg = s.segments[i];
  const visible = seg && t < seg.start + Math.min(slotLength(s, i), Math.max(seg.end - seg.start, seg.duration) + 0.8);
  const en = visible && settings.subEn ? seg.en : "";
  const uz = visible && settings.subUz ? seg.uz : "";
  if ($("sub-en").textContent !== en) $("sub-en").textContent = en;
  if ($("sub-uz").textContent !== uz) $("sub-uz").textContent = uz;
}

let lastUserScroll = 0;
$("lines").addEventListener("wheel", () => (lastUserScroll = Date.now()), { passive: true });
$("lines").addEventListener("touchmove", () => (lastUserScroll = Date.now()), { passive: true });

function renderCurrentLine(s, t) {
  const i = segmentAt(s.segments, t);
  if (i === s.shownLine) return;
  s.shownLine = i;
  $("lines").querySelector('.line[aria-current="true"]')?.removeAttribute("aria-current");
  const btn = $("lines").querySelector(`.line[data-i="${i}"]`);
  if (!btn) return;
  btn.setAttribute("aria-current", "true");
  if (Date.now() - lastUserScroll > 4000) btn.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// -------------------------------------------------------------- interaction

function seekTo(t) {
  if (!player?.seekTo) return;
  player.seekTo(t, true);
  if (session) {
    session.noWait.clear();
    sendFocus(session, t);
  }
}

$("lines").addEventListener("click", (e) => {
  const btn = e.target.closest(".line");
  if (btn && session) seekTo(session.segments[Number(btn.dataset.i)].start + 0.05);
});

$("map").addEventListener("click", (e) => {
  if (!session) return;
  const rect = e.currentTarget.getBoundingClientRect();
  seekTo(((e.clientX - rect.left) / rect.width) * videoDuration(session));
});

$("link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  showFormError("");
  const id = parseVideoId($("link").value);
  if (!id) {
    showFormError("Bu YouTube havolasiga o'xshamaydi. Masalan: https://www.youtube.com/watch?v=jNQXAC9IVRw");
    $("link").focus();
    return;
  }
  const voice = $("voice").value;
  const source = $("source").value;
  history.replaceState(null, "", `?v=${id}`);
  $("go").disabled = true;
  // Don't let a slow or failing player hold up the dub: wait at most 8 s.
  const player$ = session?.videoId !== id || !playerReady ? loadPlayer(id) : playerReady;
  await Promise.race([player$, new Promise((r) => setTimeout(r, 8000))]);
  startSession(id, voice, source);
});

$("retry").addEventListener("click", () => session && startSession(session.videoId, session.voice, session.source));
$("unlock").addEventListener("click", () => {
  $("unlock").hidden = true;
  if (session) pauseActive(session); // next tick retries play() inside this click's activation
  tick();
});

function bindSwitch(id, key, after) {
  const btn = $(id);
  const paint = () => btn.setAttribute("aria-pressed", String(settings[key]));
  paint();
  btn.addEventListener("click", () => {
    settings[key] = !settings[key];
    paint();
    after?.();
  });
}

function bindSlider(id, out, key, after) {
  const input = $(id);
  const paint = () => ($(out).textContent = `${settings[key]}%`);
  input.value = settings[key];
  paint();
  input.addEventListener("input", () => {
    settings[key] = Number(input.value);
    paint();
    after?.();
  });
}

bindSwitch("t-orig", "orig", applyOriginalVolume);
bindSlider("v-orig", "o-orig", "origVol", applyOriginalVolume);
bindSwitch("t-dub", "dub", () => {
  applyOriginalVolume();
  if (!settings.dub && session) pauseActive(session);
});
bindSlider("v-dub", "o-dub", "dubVol");
bindSwitch("t-sub-en", "subEn");
bindSwitch("t-sub-uz", "subUz");
$("wait").checked = settings.wait;
$("wait").addEventListener("change", () => {
  settings.wait = $("wait").checked;
});

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else $("stage").requestFullscreen?.();
}
$("fullscreen").addEventListener("click", toggleFullscreen);
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "f" || e.key === "F") toggleFullscreen();
});

// ------------------------------------------------------------------ startup

async function loadVoices() {
  const select = $("voice");
  const found = await detectApi();
  if (found && API) {
    // Opened from a file or an editor preview: YouTube's player refuses such
    // pages (error 153), so continue on the server's own address.
    location.replace(`${API}/${location.search}`);
    return;
  }
  const res = found ? await api("GET", "/voices") : { ok: false };
  const server = $("server");
  server.dataset.state = res.ok ? "ok" : "down";
  $("server-text").textContent = res.ok ? "Server ishlayapti" : "Server o'chiq";
  if (!res.ok) {
    showFormError(
      found
        ? ERRORS.NETWORK
        : "Server topilmadi. backend papkasida serverni ishga tushiring va sahifani http://127.0.0.1:9988/ manzilida oching."
    );
    select.innerHTML = `<option value="uz-UZ-MadinaNeural">Madina</option><option value="uz-UZ-SardorNeural">Sardor</option>`;
    return;
  }
  const { voices, elevenlabs } = res.data;
  const group = (label, items) => {
    const g = document.createElement("optgroup");
    g.label = label;
    for (const v of items) g.append(new Option(v.name, v.id));
    return g;
  };
  const free = voices.filter((v) => v.provider === "edge");
  const eleven = voices.filter((v) => v.provider === "elevenlabs");
  select.replaceChildren(group("Bepul ovozlar", free));
  const eg = group("ElevenLabs", eleven);
  if (!elevenlabs.configured || elevenlabs.error || elevenlabs.note || !eleven.length) {
    const note = new Option(
      !elevenlabs.configured
        ? "Kalit qo'shilmagan (.env)"
        : elevenlabs.error
          ? "Ovozlarni yuklab bo'lmadi"
          : elevenlabs.note === "premade_only"
            ? "Standart ovozlar (kalitda Voices: Read yo'q)"
            : "Hisobda ovoz yo'q",
      ""
    );
    note.disabled = true;
    eg.append(note);
  }
  select.append(eg);
}

loadVoices();
renderProgress(null);

const initial = new URLSearchParams(location.search).get("v");
// A page opened as a file is about to move to the server's address (see
// loadVoices); YouTube's player can't run on file:// anyway.
if (initial && /^[\w-]{11}$/.test(initial) && location.protocol.startsWith("http")) {
  $("link").value = `https://www.youtube.com/watch?v=${initial}`;
  loadPlayer(initial);
}
