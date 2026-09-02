import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { FileEntry, FileInfo } from "@/hooks/useWorkspaceFiles";

// Keep mermaid out of the unit test — FileContentViewer imports it for
// .lobster files, which Brain never renders.
vi.mock("@/components/chat/LobsterGraphView", () => ({ LobsterGraphView: () => null }));

import { BrainPage } from "../BrainPage";
import { BrainFileList } from "../BrainFileList";

const f = (path: string): FileEntry => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  type: "file",
  size: 5,
  modified_at: 1,
});
const FILES = [f("index.md"), f("entities/ada-lovelace.md")];
const page = (content: string, path = "index.md"): FileInfo => ({
  name: path.slice(path.lastIndexOf("/") + 1),
  path,
  size: content.length,
  modified_at: 1_756_000_000,
  content,
  binary: false,
  mime_type: "text/markdown",
});
const noop = { onRetry: () => {}, onSave: async () => {}, onDirtyChange: () => {} };

describe("BrainPage", () => {
  it("renders markdown with frontmatter and managed-block comments hidden", () => {
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page(
          "---\npageType: report\ntitle: Wiki Index\n---\n# Wiki Index\n\n<!-- openclaw:wiki:index:start -->\n- [[entities/ada-lovelace|Ada Lovelace]]\n<!-- openclaw:wiki:index:end -->",
        )}
      />,
    );
    expect(screen.getByRole("heading", { name: "Wiki Index" })).toBeInTheDocument();
    expect(screen.queryByText(/pageType/)).toBeNull();
    expect(screen.queryByText(/openclaw:wiki/)).toBeNull();
  });

  it("a wikilink navigates in-panel to the resolved vault path and keeps a deep-link href", () => {
    const onNavigate = vi.fn();
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={onNavigate}
        {...noop}
        file={page("[[entities/ada-lovelace|Ada Lovelace]]")}
      />,
    );
    const link = screen.getByRole("link", { name: "Ada Lovelace" });
    expect(link).toHaveAttribute("href", "/chat?panel=teams-brain&path=entities%2Fada-lovelace.md");
    fireEvent.click(link);
    expect(onNavigate).toHaveBeenCalledWith("entities/ada-lovelace.md");
  });

  it("a modified click keeps the native deep link so new-tab works", () => {
    const onNavigate = vi.fn();
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={onNavigate}
        {...noop}
        file={page("[[entities/ada-lovelace|Ada Lovelace]]")}
      />,
    );
    fireEvent.click(screen.getByRole("link", { name: "Ada Lovelace" }), { metaKey: true });
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("a bare-stem wikilink resolves the Obsidian way; an unwritten page is marked but still navigates", () => {
    const onNavigate = vi.fn();
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={onNavigate}
        {...noop}
        file={page("[[ada-lovelace]] [[grace-hopper]]")}
      />,
    );
    fireEvent.click(screen.getByRole("link", { name: "ada-lovelace" }));
    expect(onNavigate).toHaveBeenLastCalledWith("entities/ada-lovelace.md");
    const dead = screen.getByRole("link", { name: "grace-hopper" });
    expect(dead).toHaveAttribute("title", "No page here yet");
    fireEvent.click(dead);
    expect(onNavigate).toHaveBeenLastCalledWith("grace-hopper.md");
  });

  it("external links still open in a new tab", () => {
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page("[docs](https://example.com)")}
      />,
    );
    expect(screen.getByRole("link", { name: "docs" })).toHaveAttribute("target", "_blank");
  });

  it("editing mounts the raw editor with frontmatter intact", () => {
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page("---\ntitle: X\n---\n# X")}
      />,
    );
    // toHaveValue, not getByDisplayValue: the default DTL normalizer collapses
    // the newlines this assertion exists to prove survived.
    expect(screen.getByRole("textbox")).toHaveValue("---\ntitle: X\n---\n# X");
  });

  it("a non-markdown file reads through the file viewer even out of edit mode", () => {
    const png: FileInfo = {
      name: "chart.png",
      path: "inbox/chart.png",
      size: 4,
      modified_at: 1,
      content: "AAAA",
      binary: true,
      mime_type: "image/png",
    };
    render(
      <BrainPage
        path="inbox/chart.png"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={png}
      />,
    );
    expect(screen.getByRole("img", { name: "chart.png" })).toBeInTheDocument();
  });

  it("a frontmatter-only stub reads as empty, not as a blank panel", () => {
    const { container } = render(
      <BrainPage
        path="entities/ada-lovelace.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page("---\npageType: entity\n---\n", "entities/ada-lovelace.md")}
      />,
    );
    expect(screen.getByText("This page is empty.")).toBeInTheDocument();
    expect(container.querySelector("article")).toBeNull();
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("frontmatter renders as pills, never as YAML", () => {
    render(
      <BrainPage
        path="entities/ada-lovelace.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page(
          "---\npageType: entity\nstatus: draft\nupdatedAt: 2026-08-31T23:30:00Z\nsourceIds:\n  - s-1\n---\n# Ada\n",
          "entities/ada-lovelace.md",
        )}
      />,
    );
    expect(screen.getByText("entity")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("Updated Aug 31, 2026")).toBeInTheDocument();
    expect(screen.getByText("1 source")).toBeInTheDocument();
    expect(screen.queryByText(/pageType|sourceIds/)).toBeNull();
    expect(screen.getByRole("heading", { name: "Ada" })).toBeInTheDocument();
  });

  it("a ## Related section becomes a chip block, not body markdown", () => {
    const onNavigate = vi.fn();
    render(
      <BrainPage
        path="entities/x.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={onNavigate}
        {...noop}
        file={page(
          "# X\n\nText.\n\n## Related\n\n- [[entities/ada-lovelace|Ada Lovelace]]\n",
          "entities/x.md",
        )}
      />,
    );
    // The label is ours, not a rendered `##` — so no heading of that name exists.
    expect(screen.queryAllByRole("heading", { name: "Related" })).toHaveLength(0);
    expect(screen.getByText("Related")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Ada Lovelace" }));
    expect(onNavigate).toHaveBeenCalledWith("entities/ada-lovelace.md");
  });

  it("no Related section renders no Related block, and no pills without frontmatter", () => {
    const { container } = render(
      <BrainPage
        path="entities/x.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page("# X\n", "entities/x.md")}
      />,
    );
    expect(screen.queryByText("Related")).toBeNull();
    // Pills and the Related label are the only .label-caps this page can draw.
    expect(container.querySelector(".label-caps")).toBeNull();
  });

  it("a page whose only body is a Related list is the chip block, not the empty notice", () => {
    render(
      <BrainPage
        path="entities/x.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={() => {}}
        {...noop}
        file={page(
          "---\npageType: entity\n---\n\n## Related\n\n- [[entities/ada-lovelace|Ada Lovelace]]\n",
          "entities/x.md",
        )}
      />,
    );
    expect(screen.queryByText("This page is empty.")).toBeNull();
    expect(screen.getByRole("link", { name: "Ada Lovelace" })).toBeInTheDocument();
  });

  it("renders the compiled Related block as chips, label and all", () => {
    const onNavigate = vi.fn();
    render(
      <BrainPage
        path="entities/ada-lovelace.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={null}
        onNavigate={onNavigate}
        {...noop}
        file={page(
          [
            "# Ada",
            "",
            "She wrote the first algorithm.",
            "",
            "## Related",
            "<!-- openclaw:wiki:related:start -->",
            "### Referenced By",
            "- [[entities/ada-lovelace|Ada Lovelace]]",
            "<!-- openclaw:wiki:related:end -->",
          ].join("\n"),
          "entities/ada-lovelace.md",
        )}
      />,
    );
    // The sub-heading came out of the body with the section — no orphan.
    expect(screen.queryByRole("heading", { name: "Referenced By" })).toBeNull();
    expect(screen.getByText("Related")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Ada Lovelace" }));
    expect(onNavigate).toHaveBeenCalledWith("entities/ada-lovelace.md");
  });

  it("a 404 is the missing-page notice with an exit to the index", () => {
    const onNavigate = vi.fn();
    const err = Object.assign(new Error("API 404"), { status: 404 });
    render(
      <BrainPage
        path="main/index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={err}
        onNavigate={onNavigate}
        {...noop}
        file={null}
      />,
    );
    expect(screen.getByText(/No page at main\/index\.md/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open the index" }));
    expect(onNavigate).toHaveBeenCalledWith("index.md");
  });

  it("a refused path (403) is the same stale-link notice, not an outage", () => {
    // _guard_brain_path refuses a traversal-shaped wikilink with 400/403. The
    // link is what is wrong; offering Retry would be a lie.
    render(
      <BrainPage
        path="../secrets.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={Object.assign(new Error("API 403"), { status: 403 })}
        onNavigate={() => {}}
        {...noop}
        file={null}
      />,
    );
    expect(screen.getByText(/No page at \.\.\/secrets\.md/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("a 5xx or a network failure is the unavailable notice with Retry", () => {
    const onRetry = vi.fn();
    render(
      <BrainPage
        path="index.md"
        files={FILES}
        editing={false}
        isLoading={false}
        error={new Error("boom")}
        onNavigate={() => {}}
        {...noop}
        onRetry={onRetry}
        file={null}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe("BrainFileList", () => {
  it("says why it is empty instead of rendering nothing", () => {
    render(<BrainFileList files={[]} onSelect={() => {}} emptyText="Nothing filed yet." />);
    expect(screen.getByText("Nothing filed yet.")).toBeInTheDocument();
  });

  it("a row hands its vault path back", () => {
    const onSelect = vi.fn();
    render(<BrainFileList files={FILES} onSelect={onSelect} emptyText="" />);
    fireEvent.click(screen.getByRole("button", { name: /entities\/ada-lovelace\.md/ }));
    expect(onSelect).toHaveBeenCalledWith("entities/ada-lovelace.md");
  });
});
