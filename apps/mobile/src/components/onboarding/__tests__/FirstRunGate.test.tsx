import { act, render, screen, waitFor } from "@testing-library/react-native";
import { Text } from "react-native";

const mockGet = jest.fn();

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
  useUser: () => ({
    user: { id: "user_1", unsafeMetadata: {}, update: jest.fn() },
  }),
}));

// useSafeAreaInsets (used by the funnel) throws without a provider.
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  createApiClient: () => ({ get: mockGet, post: jest.fn() }),
}));

import { FirstRunGate } from "../FirstRunGate";

describe("FirstRunGate", () => {
  beforeEach(() => {
    mockGet.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it("re-probes a null status (200, pre-signup) until trial_local lands, then shows the funnel", async () => {
    jest.useFakeTimers();
    // /billing/account never 404s — a row-less signup gets 200 with a null
    // status while the first /container/status call mints the trial row.
    mockGet
      .mockResolvedValueOnce({ subscription_status: null })
      .mockResolvedValueOnce({ subscription_status: "trial_local" });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );
    expect(screen.getByText("APP")).toBeTruthy();

    await act(async () => {
      await jest.advanceTimersByTimeAsync(5_000);
    });
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Let’s get started.")).toBeTruthy();
  });

  it("resolves a paid account permanently to the normal shell", async () => {
    mockGet.mockResolvedValue({ subscription_status: "active" });
    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });
});
