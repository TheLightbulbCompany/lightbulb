// The helper's only door to the backend. config.json (baked in by the backend at every session
// start, not web-accessible) holds the API base and a short-lived token that stays here: the
// content script and the popup ask by message, and the origin a login is for comes from
// Chrome's `sender`, never from the page.
let config;
const settings = async () => (config ??= await (await fetch(chrome.runtime.getURL("config.json"))).json());

async function call(path, body) {
  const { api, token } = await settings();
  const response = await fetch(api + path, {
    method: body ? "POST" : "GET",
    headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.");
    error.status = response.status;
    throw error;
  }
  return data;
}

// Chrome lets no extension script the Web Store, so the offer is our popup, opened on the page.
const STORE = /^https:\/\/chromewebstore\.google\.com\/detail\/(?:[^/]+\/)?([a-p]{32})/;
chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (change.status === "complete" && tab.active && STORE.test(tab.url || "")) {
    // openPopup can refuse without a user gesture; then the same page opens as a small window.
    chrome.action.openPopup({ windowId: tab.windowId }).catch(() => {
      const url = chrome.runtime.getURL(`popup.html?${new URLSearchParams({ url: tab.url, title: tab.title || "" })}`);
      chrome.windows.create({ url, type: "popup", width: 360, height: 220 }).catch(() => {});
    });
  }
});

// A submitted login waits here (memory only) for the "Save?" bar on the next page.
const PENDING_MS = 60_000;
const pendingKey = (tab) => `pending:${tab}`;

async function handle(msg, sender) {
  // Our own popup (action popup or its window fallback), never a content script on a page.
  const fromPage = !(sender.url || "").startsWith(chrome.runtime.getURL(""));
  const origin = sender.origin;
  const tab = sender.tab?.id;
  switch (msg?.type) {
    case "install":
      if (fromPage) throw new Error("Not allowed.");
      return call("/extensions", { idOrUrl: msg.id });
    case "logins":
      return call(`/logins?origin=${encodeURIComponent(origin)}`);
    case "fill":
      // The backend types it into this tab itself; no password ever comes back here.
      return call("/logins/fill-request", { id: msg.id });
    case "submitted":
      if (!msg.username || !msg.password) return {};
      await chrome.storage.session.set({
        [pendingKey(tab)]: { origin, username: msg.username, password: msg.password, at: Date.now() },
      });
      return {};
    case "pending": {
      const pending = (await chrome.storage.session.get(pendingKey(tab)))[pendingKey(tab)];
      if (!pending || Date.now() - pending.at > PENDING_MS) return {};
      const { logins } = await call(`/logins?origin=${encodeURIComponent(pending.origin)}`);
      if (logins.some((login) => login.username === pending.username)) {
        await chrome.storage.session.remove(pendingKey(tab));
        return {};
      }
      return { save: { origin: pending.origin, username: pending.username } };
    }
    case "save": {
      const pending = (await chrome.storage.session.get(pendingKey(tab)))[pendingKey(tab)];
      // The captured password lives only PENDING_MS, on this path too: a retry later must not
      // land in some future window of a person's input.
      if (!pending || Date.now() - pending.at > PENDING_MS) {
        await chrome.storage.session.remove(pendingKey(tab));
        throw new Error("Nothing to save.");
      }
      try {
        const saved = await call("/logins", { origin: pending.origin, username: pending.username, password: pending.password });
        await chrome.storage.session.remove(pendingKey(tab));
        return saved;
      } catch (error) {
        // 403: no person clicked Save, so whoever pressed it does not get another try. Anything
        // else (409 while an agent holds the browser, a network fault) keeps it for a retry
        // within PENDING_MS.
        if (error.status === 403) await chrome.storage.session.remove(pendingKey(tab));
        throw error;
      }
    }
    case "dismiss":
      await chrome.storage.session.remove(pendingKey(tab));
      return {};
    default:
      throw new Error("Unknown request.");
  }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  handle(msg, sender).then(
    (value) => reply({ ok: true, ...value }),
    (error) => reply({ ok: false, error: error.message }),
  );
  return true;
});
