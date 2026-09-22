// Service worker: the only place that talks to the local dubbing server.
//
// Requests go through here (not the content script) because extension
// requests with host_permissions are exempt from the page's CORS rules and
// from Chrome's "local network access" prompt for youtube.com -> localhost.

// 127.0.0.1 rather than "localhost": on some machines localhost resolves to
// IPv6 ::1 first, which may be a different server than the one uvicorn binds.
const API_BASE = "http://127.0.0.1:9988";

const OFFLINE = {
  code: "BACKEND_OFFLINE",
  message: `Cannot reach the dubbing server at ${API_BASE}. Start it with "python main.py".`,
};

async function api(path, { method = "GET", timeout = 15000 } = {}) {
  let resp;
  try {
    resp = await fetch(API_BASE + path, { method, signal: AbortSignal.timeout(timeout) });
  } catch (err) {
    if (err.name === "TimeoutError") {
      return { ok: false, error: { code: "TIMEOUT", message: "The dubbing server did not respond in time." } };
    }
    return { ok: false, error: OFFLINE };
  }
  let body = null;
  try {
    body = await resp.json();
  } catch {
    /* non-JSON error page */
  }
  if (!resp.ok) {
    const detail = body?.detail;
    const error =
      detail && typeof detail === "object" && !Array.isArray(detail) && detail.code
        ? detail
        : { code: `HTTP_${resp.status}`, message: `Server error (HTTP ${resp.status}).` };
    return { ok: false, error };
  }
  return { ok: true, data: body };
}

function query(videoId, voice, extra = {}) {
  return "?" + new URLSearchParams({ video_id: videoId, voice, ...extra });
}

// Messages carry JSON only, so the MP3 travels to the content script as base64.
function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

async function fetchAudio(videoId, voice) {
  try {
    const resp = await fetch(API_BASE + "/dub" + query(videoId, voice), {
      signal: AbortSignal.timeout(10 * 60 * 1000),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => null);
      return { ok: false, error: body?.detail?.code ? body.detail : { code: `HTTP_${resp.status}`, message: "Failed to download audio." } };
    }
    const buffer = await resp.arrayBuffer();
    return { ok: true, data: { base64: toBase64(buffer), mime: "audio/mpeg", bytes: buffer.byteLength } };
  } catch {
    return { ok: false, error: OFFLINE };
  }
}

const BADGE = {
  loading: { text: "…", color: "#6b7280" },
  playing: { text: "UZ", color: "#0ea5e9" },
  error: { text: "!", color: "#dc2626" },
};

function updateBadge(tabId, status) {
  if (tabId == null) return;
  const badge = BADGE[status];
  chrome.action.setBadgeText({ tabId, text: badge ? badge.text : "" });
  if (badge) chrome.action.setBadgeBackgroundColor({ tabId, color: badge.color });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handlers = {
    "api:health": () => api("/health", { timeout: 3000 }),
    "api:start": () => api("/dub/jobs" + query(msg.videoId, msg.voice), { method: "POST" }),
    "api:status": () => api("/dub/jobs" + query(msg.videoId, msg.voice)),
    "api:audio": () => fetchAudio(msg.videoId, msg.voice),
  };

  if (msg?.type === "dub:state") {
    updateBadge(sender.tab?.id, msg.state?.status);
    return false; // broadcast for the popup; no reply needed
  }
  const handler = handlers[msg?.type];
  if (!handler) return false;
  handler()
    .then(sendResponse)
    .catch((err) => sendResponse({ ok: false, error: { code: "INTERNAL", message: String(err) } }));
  return true; // keep the channel open for the async reply
});
