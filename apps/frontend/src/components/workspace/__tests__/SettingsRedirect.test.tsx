// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

const replace = vi.fn();
let search = "";
const router = { replace };
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(search),
}));

const live = vi.hoisted(() => ({ value: null as null | { superAgent: boolean; httpReady: boolean; featuresLoaded: boolean } }));
vi.mock("@/hooks/useLiveEvents", () => ({ useOptionalLiveEvents: () => live.value }));

import { SettingsRedirect } from "../SettingsRedirect";

// /chat?panel=<a key Settings took> must land on that Settings section — from a
// bookmark, a mobile "Manage on web" link, a backend action_href, or an in-shell
// pushState — and never paint a dead tab on the way.
describe("SettingsRedirect", () => {
  beforeEach(() => {
    replace.mockClear();
    search = "";
    live.value = null;
  });

  it("sends a moved panel key to its Settings section and renders nothing", () => {
    search = "panel=llm&action=change-provider";
    render(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(replace).toHaveBeenCalledWith("/settings?panel=providers&action=change-provider");
    expect(screen.queryByText("shell")).toBeNull();
  });

  it("renders the shell for everything else", () => {
    search = "panel=teams-issues";
    render(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(screen.getByText("shell")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("super-agent (UX slice 4): goes straight to the Settings place in the shell, once the live config has said so", () => {
    search = "panel=usage&connected=github";
    live.value = { superAgent: false, httpReady: true, featuresLoaded: false };
    const { rerender } = render(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(replace).not.toHaveBeenCalled();
    live.value = { superAgent: true, httpReady: true, featuresLoaded: true };
    rerender(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(replace).toHaveBeenCalledWith("/chat?panel=settings&connected=github&section=usage");
  });
});
