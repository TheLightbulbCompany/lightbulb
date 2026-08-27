// Lightbulb AI container Chrome runs under a kiosk window manager that fullscreens
// every top-level window and offers no switcher: a page's window.open popup
// (an OAuth consent, typically) lands on top of the page and the owner
// watching the live view cannot get back. Chrome's own tab strip IS a
// switcher, so fold every new window into the first one as a tab instead.
// Moving the tab keeps its renderer, so window.opener / postMessage survive.
let mainWindowId = null;

async function ensureMain() {
  if (mainWindowId !== null) {
    try {
      await chrome.windows.get(mainWindowId);
      return mainWindowId;
    } catch {
      mainWindowId = null;
    }
  }
  const wins = await chrome.windows.getAll({ windowTypes: ["normal"] });
  mainWindowId = wins[0]?.id ?? null;
  return mainWindowId;
}

chrome.tabs.onCreated.addListener(async (tab) => {
  try {
    const main = await ensureMain();
    if (main === null || tab.windowId === main) return;
    const win = await chrome.windows.get(tab.windowId);
    if (win.type !== "popup" && win.type !== "normal") return; // devtools, app, panel: leave alone
    await chrome.tabs.move(tab.id, { windowId: main, index: -1 });
    await chrome.tabs.update(tab.id, { active: true });
    await chrome.windows.update(main, { focused: true });
  } catch (err) {
    // Best effort: a window that closed itself mid-move is not an error worth surfacing.
    console.debug("popups-to-tabs: skipped", err);
  }
});
