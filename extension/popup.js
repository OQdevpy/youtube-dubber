const $ = (id) => document.getElementById(id);
const els = {
  voice: $("voice"),
  toggle: $("toggle"),
  status: $("status"),
  progress: $("progress"),
  bar: $("progress").firstElementChild,
  error: $("error"),
  serverDot: $("server-dot"),
  serverText: $("server-text"),
};

let tab = null;
let current = { status: "idle" };

// Talk to the content script, injecting it if the tab was opened before the
// extension was installed/reloaded (declared content scripts don't run there).
async function sendToTab(message) {
  try {
    return await chrome.tabs.sendMessage(tab.id, message);
  } catch {
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
    return chrome.tabs.sendMessage(tab.id, message);
  }
}

function render(state) {
  current = state ?? { status: "idle" };
  const { status, message, progress, error } = current;
  const busy = status === "loading" || status === "playing";

  els.toggle.textContent = busy ? "Stop Dubbing" : "Start Uzbek Dubbing";
  els.toggle.classList.toggle("stop", busy);
  els.toggle.disabled = false;
  els.voice.disabled = busy;

  els.status.textContent = message || (status === "idle" ? "Ready." : "");
  els.progress.hidden = status !== "loading";
  els.bar.style.width = `${Math.round((progress ?? 0) * 100)}%`;

  els.error.hidden = !(status === "error" && error);
  els.error.textContent = error?.message ?? "";
}

function showFatal(message) {
  els.toggle.disabled = true;
  els.voice.disabled = true;
  els.status.textContent = message;
}

async function checkServer() {
  const res = await chrome.runtime.sendMessage({ type: "api:health" }).catch(() => null);
  const ok = Boolean(res?.ok);
  els.serverDot.className = `dot ${ok ? "ok" : "down"}`;
  els.serverText.textContent = ok ? "server online" : "server offline";
  els.serverDot.parentElement.title = ok ? "Local dubbing server is running" : res?.error?.message ?? "";
}

els.voice.addEventListener("change", () => {
  chrome.storage.local.set({ voice: els.voice.value });
});

els.toggle.addEventListener("click", async () => {
  els.toggle.disabled = true;
  const busy = current.status === "loading" || current.status === "playing";
  try {
    render(await sendToTab(busy ? { type: "dub:ui:stop" } : { type: "dub:ui:start", voice: els.voice.value }));
  } catch (err) {
    render({ status: "error", error: { message: `Could not reach the page: ${err.message}. Try refreshing it.` } });
  }
});

// Live updates pushed by the content script.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg?.type === "dub:state" && sender.tab?.id === tab?.id) render(msg.state);
});

(async function init() {
  const { voice } = await chrome.storage.local.get("voice");
  if (voice) els.voice.value = voice;

  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  checkServer();

  if (!tab?.url || !/^https?:\/\/([a-z0-9-]+\.)?youtube\.com\//.test(tab.url)) {
    return showFatal("Open a YouTube video to use dubbing.");
  }
  try {
    const state = await sendToTab({ type: "dub:ui:state" });
    if (!state.pageVideoId) return showFatal("Open a video (not the home page) to dub it.");
    render(state);
  } catch (err) {
    showFatal(`Cannot access this tab: ${err.message}`);
  }
})();
