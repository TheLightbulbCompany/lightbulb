import { expect, it, vi } from "vitest";
import { copyText, messagePermalink } from "../messageLinks";

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
