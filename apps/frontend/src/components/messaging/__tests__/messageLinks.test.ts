import { expect, it, vi } from "vitest";
import { copyText, messagePermalink, sessionPermalink, parseSessionPermalink } from "../messageLinks";

it("recognizes only exact same-origin session URLs", () => {
  const url = sessionPermalink("room-1", "session-1", "https://flickmeon.ai");
  expect(url).toBe("https://flickmeon.ai/chat?panel=session&room=room-1&session=session-1");
  expect(parseSessionPermalink(url, "https://flickmeon.ai")).toEqual({ roomId: "room-1", sessionId: "session-1" });
  for (const invalid of [url.replace("flickmeon.ai", "other.invalid"), `${url}&message=msg`, `${url}&session=second`, url.replace("/chat?", "/other?"), "javascript:alert(1)"]) {
    expect(parseSessionPermalink(invalid, "https://flickmeon.ai")).toBeNull();
  }
});

it("still accepts the legacy panel=chat grammar for session links minted before the dock panel shipped", () => {
  const legacy = "https://flickmeon.ai/chat?panel=chat&room=room-1&session=session-1";
  expect(parseSessionPermalink(legacy, "https://flickmeon.ai")).toEqual({ roomId: "room-1", sessionId: "session-1" });
});

it("builds a chat permalink", () => {
  expect(messagePermalink("room-1", "msg-1", "https://flickmeon.ai")).toBe("https://flickmeon.ai/chat?panel=chat&room=room-1&message=msg-1");
});

it("copies via the clipboard API and reports failure without throwing", async () => {
  const writeText = vi.fn(async () => {});
  Object.assign(navigator, { clipboard: { writeText } });
  await expect(copyText("x")).resolves.toBe(true);
  writeText.mockRejectedValueOnce(new Error("denied"));
  await expect(copyText("x")).resolves.toBe(false);
});
