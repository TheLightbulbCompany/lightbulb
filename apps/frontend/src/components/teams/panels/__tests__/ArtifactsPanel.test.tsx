import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { SWRResponse } from "swr";
import {
  InlineTeamsNavProvider,
  type InlineTeamsNavValue,
} from "@/components/chat/InlineTeamsNav";

// Per-path read stub — the panel reads the grouped company artifacts once.
let reads: Record<string, Partial<SWRResponse<unknown, Error>>> = {};

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined },
  }),
}));

import { ArtifactsPanel } from "../ArtifactsPanel";

const GROUP = {
  id: "g1",
  groupBy: "task",
  issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
  title: "Ship the thing",
  count: 2,
  mediaKinds: ["document", "image"],
  previewArtifacts: [
    {
      id: "a1",
      source: "document",
      mediaKind: "document",
      title: "Implementation plan",
      issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
      updatedAt: "2026-01-01T00:00:00Z",
      href: "/ACME/issues/ISO-1#document-plan",
    },
    {
      id: "a2",
      source: "attachment",
      mediaKind: "image",
      title: "Screenshot.png",
      openPath: "/api/attachments/att1/content",
      issue: { id: "iss_1", identifier: "ISO-1", title: "Ship the thing" },
      updatedAt: "2026-01-01T00:00:00Z",
      href: "/ACME/issues/ISO-1#attachment-att1",
    },
  ],
  updatedAt: "2026-01-01T00:00:00Z",
  href: "/ACME/artifacts?groupBy=task&groupIssueId=iss_1",
};

function stubGroups() {
  reads["/artifacts?groupBy=task"] = {
    data: { artifacts: [], groups: [GROUP], nextCursor: null },
    isLoading: false,
    error: undefined,
  };
}

beforeEach(() => {
  reads = {};
});

describe("ArtifactsPanel", () => {
  it("renders each preview artifact as an openable link deep-linking to its issue at the artifact anchor", () => {
    stubGroups();
    render(<ArtifactsPanel />);

    const plan = screen.getByText(/Implementation plan/).closest("a");
    expect(plan).not.toBeNull();
    expect(plan).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss_1#document-plan",
    );

    const shot = screen.getByText(/Screenshot\.png/).closest("a");
    expect(shot).not.toBeNull();
    expect(shot).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss_1#attachment-att1",
    );
  });

  it("routes an artifact click in-shell via openDetail when a nav provider is present", () => {
    stubGroups();
    const openDetail = vi.fn();
    const value: InlineTeamsNavValue = { openDetail, leaveToList: vi.fn() };
    render(
      <InlineTeamsNavProvider value={value}>
        <ArtifactsPanel />
      </InlineTeamsNavProvider>,
    );

    fireEvent.click(screen.getByText(/Implementation plan/));
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  });

  it("keeps the group header as an overall issue-nav fallback", () => {
    stubGroups();
    const openDetail = vi.fn();
    const value: InlineTeamsNavValue = { openDetail, leaveToList: vi.fn() };
    render(
      <InlineTeamsNavProvider value={value}>
        <ArtifactsPanel />
      </InlineTeamsNavProvider>,
    );

    // The identifier chip lives inside the header link.
    const header = screen.getByText("ISO-1").closest("a");
    expect(header).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=iss_1");
    fireEvent.click(screen.getByText("ISO-1"));
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  });

  it("renders artifact links with native hrefs when no nav provider is mounted", () => {
    stubGroups();
    render(<ArtifactsPanel />);
    // No provider → the link keeps its href for native navigation.
    expect(screen.getByText(/Implementation plan/).closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss_1#document-plan",
    );
  });

  it("shows the empty state when there are no groups", () => {
    reads["/artifacts?groupBy=task"] = {
      data: { artifacts: [], groups: [], nextCursor: null },
      isLoading: false,
      error: undefined,
    };
    render(<ArtifactsPanel />);
    expect(screen.getByText("No artifacts yet.")).toBeInTheDocument();
  });
});
