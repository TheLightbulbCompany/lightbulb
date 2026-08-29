// apps/frontend/src/components/teams/inbox/__tests__/ApprovalRow.provisioning.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const approve = vi.fn();
const reject = vi.fn();
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve, reject }),
}));

const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post, get }) }));

import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

const CONSENT: Approval = {
  id: "a-consent",
  type: "request_board_approval",
  payload: {
    isol8_action: "provider_consent",
    title: "PostHog analytics",
    provider: "posthog",
    service: "analytics",
    terms_url: "https://posthog.com/terms",
    pricing: "Free up to 1M events/mo",
  },
  status: "pending",
};

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset().mockResolvedValue(undefined);
  notifyApiError.mockReset();
  notifyError.mockReset();
  post.mockReset();
  get.mockReset();
});

describe("ApprovalRow provider_consent", () => {
  it("shows provider, service, price and a Terms link, then approves on click", async () => {
    render(<ApprovalRow approval={CONSENT} />);

    expect(screen.getByText("Approve provider: PostHog analytics")).toBeInTheDocument();
    expect(screen.getByText(/posthog · analytics · Free up to 1M events\/mo/)).toBeInTheDocument();
    expect(screen.getByText("Terms")).toHaveAttribute("href", "https://posthog.com/terms");

    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith("a-consent"));
  });

  it("drops a non-https Terms link", () => {
    const payload = { ...CONSENT.payload, terms_url: "javascript:alert(1)" };
    render(<ApprovalRow approval={{ ...CONSENT, payload }} />);
    expect(screen.queryByText("Terms")).toBeNull();
  });
});
