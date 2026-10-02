import { expect, it, vi } from "vitest";
import { copyText, messagePermalink, sessionPermalink, parseSessionPermalink } from "../messageLinks";

it("recognizes only exact same-origin session URLs", () => {
  const url = sessionPermalink("session-1", "https://thelightbulb.company");
  expect(url).toBe("https://thelightbulb.company/chat?panel=session&session=session-1");
  expect(parseSessionPermalink(url, "https://thelightbulb.company")).toEqual({ sessionId: "session-1" });
  for (const invalid of [url.replace("thelightbulb.company", "other.invalid"), `${url}&message=msg`, `${url}&session=second`, url.replace("/chat?", "/other?"), "javascript:alert(1)"]) {
    expect(parseSessionPermalink(invalid, "https://thelightbulb.company")).toBeNull();
  }
});

it("still accepts room-scoped and legacy panel=chat session links, ignoring the room", () => {
  for (const legacy of ["https://thelightbulb.company/chat?panel=session&room=room-1&session=session-1", "https://thelightbulb.company/chat?panel=chat&room=room-1&session=session-1"]) {
    expect(parseSessionPermalink(legacy, "https://thelightbulb.company")).toEqual({ sessionId: "session-1" });
  }
});

it("builds a chat permalink", () => {
  expect(messagePermalink("room-1", "msg-1", "https://thelightbulb.company")).toBe("https://thelightbulb.company/chat?panel=chat&room=room-1&message=msg-1");
});

it("copies via the clipboard API and reports failure without throwing", async () => {
  const writeText = vi.fn(async () => {});
  Object.assign(navigator, { clipboard: { writeText } });
  await expect(copyText("x")).resolves.toBe(true);
  writeText.mockRejectedValueOnce(new Error("denied"));
  await expect(copyText("x")).resolves.toBe(false);
});
