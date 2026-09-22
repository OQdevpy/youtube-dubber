// Content script: ducks the YouTube video, fetches the Uzbek dub (via the
// service worker) and keeps the dub in sync with the video's clock.
//
// The dub is ONE MP3 whose timeline matches the video, so syncing is simply
// "audio.currentTime follows video.currentTime": play/pause/seek/speed
// changes and ads are all mirrored by syncAudio().

(() => {
  // After an extension reload, this script may be injected into a page that
  // still has an orphaned copy. Replace the orphan, but never run twice.
  if (window.__uzDubAlive?.()) return;
  window.__uzDubShutdown?.();

  const DUCK_VOLUME = 0.2; // original audio level while dubbing (20%)
  const POLL_MS = 1500;
  const MAX_DRIFT = 0.3; // seconds before we hard-resync the dub
  const ACTIVE = new Set(["loading", "playing"]);

  class Cancelled extends Error {}

  let session = null;
  let state = { status: "idle", message: "", progress: 0, videoId: null, voice: null, error: null };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const contextAlive = () => Boolean(chrome.runtime?.id);

  function getVideoId() {
    const url = new URL(location.href);
    if (url.pathname === "/watch") return url.searchParams.get("v");
    const match = url.pathname.match(/^\/(?:shorts|live|embed)\/([\w-]{11})/);
    return match ? match[1] : null;
  }

  function getVideo() {
    return (
      document.querySelector("#movie_player video.html5-main-video") ||
      document.querySelector("video.html5-main-video") ||
      document.querySelector("video")
    );
  }

  function isAdShowing() {
    return Boolean(document.querySelector(".html5-video-player.ad-showing"));
  }

  function publish(patch) {
    state = { ...state, ...patch };
    if (!contextAlive()) return;
    chrome.runtime.sendMessage({ type: "dub:state", state }).catch(() => {});
  }

  // Ask the service worker; aborts quietly if this session was superseded.
  async function bg(msg, s) {
    let res;
    try {
      res = await chrome.runtime.sendMessage(msg);
    } catch {
      res = {
        ok: false,
        error: { code: "EXTENSION_RELOADED", message: "The extension was updated. Refresh this page." },
      };
    }
    if (s !== session) throw new Cancelled();
    return res ?? { ok: false, error: { code: "INTERNAL", message: "No response from the extension." } };
  }

  function on(s, target, event, handler) {
    target.addEventListener(event, handler);
    s.listeners.push(() => target.removeEventListener(event, handler));
  }

  // ------------------------------------------------------------------ ducking

  function duck(s) {
    s.originalVolume = s.video.volume;
    s.duckTo = Math.min(s.originalVolume, DUCK_VOLUME);
    s.video.volume = s.duckTo;
  }

  function unduck(s) {
    if (s.originalVolume != null && s.video.isConnected) s.video.volume = s.originalVolume;
  }

  // ---------------------------------------------------------------- lifecycle

  async function start(voice) {
    const videoId = getVideoId();
    const video = getVideo();
    if (!videoId || !video) {
      publish({
        status: "error",
        error: { code: "NOT_A_VIDEO", message: "Open a YouTube video page first." },
        message: "",
      });
      return;
    }
    if (session && session.videoId === videoId && session.voice === voice && ACTIVE.has(session.status)) {
      return; // already dubbing this video
    }
    stop({ quiet: true });

    const s = (session = { videoId, voice, video, status: "loading", audio: null, url: null, listeners: [] });
    publish({ status: "loading", videoId, voice, progress: 0, message: "Starting…", error: null });
    duck(s);

    try {
      let res = await bg({ type: "api:start", videoId, voice }, s);
      for (;;) {
        if (!res.ok) throw res.error;
        const job = res.data;
        if (job.state === "error") throw job.error ?? { code: "INTERNAL", message: job.message };
        if (job.state === "done") break;
        publish({ progress: job.progress, message: job.message });
        await sleep(POLL_MS);
        if (s !== session) throw new Cancelled();
        res = await bg({ type: "api:status", videoId, voice }, s);
      }

      publish({ progress: 1, message: "Downloading audio…" });
      const audioRes = await bg({ type: "api:audio", videoId, voice }, s);
      if (!audioRes.ok) throw audioRes.error;
      await attachAudio(s, audioRes.data);
    } catch (err) {
      if (err instanceof Cancelled) return;
      const error = err?.code ? err : { code: "INTERNAL", message: String(err?.message ?? err) };
      teardown(s);
      if (session === s) session = null;
      publish({ status: "error", error, message: "" });
    }
  }

  async function attachAudio(s, { base64, mime }) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    s.url = URL.createObjectURL(new Blob([bytes], { type: mime }));

    const audio = (s.audio = new Audio(s.url));
    audio.preload = "auto";
    await new Promise((resolve, reject) => {
      const fail = (message) => reject({ code: "AUDIO_LOAD", message });
      const timer = setTimeout(() => fail("The dubbed audio took too long to load."), 20000);
      audio.addEventListener("loadedmetadata", () => (clearTimeout(timer), resolve()), { once: true });
      audio.addEventListener("error", () => (clearTimeout(timer), fail("The browser could not decode the dubbed audio.")), {
        once: true,
      });
    });
    if (s !== session) throw new Cancelled();

    const sync = () => syncAudio(s);
    for (const event of ["play", "playing", "pause", "waiting", "seeking", "seeked", "ratechange", "timeupdate", "ended", "volumechange"]) {
      on(s, s.video, event, sync);
    }
    s.status = "playing";
    publish({ status: "playing", progress: 1, message: "Dubbing in Uzbek", error: null });
    syncAudio(s, true);
  }

  function syncAudio(s, force = false) {
    const { video, audio } = s;
    if (!audio || s !== session) return;

    // YouTube re-applies its own volume after ads / quality switches.
    if (video.volume > s.duckTo + 0.001) video.volume = s.duckTo;

    const videoPlaying = !video.paused && !video.ended && video.readyState >= 3 && !isAdShowing();
    if (!videoPlaying || video.currentTime >= audio.duration) {
      if (!audio.paused) audio.pause();
      return;
    }
    if (audio.playbackRate !== video.playbackRate) audio.playbackRate = video.playbackRate;
    if (force || Math.abs(audio.currentTime - video.currentTime) > MAX_DRIFT) {
      audio.currentTime = video.currentTime;
    }
    if (audio.paused) audio.play().catch((err) => onPlayError(s, err));
  }

  function onPlayError(s, err) {
    if (err?.name !== "NotAllowedError" || s !== session) return; // AbortError: interrupted by a pause
    publish({ message: "Click anywhere on the page to enable the Uzbek audio." });
    on(s, document, "pointerdown", function retry() {
      document.removeEventListener("pointerdown", retry);
      publish({ message: "Dubbing in Uzbek" });
      syncAudio(s, true);
    });
  }

  function teardown(s) {
    s.listeners.forEach((off) => off());
    s.listeners = [];
    if (s.audio) {
      s.audio.pause();
      s.audio.removeAttribute("src");
      s.audio.load();
    }
    if (s.url) URL.revokeObjectURL(s.url);
    unduck(s);
    s.status = "stopped";
  }

  function stop({ quiet = false, message = "Stopped" } = {}) {
    const s = session;
    session = null;
    if (s) teardown(s);
    if (!quiet) publish({ status: "idle", progress: 0, message, error: null });
  }

  // YouTube is a single-page app: stop when the user navigates to another video.
  const navTimer = setInterval(() => {
    if (!contextAlive()) return shutdown();
    if (session && getVideoId() !== session.videoId) {
      stop({ message: "Video changed. Press Start to dub this one." });
    }
  }, 1000);

  function shutdown() {
    clearInterval(navTimer);
    stop({ quiet: true });
  }

  window.__uzDubAlive = contextAlive;
  window.__uzDubShutdown = shutdown;

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    switch (msg?.type) {
      case "dub:ui:start":
        start(msg.voice);
        break;
      case "dub:ui:stop":
        stop();
        break;
      case "dub:ui:state":
        break;
      default:
        return false;
    }
    sendResponse({ ...state, pageVideoId: getVideoId() });
    return false;
  });
})();
