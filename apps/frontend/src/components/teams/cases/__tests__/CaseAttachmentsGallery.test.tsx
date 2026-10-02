import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const download = vi.fn();
const saveBlob = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ download }),
}));
vi.mock("@/components/teams/issues/issue-output/attachmentBlob", () => ({
  saveBlob: (...args: unknown[]) => saveBlob(...args),
}));

import { CaseAttachmentsGallery } from "@/components/teams/cases/CaseAttachmentsGallery";

const PDF = {
  id: "att1",
  asset: { id: "asset9", contentType: "application/pdf", byteSize: 2048, originalFilename: "brief.pdf" },
  createdAt: "2026-10-01T00:00:00.000Z",
  updatedAt: "2026-10-01T00:00:00.000Z",
};

beforeEach(() => {
  download.mockReset();
  saveBlob.mockReset();
  // jsdom has no object URLs.
  URL.createObjectURL = vi.fn(() => "blob:case-file");
  URL.revokeObjectURL = vi.fn();
});

describe("CaseAttachmentsGallery", () => {
  it("downloads a non-image attachment through the case content route, under its filename", async () => {
    download.mockResolvedValue(new Blob(["pdf"]));
    render(<CaseAttachmentsGallery caseId="c1" attachments={[PDF]} />);

    // Not fetched until asked for: only images load eagerly.
    expect(download).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Download brief.pdf" }));

    await waitFor(() => expect(saveBlob).toHaveBeenCalledWith("blob:case-file", "brief.pdf"));
    expect(download).toHaveBeenCalledWith("/cases/c1/attachments/att1/content");
  });
});
