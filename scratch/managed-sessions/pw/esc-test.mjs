import { webkit } from "playwright";
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const browser = await webkit.launch();
const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
await page.goto("http://localhost:8765/"); await sleep(3000);
const screen = () => page.evaluate(() => { const b = term.buffer.active; const out = [];
  for (let i = b.viewportY; i < b.viewportY + term.rows; i++) out.push(b.getLine(i)?.translateToString(true) ?? ""); return out.join("\n"); });
await page.click("#t");
await page.keyboard.press("Control+e"); for (let i = 0; i < 4; i++) { await page.keyboard.press("Control+u"); await sleep(100); }
await page.keyboard.type("Write the numbers from 1 to 2000, one per line, nothing else."); await page.keyboard.press("Enter");
await sleep(5000);
const before = await screen();
await page.keyboard.press("Escape"); await sleep(2500);
const after = await screen();
console.log(JSON.stringify({ wasRunning: /esc to interrupt|\d{2,}/.test(before), interrupted: /Interrupted/.test(after.split("Write the numbers").pop()) }));
await page.keyboard.type("/exit"); await sleep(600); await page.keyboard.press("Enter"); await sleep(2500);
console.log("tail:", (await screen()).split("\n").filter(l => l.trim()).slice(-3).join(" | ").slice(0, 200));
await browser.close();
