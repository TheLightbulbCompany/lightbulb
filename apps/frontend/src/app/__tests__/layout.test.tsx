import { describe, expect, it, vi } from "vitest";

vi.mock("next/font/google", () => ({
  Geist_Mono: () => ({ variable: "geist" }),
}));

vi.mock("next/font/local", () => ({
  default: () => ({ variable: "satoshi" }),
}));

import RootLayout from "../layout";

describe("RootLayout", () => {
  it("passes the choose-organization task URL to ClerkProvider", () => {
    const root = RootLayout({ children: null });

    expect(root.props.taskUrls).toEqual({
      "choose-organization": "/session-tasks/choose-organization",
    });
  });
});
