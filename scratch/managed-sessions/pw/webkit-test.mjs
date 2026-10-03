// Drive the terminal page in WebKit (Safari's engine) against the held Claude session.
import { webkit } from "playwright";
import fs from "fs";
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const results = {};
const browser = await webkit.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
await page.goto("http://localhost:8765/");
await sleep(2500);
const screen = () => page.evaluate(() => { const b = term.buffer.active; const out = [];
  for (let i = b.viewportY; i < b.viewportY + term.rows; i++) out.push(b.getLine(i)?.translateToString(true) ?? ""); return out.join("\n"); });
const pid = Number(fs.readFileSync("/dev/stdin", "utf8").trim() || 0);
const status = () => { try { return JSON.parse(fs.readFileSync(`${process.env.HOME}/.claude/sessions/${pid}.json`)).status; } catch { return "?"; } };
const clear = async () => { for (let i = 0; i < 4; i++) { await page.keyboard.press("Control+u"); await sleep(120); } };
let s = await screen();
if (s.includes("trust this folder")) { await page.keyboard.press("ArrowDown"); await page.keyboard.press("Enter"); await sleep(6000); s = await screen(); }
results.rendered = /Claude Code|❯|>/.test(s); results.size = await page.evaluate(() => `${term.cols}x${term.rows}`);
await page.click("#t"); await clear();
// Shift+Enter, as xterm.js sends it by default
await page.keyboard.type("line one"); await page.keyboard.press("Shift+Enter"); await page.keyboard.type("line two"); await sleep(1500);
s = await screen(); results.shiftEnter_default = { status: status(), twoLinesInPrompt: s.includes("line one") && s.includes("line two") && !/line one[\s\S]*⏺/.test(s) };
await page.keyboard.press("Escape"); await sleep(500); await clear(); await sleep(2500);
// Option+Enter (ESC CR), what terminal-setup maps Shift+Enter to
await page.keyboard.type("line one"); await page.keyboard.press("Alt+Enter"); await page.keyboard.type("line two"); await sleep(1500);
s = await screen(); results.optionEnter = { status: status(), twoLinesInPrompt: /line one\s*\n.*line two/.test(s) };
await clear(); await clear();
// Option+B word jump (macOptionIsMeta)
await page.keyboard.type("alpha beta gamma"); await page.keyboard.press("Alt+b"); await page.keyboard.type("X"); await sleep(800);
s = await screen(); results.optionB = s.includes("alpha beta Xgamma"); await clear();
// Bracketed paste of three lines (the path Cmd+V takes in xterm.js)
await page.evaluate(() => term.paste("p1 first\np2 second\np3 third")); await sleep(1500);
s = await screen(); results.paste = { status: status(), screenHas: (s.match(/Pasted text[^\n]*|p1 first|p3 third/g) || []).slice(0, 3) };
await clear(); await clear();
// Slash menu and arrows (never Enter)
await page.keyboard.type("/"); await sleep(1200); s = await screen(); results.slashMenu = /\/(help|clear|compact|model)/.test(s);
await page.keyboard.press("ArrowDown"); await sleep(300); await page.keyboard.press("Escape"); await sleep(300); await clear();
// Resize
await page.setViewportSize({ width: 760, height: 520 }); await page.evaluate(() => dispatchEvent(new Event("resize"))); await sleep(1500);
s = await screen(); results.afterResize = { size: await page.evaluate(() => `${term.cols}x${term.rows}`), promptBoxes: (s.match(/╭|─{20,}/g) || []).length };
fs.writeFileSync("webkit-after-resize.txt", s);
await browser.close();
console.log(JSON.stringify(results, null, 1));
