import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => null }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => null }));

import SecurityPage, { metadata } from "@/app/security/page";

describe("/security", () => {
  it("names the two selectable providers and legacy key compatibility, and links the Privacy Policy", () => {
    const { container } = render(<SecurityPage />);
    const text = container.textContent ?? "";
    for (const path of ["Lightbulb-managed", "Your ChatGPT account", "Workspaces already configured with a saved OpenAI or Anthropic key"]) {
      expect(text).toContain(path);
    }
    expect(text).toContain("we do not use it to train AI models");
    expect(screen.getByRole("link", { name: "Privacy Policy" })).toHaveAttribute("href", "/privacy");
    expect(metadata.alternates?.canonical).toBe("https://thelightbulb.company/security");
  });

  it("carries the claims table with evidence on every row", () => {
    render(<SecurityPage />);
    const table = screen.getByRole("table", { name: "Security claims and the evidence for them" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(11);
    for (const row of rows) {
      const cells = within(row).getAllByRole("cell");
      expect(cells).toHaveLength(2);
      for (const cell of cells) expect((cell.textContent ?? "").trim()).not.toBe("");
    }
    expect(table.textContent).toContain("a computer that stays offline forever keeps what it held");
    // Memory stays on members' computers on the free network only: a workspace keeps a searchable copy.
    expect(table.textContent).toContain(
      "On the free network, memory lives on members' computers and moves only between them",
    );
    expect(table.textContent).toContain(
      "In a workspace, the workspace's always-on member keeps a copy of the team entries, never the private entries on your computers, and the facts it mirrors for search are reachable only by that member and our backend.",
    );
  });

  it("says what leaves your machine: memory, capture and upload", () => {
    render(<SecurityPage />);
    const band = screen.getByRole("heading", { name: "What leaves your machine" }).parentElement!;
    for (const word of ["memory", "capture", "upload"]) {
      expect(band.textContent?.toLowerCase()).toContain(word);
    }
    // Capture keeps prompts and replies locally; uploads and workspace memory are processed in AWS.
    expect(band.textContent).toContain("your prompts and the agents' replies");
    expect(band.textContent).toContain("lightbulb sessions turns off");
    expect(band.textContent).toContain("AWS (Bedrock AgentCore)");
    expect(band.textContent).not.toContain("third party");
    expect(within(band).getByRole("link", { name: /memory on your computers/ })).toHaveAttribute("href", "/docs/memory");
  });

  it("scopes the secret claim: facts are refused, room turns are scrubbed", () => {
    render(<SecurityPage />);
    const table = screen.getByRole("table", { name: "Security claims and the evidence for them" });
    expect(table.textContent).toContain("A match is refused with the reason");
    expect(table.textContent).toContain("Workspace room turns are handled differently: secret-shaped text is scrubbed");
  });

  it("names no competitor and claims no compliance", () => {
    const { container } = render(<SecurityPage />);
    const text = container.textContent ?? "";
    for (const name of ["past.dev", "Beacon", "Mem0", "Hindsight", "Honcho", "Graphiti", "Supermemory"]) {
      expect(text).not.toContain(name);
    }
    // The page's own denial ("we do not hold ... such as SOC 2") is the one allowed mention.
    const claims = text.replace(/We do not hold a security certification such as SOC 2 today\./, "");
    expect(claims).not.toMatch(/SOC ?2|SAML|HIPAA/);
    expect(text).not.toContain("!");
  });
});
