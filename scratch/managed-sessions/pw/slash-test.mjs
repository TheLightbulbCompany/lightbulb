import { webkit } from "playwright";
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const browser = await webkit.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
await page.goto("http://localhost:8765/"); await sleep(3000);
const screen = () => page.evaluate(() => { const b = term.buffer.active; const out = [];
  for (let i = b.viewportY; i < b.viewportY + term.rows; i++) out.push(b.getLine(i)?.translateToString(true) ?? ""); return out.join("\n"); });
await page.click("#t"); for (let i = 0; i < 4; i++) { await page.keyboard.press("Control+u"); await sleep(100); }
await page.keyboard.type("/"); await sleep(1500);
const s = await screen();
console.log(s.split("\n").filter(l => l.trim()).slice(-14).join("\n"));
await page.keyboard.press("ArrowDown"); await sleep(400);
const s2 = await screen(); console.log("--- after ArrowDown, selection moved:", s !== s2);
await page.keyboard.press("Escape"); await sleep(300); await page.keyboard.press("Control+u");
await browser.close();
