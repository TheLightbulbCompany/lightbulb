import { render, screen } from "@testing-library/react-native";

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: async () => "test-token" }),
  useOrganization: () => ({ organization: null, membership: null }),
}));

jest.mock("expo-router", () => {
  const React = jest.requireActual("react");
  return { useFocusEffect: (cb: () => void) => React.useEffect(cb, [cb]) };
});

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({
    get: async () => ({
      servers: {
        posthog: { url: "https://mcp.posthog.com/mcp?token=secret" },
        meta_ads: { transport: "streamable-http", url: "https://mcp.facebook.com/ads" },
      },
    }),
  }),
}));

import IntegrationsPanel from "../integrations";

it("labels remote MCP entries as configured without exposing URL credentials", async () => {
  await render(<IntegrationsPanel />);

  expect(await screen.findByText("Remote MCP · mcp.posthog.com")).toBeTruthy();
  expect(screen.getByText("streamable-http · mcp.facebook.com")).toBeTruthy();
  expect(screen.getAllByText("Configured")).toHaveLength(2);
  expect(screen.queryByText("Incomplete")).toBeNull();
  expect(JSON.stringify(screen.toJSON())).not.toContain("secret");
});
