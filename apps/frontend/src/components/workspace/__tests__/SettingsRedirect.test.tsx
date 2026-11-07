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


import { SettingsRedirect } from "../SettingsRedirect";

// /chat?panel=<a key Settings took> must land on that Settings section — from a
// bookmark, a mobile "Manage on web" link, a backend action_href, or an in-shell
// pushState — and never paint a dead tab on the way.
describe("SettingsRedirect", () => {
  beforeEach(() => {
    replace.mockClear();
    search = "";
  });

  it("sends a moved panel key to its Settings section and renders nothing", () => {
    search = "panel=llm&action=change-provider";
    render(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(replace).toHaveBeenCalledWith("/chat?panel=settings&action=change-provider&section=billing");
    expect(screen.queryByText("shell")).toBeNull();
  });

  it("renders the shell for everything else", () => {
    search = "panel=teams-issues";
    render(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(screen.getByText("shell")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("UX slice 4: goes straight to the Settings place in the shell, keeping other params", () => {
    search = "panel=usage&connected=github";
    render(<SettingsRedirect><div>shell</div></SettingsRedirect>);
    expect(replace).toHaveBeenCalledWith("/chat?panel=settings&connected=github&section=usage");
  });
});
