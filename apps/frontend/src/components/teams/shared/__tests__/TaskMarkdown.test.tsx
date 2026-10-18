import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

let settings: { issuePrefix?: string } | undefined = { issuePrefix: "ISOA" };
const reads: (string | null)[] = [];
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      reads.push(path);
      if (path === "/settings") return { data: settings };
      if (path === "/issues/ISOA-845") return { data: { identifier: "ISOA-845", title: "Fix it", status: "in_progress" } };
      return { data: undefined };
    },
  }),
}));

import { TaskMarkdown } from "../components/TaskMarkdown";
import { MarkdownView } from "../components/MarkdownView";

describe("TaskMarkdown issue references", () => {
  it("links bare ISOA-845", () => {
    const { container } = render(<TaskMarkdown body="See ISOA-845 next." />);
    const anchors = container.querySelectorAll("a");
    expect(anchors).toHaveLength(1);
    expect(anchors[0].getAttribute("href")).toContain("ISOA-845");
    expect(anchors[0].getAttribute("href")).toBe("/chat?panel=teams-issue-detail&detailId=ISOA-845");
  });

  it("leaves GPT-4, UTF-8, ISO-8601 plain", () => {
    const { container } = render(<TaskMarkdown body="Uses GPT-4 with UTF-8 and ISO-8601 dates." />);
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });

  it("does not link inside code spans", () => {
    const { container } = render(
      <TaskMarkdown body={"Run `git log ISOA-845` then\n\n```\nISOA-845\n```"} />,
    );
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });

  it("links nothing until the issue prefix is loaded", () => {
    settings = undefined;
    const { container } = render(<TaskMarkdown body="See ISOA-845." />);
    expect(container.querySelectorAll("a")).toHaveLength(0);
    settings = { issuePrefix: "ISOA" };
  });

  it("loads the issue status only on hover", () => {
    reads.length = 0;
    render(<TaskMarkdown body="See ISOA-845." />);
    expect(reads).not.toContain("/issues/ISOA-845");
    fireEvent.pointerEnter(screen.getByRole("link"));
    expect(reads).toContain("/issues/ISOA-845");
    expect(screen.getByLabelText("Issue ISOA-845: Fix it")).toBeTruthy();
  });

  it("keeps agent mentions as chips", () => {
    const { container } = render(<TaskMarkdown body="Ping [@CEO](agent://abc)" />);
    expect(container.querySelector("[data-mention-href='agent://abc']")?.textContent).toBe("@CEO");
  });
});

describe("MarkdownView (room messages)", () => {
  it("MarkdownView without TaskMarkdown leaves ISOA-845 plain", () => {
    const { container } = render(<MarkdownView>{"See ISOA-845."}</MarkdownView>);
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });
});
