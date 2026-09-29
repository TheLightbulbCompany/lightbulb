import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";

import { AwsConnectCard } from "../AwsConnectCard";
import { AwsChangeAccessCard } from "../AwsChangeAccessCard";
import { ConnectButton } from "../ConnectButton";
import { type Connection } from "@/hooks/useConnections";

// The card reads `useApi().get` (aws-setup) + `useApi().post` (aws-complete)
// directly, so capture both to assert the endpoints + payload.
const getMock = vi.fn();
const postMock = vi.fn().mockResolvedValue({ status: "connected", provider: "aws" });
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    useApi: () => ({ get: getMock, post: postMock, put: vi.fn(), del: vi.fn() }),
  };
});

const SETUP = {
  external_id: "lightbulb-ext-abc123",
  principal_arn: "arn:aws:iam::877352799272:role/lightbulb-prod-owner-task",
  our_account_id: "877352799272",
  default_region: "us-east-1",
  // null → CFN template not hosted yet; the manual paste path must still work.
  view_only: { policy_arn: "arn:aws:iam::aws:policy/job-function/ViewOnlyAccess", quick_create_url: null },
  read_only: { policy_arn: "arn:aws:iam::aws:policy/ReadOnlyAccess", quick_create_url: null },
  power_user: { policy_arn: "arn:aws:iam::aws:policy/PowerUserAccess", quick_create_url: null },
};

const awsRow: Connection = {
  provider: "aws",
  display_name: "AWS",
  acquisition: "aws_sts",
  status: "disconnected",
};

const VALID_ARN = "arn:aws:iam::111122223333:role/IsolReadOnly";

function renderCard(overrides: Partial<React.ComponentProps<typeof AwsConnectCard>> = {}) {
  const refreshConnections = vi.fn().mockResolvedValue(undefined);
  render(
    <AwsConnectCard
      connection={awsRow}
      canManage
      refreshConnections={refreshConnections}
      {...overrides}
    />,
  );
  return { refreshConnections };
}

describe("AwsConnectCard", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockResolvedValue(SETUP);
    postMock.mockClear();
  });

  it("renders a collapsed Connect button, not a paste-key input", () => {
    renderCard();
    expect(screen.getByRole("button", { name: /connect aws/i })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/paste api key/i)).not.toBeInTheDocument();
  });

  it("calls aws-setup on expand and shows the ExternalId + principal ARN", async () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /connect aws/i }));

    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/connections/aws/aws-setup"));
    expect(await screen.findByText(SETUP.external_id)).toBeInTheDocument();
    expect(screen.getByText(SETUP.principal_arn)).toBeInTheDocument();
    // ViewOnly is the default scope.
    expect(screen.getByRole("button", { name: "ViewOnly" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("disables Create-role and shows 'coming soon' when quick_create_url is null", async () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /connect aws/i }));

    const createBtn = await screen.findByRole("button", { name: /create role in aws/i });
    expect(createBtn).toBeDisabled();
    expect(screen.getByText(/aws role template coming soon/i)).toBeInTheDocument();
  });

  it("shows the managed-policy ARN to attach on the manual path, tracking the scope toggle", async () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /connect aws/i }));
    await screen.findByText(SETUP.external_id);

    // ViewOnly default → ViewOnlyAccess policy surfaced for the hand-built role.
    expect(screen.getByText(/managed policy to attach/i)).toBeInTheDocument();
    expect(screen.getByText(SETUP.view_only.policy_arn)).toBeInTheDocument();

    // Toggling to ReadOnly swaps the displayed policy ARN.
    fireEvent.click(screen.getByRole("button", { name: "ReadOnly" }));
    expect(screen.getByText(SETUP.read_only.policy_arn)).toBeInTheDocument();
  });

  it("offers an in-card Retry that re-runs aws-setup after a setup failure", async () => {
    getMock.mockRejectedValueOnce(new Error("boom"));
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /connect aws/i }));

    const retry = await screen.findByRole("button", { name: /retry setup/i });
    expect(getMock).toHaveBeenCalledTimes(1);

    getMock.mockResolvedValueOnce(SETUP);
    fireEvent.click(retry);

    expect(await screen.findByText(SETUP.external_id)).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it("enables Connect only for a well-formed role ARN, then POSTs aws-complete", async () => {
    const { refreshConnections } = renderCard();
    fireEvent.click(screen.getByRole("button", { name: /connect aws/i }));
    await screen.findByText(SETUP.external_id);

    const input = screen.getByLabelText(/role arn/i);
    const submit = screen.getByRole("button", { name: "Connect AWS" });
    expect(submit).toBeDisabled();

    // Malformed ARN keeps Connect disabled.
    fireEvent.change(input, { target: { value: "not-an-arn" } });
    expect(submit).toBeDisabled();

    fireEvent.change(input, { target: { value: VALID_ARN } });
    expect(submit).toBeEnabled();

    fireEvent.click(submit);
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/connections/aws/aws-complete", {
        role_arn: VALID_ARN,
        region: "us-east-1",
      }),
    );
    await waitFor(() => expect(refreshConnections).toHaveBeenCalledTimes(1));
  });
});

describe("ConnectButton aws_sts wiring", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockResolvedValue(SETUP);
  });

  it("renders the AWS STS card (not a paste box) for an aws_sts provider", () => {
    render(
      <ConnectButton
        connection={awsRow}
        canManage
        connectPaste={vi.fn()}
        startOAuth={vi.fn()}
        disconnect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /connect aws/i })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/paste api key/i)).not.toBeInTheDocument();
  });

  it("offers Change access level (not a reconnect) on a connected aws_sts row", () => {
    render(
      <ConnectButton
        connection={{ ...awsRow, status: "connected" }}
        canManage
        connectPaste={vi.fn()}
        startOAuth={vi.fn()}
        disconnect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /disconnect aws/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /change aws access level/i })).toBeInTheDocument();
  });
});

describe("AwsChangeAccessCard", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockResolvedValue(SETUP);
    postMock.mockClear();
  });

  it("reads aws-setup on expand, shows the tier's policy ARN + a region-scoped stack-update link, and never POSTs", async () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(<AwsChangeAccessCard connection={{ ...awsRow, status: "connected" }} canManage />);

    fireEvent.click(screen.getByRole("button", { name: /change aws access level/i }));
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/connections/aws/aws-setup"));

    // ViewOnly default policy shown; toggling to PowerUser swaps it.
    expect(await screen.findByText(SETUP.view_only.policy_arn)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "PowerUser" }));
    expect(screen.getByText(SETUP.power_user.policy_arn)).toBeInTheDocument();

    // Deep link targets the CFN stack-update console for the existing stack
    // (lightbulb-connector — see CONNECTOR_ROLE_NAME), region-scoped to the
    // setup default.
    fireEvent.click(screen.getByRole("button", { name: /update stack in aws/i }));
    expect(openSpy).toHaveBeenCalledWith(
      "https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/update/template?stackId=lightbulb-connector",
      "_blank",
      "noopener,noreferrer",
    );

    // In-place change: no aws-complete / disconnect call.
    expect(postMock).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });
});
