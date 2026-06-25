import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IssueAttachmentsSection } from "@/components/teams/issues/IssueAttachmentsSection";
import type { UseIssueAttachmentsResult } from "@/components/teams/issues/hooks/useIssueAttachments";
import type { IssueAttachment } from "@/components/teams/shared/types";

// A single hoisted result the test mutates per-case so it can flip
// loading/error/empty/populated states. Annotated return type so `tsc`
// (which CI runs over test files) accepts the mock shape.
const { hookResult } = vi.hoisted(() => ({
  hookResult: {
    value: undefined as unknown as UseIssueAttachmentsResult,
  },
}));

vi.mock("@/components/teams/issues/hooks/useIssueAttachments", () => ({
  useIssueAttachments: (): UseIssueAttachmentsResult => hookResult.value,
}));

function makeResult(over: Partial<UseIssueAttachmentsResult> = {}): UseIssueAttachmentsResult {
  return {
    attachments: [],
    isLoading: false,
    isError: false,
    error: null,
    upload: vi.fn().mockResolvedValue({} as IssueAttachment),
    remove: vi.fn().mockResolvedValue(undefined),
    download: vi.fn().mockResolvedValue(undefined),
    ...over,
  };
}

const ATTACHMENT: IssueAttachment = {
  id: "att_1",
  originalFilename: "design.png",
  contentType: "image/png",
  byteSize: 2048,
  createdAt: "2026-06-20T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  hookResult.value = makeResult();
});

describe("IssueAttachmentsSection", () => {
  it("shows the empty state with an upload control when there are no attachments", () => {
    render(<IssueAttachmentsSection issueId="iss_1" />);
    expect(screen.getByText(/no attachments yet/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/upload attachment/i)).toBeInTheDocument();
  });

  it("renders the attachment list with filename + size + actions", () => {
    hookResult.value = makeResult({ attachments: [ATTACHMENT] });
    render(<IssueAttachmentsSection issueId="iss_1" />);
    expect(screen.getByText("design.png")).toBeInTheDocument();
    expect(screen.getByText(/2\.0 KB/)).toBeInTheDocument();
    expect(screen.getByLabelText(/download design\.png/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/delete design\.png/i)).toBeInTheDocument();
  });

  it("shows an error affordance when the list fails to load", () => {
    hookResult.value = makeResult({ isError: true, error: new Error("boom") });
    render(<IssueAttachmentsSection issueId="iss_1" />);
    expect(screen.getByText(/couldn't load attachments/i)).toBeInTheDocument();
  });

  it("calls upload with the chosen file", async () => {
    const upload = vi.fn().mockResolvedValue({} as IssueAttachment);
    hookResult.value = makeResult({ upload });
    render(<IssueAttachmentsSection issueId="iss_1" />);

    const input = screen.getByLabelText(/upload attachment/i) as HTMLInputElement;
    const file = new File(["hello"], "hello.txt", { type: "text/plain" });
    // The change handler awaits the (mocked) upload then setStates; wrap in act
    // so the resulting re-render is flushed inside the test's act scope.
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });

    await vi.waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    expect(upload).toHaveBeenCalledWith(file);
  });

  it("calls download when the download button is clicked", async () => {
    const download = vi.fn().mockResolvedValue(undefined);
    hookResult.value = makeResult({ attachments: [ATTACHMENT], download });
    render(<IssueAttachmentsSection issueId="iss_1" />);

    await userEvent.click(screen.getByLabelText(/download design\.png/i));
    expect(download).toHaveBeenCalledWith(ATTACHMENT);
  });

  it("calls remove when the delete button is clicked", async () => {
    const remove = vi.fn().mockResolvedValue(undefined);
    hookResult.value = makeResult({ attachments: [ATTACHMENT], remove });
    render(<IssueAttachmentsSection issueId="iss_1" />);

    await userEvent.click(screen.getByLabelText(/delete design\.png/i));
    expect(remove).toHaveBeenCalledWith("att_1");
  });
});
