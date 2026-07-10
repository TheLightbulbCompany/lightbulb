import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

import BillingSettingsPage from "../billing/page";

describe("/settings/billing route", () => {
  it("redirects to the billing panel on /settings", () => {
    render(<BillingSettingsPage />);
    expect(replace).toHaveBeenCalledWith("/settings?panel=billing");
  });
});
