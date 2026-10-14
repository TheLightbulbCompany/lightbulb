import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

const props = vi.hoisted(() => ({ beforeSend: undefined as undefined | ((e: { url: string }) => { url: string }) }));
vi.mock("@vercel/analytics/next", () => ({
  Analytics: (p: { beforeSend: (e: { url: string }) => { url: string } }) => { props.beforeSend = p.beforeSend; return null; },
}));

import { VercelAnalytics } from "../VercelAnalytics";

describe("VercelAnalytics", () => {
  it("never sends an invitation's ticket or the approval card it opens", () => {
    render(<VercelAnalytics />);
    const card = encodeURIComponent("/chat?panel=teams-approval-detail&detailId=ap-secret");
    const sent = props.beforeSend!({ url: `https://thelightbulb.company/sign-up?__clerk_ticket=t&redirect_url=${card}` });
    expect(sent.url).toBe("https://thelightbulb.company/sign-up");
    expect(props.beforeSend!({ url: "https://thelightbulb.company/join/invite?decision=x" }).url)
      .toBe("https://thelightbulb.company/join/invite");
  });
});
