// Opened by sw.js on a Chrome Web Store page (the store can't be scripted, so this is the offer).
const STORE = /^https:\/\/chromewebstore\.google\.com\/detail\/(?:[^/]+\/)?([a-p]{32})/;
const $ = (id) => document.getElementById(id);

chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
  const id = STORE.exec(tab?.url || "")?.[1];
  if (!id) return;
  const name = (tab.title || "").replace(/\s*-\s*Chrome Web Store\s*$/i, "").trim() || "This extension";
  $("title").textContent = name;
  $("note").textContent = "Add it to the workspace's shared browser. Everyone, agents included, gets it the next time the browser starts; it restarts now if nobody is using it.";
  $("actions").hidden = false;
  $("add").addEventListener("click", async () => {
    $("add").disabled = true;
    $("add").textContent = "Adding…";
    const reply = await chrome.runtime.sendMessage({ type: "install", id }).catch(() => null);
    $("note").classList.toggle("error", !reply?.ok);
    if (!reply?.ok) {
      $("note").textContent = reply?.error || "Couldn't add that extension. Try again.";
      $("add").disabled = false;
      $("add").textContent = "Add to the shared browser";
      return;
    }
    $("actions").hidden = true;
    $("note").textContent = reply.status === "requested"
      ? "An agent is using the browser, so a person has to confirm this. It's waiting in Activity."
      : reply.restarting
        ? `Added ${reply.name}. The browser is restarting to load it; sign-ins are saved first.`
        : `Added ${reply.name}. It loads the next time the browser starts.`;
  });
});
