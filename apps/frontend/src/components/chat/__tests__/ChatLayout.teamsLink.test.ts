import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Smoke test for the Teams link in ChatLayout.tsx.
 *
 * Rendering ChatLayout end-to-end requires mocking Clerk, SWR, gateway,
 * agents/billing hooks, posthog, and the catalog — far heavier than what
 * this regression deserves. A source-level assertion is the most direct
 * way to lock in the fix: the file must not reference the retired
 * ``company.isol8.co`` host, and the only Teams link must point at the
 * native ``/teams`` route.
 */

const filePath = resolve(__dirname, "../ChatLayout.tsx");

describe("ChatLayout Teams link", () => {
  const source = readFileSync(filePath, "utf8");

  it("does not reference the retired company.isol8.co host", () => {
    // Strip block comments so historical context kept in JSDoc comments
    // doesn't false-positive the check. Inline ``// company.isol8.co``
    // comments would still match — none should remain.
    const codeOnly = source.replace(/\/\*[\s\S]*?\*\//g, "");
    expect(codeOnly).not.toMatch(/company\.isol8\.co/);
  });

  it("renders Teams link via next/link to /teams (not a cross-subdomain anchor)", () => {
    // Loose match — line-wrapping inside the JSX block means we just
    // need to confirm the href landed on /teams.
    expect(source).toMatch(/href="\/teams"/);
  });

  it("does not derive a per-environment ${env}.company URL anywhere", () => {
    expect(source).not.toMatch(/\$\{env\}\.company/);
    expect(source).not.toMatch(/NEXT_PUBLIC_COMPANY_URL/);
  });

  it("renders the sidebar Teams pill with the teams-pill className", () => {
    expect(source).toMatch(/className="teams-pill"/);
  });

  it("renders the literal text 'Teams' inside the sidebar pill", () => {
    // The pill markup wraps the label in a <span>, e.g. <span>Teams</span>.
    expect(source).toMatch(/<span>Teams<\/span>/);
  });

  it("does not hardcode a placeholder badge count of 3 on the Teams pill", () => {
    // The earlier baseline rendered a static <b>3</b>. The badge must now
    // be conditional on approvalsCount > 0 so an empty state hides it.
    expect(source).not.toMatch(/<b>3<\/b>/);
  });

  it("declares an approvalsCount source the badge is gated on", () => {
    // Source-level guard: ensures the placeholder constant (or future hook
    // assignment) exists and the badge JSX gates on it. When the
    // /teams/approvals BFF lands and we swap the constant for a hook, this
    // assertion still holds — the gate stays the same.
    expect(source).toMatch(/approvalsCount/);
    expect(source).toMatch(/approvalsCount\s*>\s*0\s*&&\s*<b>\{approvalsCount\}<\/b>/);
  });
});
