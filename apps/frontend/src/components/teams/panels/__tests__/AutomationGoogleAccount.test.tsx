import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

const assign = vi.fn().mockResolvedValue(undefined);
const connectAnother = vi.fn().mockResolvedValue(undefined);
const assignments: Record<string, string> = {};

vi.mock("@/hooks/useGoogleAccounts", () => ({
  useGoogleAccounts: () => ({ assignments, assign, connectAnother }),
}));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

import { AutomationGoogleAccount } from "../AutomationGoogleAccount";

const AGENTS = [
  { id: "friday", name: "Friday", role: "ceo", seeded: true },
  { id: "vera", name: "Vera", role: "general", seeded: false },
];

const OPTIONS = [
  { id: "admin@isol8.co", label: "admin@isol8.co" },
  { id: "support@isol8.co", label: "support@isol8.co" },
];

function setAssignments(next: Record<string, string>) {
  for (const k of Object.keys(assignments)) delete assignments[k];
  Object.assign(assignments, next);
}

describe("AutomationGoogleAccount", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setAssignments({});
  });

  it("names the agent that will actually run the automation", () => {
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account="support@isol8.co"
        accountOptions={OPTIONS}
        onSelectAccount={vi.fn()}
      />,
    );
    // Every seeded automation is assigned to the CEO server-side, which the
    // dialog never said — so the sentence must name it, and the account.
    const sentence = screen.getByText(/runs this automation and will read/i);
    expect(sentence.textContent).toContain("Friday");
    expect(sentence.textContent).toContain("support@isol8.co");
  });

  it("defaults the picker to the account the CEO is already assigned", () => {
    setAssignments({ friday: "admin@isol8.co" });
    const onSelectAccount = vi.fn();
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account={null}
        accountOptions={OPTIONS}
        onSelectAccount={onSelectAccount}
      />,
    );
    expect(onSelectAccount).toHaveBeenCalledWith("admin@isol8.co");
  });

  it("does not default to an assignment that is no longer connectable", () => {
    setAssignments({ friday: "gone@isol8.co" });
    const onSelectAccount = vi.fn();
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account={null}
        accountOptions={OPTIONS}
        onSelectAccount={onSelectAccount}
      />,
    );
    expect(onSelectAccount).not.toHaveBeenCalled();
  });

  it("does not default once the picker already has a selection", () => {
    setAssignments({ friday: "admin@isol8.co" });
    const onSelectAccount = vi.fn();
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account="support@isol8.co"
        accountOptions={OPTIONS}
        onSelectAccount={onSelectAccount}
      />,
    );
    expect(onSelectAccount).not.toHaveBeenCalled();
  });

  it("states the contradiction when the picked account isn't the agent's", () => {
    setAssignments({ friday: "admin@isol8.co" });
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account="support@isol8.co"
        accountOptions={OPTIONS}
        onSelectAccount={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/standing instructions say to always use that account/i),
    ).toBeInTheDocument();
    // …and the line above must stop promising an outcome the conflict makes
    // uncertain, or the two paragraphs contradict each other.
    expect(screen.queryByText(/will read/i)).toBeNull();
  });

  it("reconciles the conflict by writing the assignment on request", () => {
    setAssignments({ friday: "admin@isol8.co" });
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account="support@isol8.co"
        accountOptions={OPTIONS}
        onSelectAccount={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: /make support@isol8\.co friday's account/i,
      }),
    );
    expect(assign).toHaveBeenCalledWith("support@isol8.co", "friday");
  });

  it("never writes the assignment on its own", () => {
    setAssignments({ friday: "admin@isol8.co" });
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account="support@isol8.co"
        accountOptions={OPTIONS}
        onSelectAccount={vi.fn()}
      />,
    );
    expect(assign).not.toHaveBeenCalled();
  });

  it("still names the runner and flags the conflict for a legacy company", () => {
    // No persisted seeded_agent_ids (company provisioned before they were
    // recorded) or a transiently empty lookup → the BFF stamps every agent
    // `seeded: false`. The backend still assigns the automation to the rootless
    // role="ceo" agent, so the rail must not go silent about the mailbox.
    const legacy = [
      { id: "friday", name: "Friday", role: "ceo", seeded: false },
      { id: "vera", name: "Vera", role: "general", seeded: false },
    ];
    setAssignments({ friday: "admin@isol8.co" });
    render(
      <AutomationGoogleAccount
        agents={legacy}
        account="support@isol8.co"
        accountOptions={OPTIONS}
        onSelectAccount={vi.fn()}
      />,
    );
    const conflict = screen.getByText(
      /standing instructions say to always use that account/i,
    );
    expect(conflict.textContent).toContain("Friday");
    expect(conflict.textContent).toContain("admin@isol8.co");
    expect(
      screen.getByRole("button", {
        name: /make support@isol8\.co friday's account/i,
      }),
    ).toBeInTheDocument();
  });

  it("names the rootless CEO, not a deployed package's division head", () => {
    // A company deploy leaves a second role="ceo" agent reporting to Friday,
    // and list order is not contractually Friday-first.
    const withDivision = [
      { id: "nova", name: "Nova", role: "ceo", reportsTo: "friday" },
      { id: "friday", name: "Friday", role: "ceo", seeded: true },
    ];
    setAssignments({ nova: "nova@isol8.co", friday: "admin@isol8.co" });
    const onSelectAccount = vi.fn();
    render(
      <AutomationGoogleAccount
        agents={withDivision}
        account={null}
        accountOptions={OPTIONS}
        onSelectAccount={onSelectAccount}
      />,
    );
    expect(onSelectAccount).toHaveBeenCalledWith("admin@isol8.co");
    expect(
      screen.getByText(/runs this automation/i).textContent,
    ).toContain("Friday");
  });

  it("offers a way out of a single-option dead end", () => {
    render(
      <AutomationGoogleAccount
        agents={AGENTS}
        account="admin@isol8.co"
        accountOptions={[OPTIONS[0]]}
        onSelectAccount={vi.fn()}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: /connect another google account/i }),
    );
    expect(connectAnother).toHaveBeenCalled();
  });
});
