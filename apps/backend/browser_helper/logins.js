// The workspace's website logins in the shared browser, for people. Chrome's own password
// manager is off (policy): AWS keeps no Chrome profile between sessions, so its saves would
// vanish. Instead: after a sign-in, "Save this login for the workspace?"; on a sign-in form
// with a saved login, "Fill" (the backend types it in; no password ever reaches this script).
// Both go through sw.js -- this script never holds the token.
(() => {
  if (window.top !== window || window.__lightbulbLogins) return;
  window.__lightbulbLogins = true;

  const ask = (msg) => chrome.runtime.sendMessage(msg).catch(() => null);
  const host = location.host;
  let css;
  let fonts;
  let bar;

  const close = () => { bar?.remove(); bar = null; };

  async function show(title, note, buttons) {
    css ??= await fetch(chrome.runtime.getURL("bar.css")).then((r) => r.text());
    // @font-face is ignored inside a shadow root, and a page's CSP may refuse our font URL:
    // hand the document the font's bytes instead.
    fonts ??= Promise.all([400, 600].map(async (weight) => {
      const bytes = await fetch(chrome.runtime.getURL(`source-sans-3-${weight}.woff2`)).then((r) => r.arrayBuffer());
      document.fonts.add(await new FontFace("LB Source Sans 3", bytes, { weight: String(weight) }).load());
    })).catch(() => {});
    close();
    bar = document.createElement("lightbulb-bar");
    const root = bar.attachShadow({ mode: "closed" });
    root.innerHTML = `<style>${css}</style><div class="card floating" role="dialog" aria-label="Lightbulb">
      <p class="title"></p><p class="note"></p><div class="row"></div></div>`;
    root.querySelector(".title").textContent = title;
    const noteEl = root.querySelector(".note");
    noteEl.textContent = note;
    for (const { label, primary, run } of buttons) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = primary ? "primary" : "secondary";
      button.textContent = label;
      button.addEventListener("click", async () => {
        if (!run) return close();
        root.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        const error = await run();
        if (!error) return close();
        noteEl.textContent = error;
        noteEl.classList.add("error");
        root.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      });
      root.querySelector(".row").append(button);
    }
    document.documentElement.append(bar);
  }

  const passwordField = () => [...document.querySelectorAll('input[type="password"]')]
    .find((el) => el.offsetParent !== null && !el.disabled);
  // The username is the last visible text-like field before the password in the same form.
  function usernameField(password) {
    const scope = password?.form || document;
    const fields = [...scope.querySelectorAll('input[type="email"], input[type="text"], input[type="tel"], input:not([type])')]
      .filter((el) => el.offsetParent !== null && !el.disabled);
    const before = fields.filter((el) => password && el.compareDocumentPosition(password) & Node.DOCUMENT_POSITION_FOLLOWING);
    return before.at(-1) || fields.find((el) => /user|email|login/i.test(`${el.name} ${el.id} ${el.autocomplete}`));
  }
  // ---- save: remember a submitted login, offer it on the next page (or this one, for SPAs) ----
  let captured = 0;
  function capture() {
    const password = passwordField();
    if (!password?.value || Date.now() - captured < 1000) return;
    captured = Date.now();
    void ask({ type: "submitted", username: usernameField(password)?.value?.trim() || "", password: password.value });
    setTimeout(offerSave, 2500);
  }
  document.addEventListener("submit", capture, true);
  document.addEventListener("click", (event) => {
    if (event.target.closest?.('button, input[type="submit"], [role="button"]') && passwordField()?.value) capture();
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target instanceof HTMLInputElement && passwordField()?.value) capture();
  }, true);

  async function offerSave() {
    // Still looking at a password field: the sign-in most likely failed. The next page asks.
    if (passwordField()) return false;
    const reply = await ask({ type: "pending" });
    const save = reply?.ok && reply.save;
    if (!save) return false;
    const site = save.origin.replace(/^https?:\/\//, "");
    await show(`Save this login for the workspace?`, `${save.username} on ${site}. Everyone in the workspace, agents included, can sign in with it. The password is stored in Website sign-ins, never in chat.`, [
      { label: "Save", primary: true, run: async () => {
        const r = await ask({ type: "save" });
        return r?.ok ? null : r?.error || "Couldn't save the login. Try again.";
      } },
      { label: "Not now", run: async () => { await ask({ type: "dismiss" }); return null; } },
    ]);
    return true;
  }

  // ---- fill: on a sign-in form with saved logins, the backend types one in on a person's click ----
  async function offerFill() {
    if (!passwordField()) return;
    const reply = await ask({ type: "logins" });
    const logins = (reply?.ok && reply.logins) || [];
    if (!logins.length) return;
    await show(`Sign in to ${host}`, "From the workspace's Website sign-ins.", [
      ...logins.slice(0, 3).map((login, i) => ({ label: `Fill ${login.username}`, primary: i === 0, run: async () => {
        const r = await ask({ type: "fill", id: login.id });
        return r?.ok ? null : r?.error || "Couldn't fill the login. Try again.";
      } })),
      { label: "Not now" },
    ]);
  }

  // A slow page after the sign-in can be checked before the submit reached sw.js: look once more.
  void offerSave()
    .then((shown) => shown || new Promise((r) => setTimeout(r, 1500)).then(offerSave))
    .then((shown) => shown || offerFill());
})();
