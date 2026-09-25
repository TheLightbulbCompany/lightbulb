import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { SessionShareLinks, sharedSessionLink } from "../SessionShareLinks";

const session = { sessionId: "s1" } as LocalSession;
const link = { linkId: "l1", createdAt: "2026-09-25T10:00:00Z", expiresAt: "2026-10-02T10:00:00Z", snapshotSeq: 12 };

it("warns before sharing, defaults to seven days, copies the link, and revokes", async () => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  const client = {
    createLocalSessionShareLink: vi.fn(async () => ({ ...link, token: "t".repeat(43) })),
    listLocalSessionShareLinks: vi.fn(async () => ({ links: [link] })),
    revokeLocalSessionShareLink: vi.fn(async () => {}),
  };
  render(<SessionShareLinks session={session} client={client} />);
  await userEvent.click(screen.getByRole("button", { name: "Share snapshot…" }));
  expect(screen.getByRole("note")).toHaveTextContent(/backstop, not a guarantee/);
  expect(screen.getByLabelText("Expires after")).toHaveValue("7");
  await userEvent.click(screen.getByRole("button", { name: "Create link" }));
  expect(client.createLocalSessionShareLink).toHaveBeenCalledWith("s1", 7);
  expect(await screen.findByLabelText("Share link")).toHaveValue(sharedSessionLink("t".repeat(43)));
  await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
  expect(client.revokeLocalSessionShareLink).toHaveBeenCalledWith("s1", "l1");
});

it("draws nothing without the share routes", () => {
  const { container } = render(<SessionShareLinks session={session} client={{}} />);
  expect(container).toBeEmptyDOMElement();
});
