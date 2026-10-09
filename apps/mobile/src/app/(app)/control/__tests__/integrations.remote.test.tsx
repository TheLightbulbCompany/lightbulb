import { fireEvent, render, screen } from "@testing-library/react-native";

let mockRole = "org:admin";
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: async () => "test-token" }),
  useOrganization: () => ({ organization: { id: "org_1" }, membership: { role: mockRole } }),
}));
const mockPush = jest.fn();

jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args) },
    useFocusEffect: (cb: () => void) => React.useEffect(cb, [cb]),
  };
});

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({
    get: async () => ({
      servers: {
        posthog: { url: "https://mcp.posthog.com/mcp?token=secret" },
        meta_ads: { transport: "streamable-http", url: "https://mcp.facebook.com/ads" },
        custom: { command: "npx", args: ["-y", "@scope/mcp-server"] },
      },
    }),
  }),
}));

import IntegrationsPanel from "../integrations";

it("labels remote MCP entries as configured without exposing URL credentials", async () => {
  await render(<IntegrationsPanel />);

  expect(await screen.findByText("Remote MCP · mcp.posthog.com")).toBeTruthy();
  expect(screen.getByText("streamable-http · mcp.facebook.com")).toBeTruthy();
  expect(screen.getByText("npx -y @scope/mcp-server")).toBeTruthy();
  expect(screen.getAllByText("Configured")).toHaveLength(3);
  expect(screen.queryByText("Incomplete")).toBeNull();
  expect(JSON.stringify(screen.toJSON())).not.toContain("secret");
});

it("offers website sign-ins to an admin", async () => {
  await render(<IntegrationsPanel />);
  await fireEvent.press(await screen.findByLabelText("Website sign-ins"));
  expect(mockPush).toHaveBeenCalledWith("/(app)/control/website-signins");
});

it("hides website sign-ins from a member", async () => {
  mockRole = "org:member";
  await render(<IntegrationsPanel />);
  expect(await screen.findByText("MCP servers are managed by your organization admin")).toBeTruthy();
  expect(screen.queryByLabelText("Website sign-ins")).toBeNull();
});
