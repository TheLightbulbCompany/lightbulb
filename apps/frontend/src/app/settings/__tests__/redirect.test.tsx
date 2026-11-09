// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ replace: vi.fn(), query: "" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));
import SettingsPage from "../page";

beforeEach(() => { nav.replace.mockClear(); nav.query = ""; });

it("opens legacy Settings bookmarks outside the retired chat shell", () => {
  render(<SettingsPage />);
  expect(nav.replace).toHaveBeenCalledWith("/voice/settings");
});

it("preserves checkout and sign-in continuation on the reachable settings page", () => {
  nav.query = "panel=billing&action=connect-chatgpt&checkout=success";
  render(<SettingsPage />);
  expect(nav.replace).toHaveBeenCalledWith("/voice/settings?panel=billing&action=connect-chatgpt&checkout=success");
});
