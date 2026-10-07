import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LAUNCH_REF_KEY, type LaunchMe } from "@/lib/launch";
import { useReferralAttribution } from "../useReferralAttribution";

const me = { referral_code: "MYOWNCODE" } as LaunchMe;

function Harness({ post }: { post: ReturnType<typeof vi.fn> }) {
  useReferralAttribution({ post } as never, me);
  return null;
}

describe("useReferralAttribution", () => {
  beforeEach(() => localStorage.clear());

  it("clears the stored code once the server attributes it", async () => {
    localStorage.setItem(LAUNCH_REF_KEY, "A4BXTEBM");
    const post = vi.fn().mockResolvedValue({ attributed: true });

    render(<Harness post={post} />);

    await waitFor(() => expect(localStorage.getItem(LAUNCH_REF_KEY)).toBeNull());
    expect(post).toHaveBeenCalledWith("/launch/referral", { ref: "A4BXTEBM" });
  });

  it("KEEPS the code when the server refuses to attribute it", async () => {
    // The refusal means "not this caller" — an already-subscribed visitor, or
    // a row that already carries a referred_by. Clearing here burned the code
    // for the sign-up that followed in the same browser.
    localStorage.setItem(LAUNCH_REF_KEY, "A4BXTEBM");
    const post = vi.fn().mockResolvedValue({ attributed: false });

    render(<Harness post={post} />);

    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("A4BXTEBM");
  });

  it("keeps the code when the request throws", async () => {
    localStorage.setItem(LAUNCH_REF_KEY, "A4BXTEBM");
    const post = vi.fn().mockRejectedValue(new Error("network"));

    render(<Harness post={post} />);

    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("A4BXTEBM");
  });

  it("never self-attributes", () => {
    localStorage.setItem(LAUNCH_REF_KEY, "MYOWNCODE");
    const post = vi.fn();

    render(<Harness post={post} />);

    expect(post).not.toHaveBeenCalled();
    expect(localStorage.getItem(LAUNCH_REF_KEY)).toBe("MYOWNCODE");
  });
});
