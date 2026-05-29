// apps/frontend/src/__tests__/teams/TeamsPanelRouter.redirect.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

describe("TeamsPanelRouter retirement", () => {
  it("redirects the retired approvals panel to the inbox", () => {
    render(<TeamsPanelRouter panel="approvals" />);
    expect(replace).toHaveBeenCalledWith("/teams/inbox");
  });
});
