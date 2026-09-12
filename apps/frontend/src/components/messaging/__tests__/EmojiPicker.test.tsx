import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { EmojiPicker, RECENT_EMOJI_KEY, readRecentEmoji, recordRecentEmoji } from "../EmojiPicker";
import { ALL_EMOJI, searchEmoji } from "@lightbulb/shared";

beforeEach(() => localStorage.clear());

it("searches by name and keyword", () => {
  expect(searchEmoji("thumbs").some((entry) => entry.emoji === "👍")).toBe(true);
  expect(searchEmoji("party").some((entry) => entry.emoji === "🎉")).toBe(true);
  expect(searchEmoji("zzzznotanemoji")).toEqual([]);
});

it("has a real, deduplicated emoji list of at least 400 entries", () => {
  expect(ALL_EMOJI.length).toBeGreaterThanOrEqual(400);
  const seen = new Set(ALL_EMOJI.map((entry) => entry.emoji));
  expect(seen.size).toBe(ALL_EMOJI.length);
});

it("keeps an MRU of eight recent emoji and survives a broken localStorage", () => {
  for (const emoji of ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]) recordRecentEmoji(emoji);
  expect(readRecentEmoji()).toEqual(["9️⃣", "8️⃣", "7️⃣", "6️⃣", "5️⃣", "4️⃣", "3️⃣", "2️⃣"]);
  localStorage.setItem(RECENT_EMOJI_KEY, "{not json");
  expect(readRecentEmoji()).toEqual([]);
});

it("opens, searches, picks with keyboard, records recent, and restores focus", async () => {
  const user = userEvent.setup();
  const onPick = vi.fn();
  render(<EmojiPicker onPick={onPick} />);
  const trigger = screen.getByRole("button", { name: "Add reaction" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Choose an emoji" });
  const search = within(dialog).getByRole("searchbox", { name: "Search emoji" });
  expect(search).toHaveFocus();
  await user.type(search, "thumbs up");
  await user.keyboard("{ArrowDown}{Enter}");
  expect(onPick).toHaveBeenCalledWith("👍");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(readRecentEmoji()).toEqual(["👍"]);
});

it("closes on Escape and returns focus to the trigger", async () => {
  const user = userEvent.setup();
  render(<EmojiPicker onPick={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "Add reaction" });
  await user.click(trigger);
  expect(screen.getByRole("dialog", { name: "Choose an emoji" })).toBeInTheDocument();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("moves focus between adjacent emoji buttons with ArrowRight and ArrowLeft", async () => {
  const user = userEvent.setup();
  render(<EmojiPicker onPick={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Add reaction" }));
  await user.keyboard("{ArrowDown}");
  const first = document.activeElement;
  expect(first).toHaveAttribute("data-emoji");
  await user.keyboard("{ArrowRight}");
  expect(document.activeElement).toHaveAttribute("data-emoji");
  expect(document.activeElement).not.toBe(first);
  await user.keyboard("{ArrowLeft}");
  expect(document.activeElement).toBe(first);
});
