import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { Text } from "react-native";

// Sign in with Apple + "Hide My Email" reaches us with no name; the app asks
// for one before any workspace surface instead of showing a relay prefix.
type MockUser = { firstName: string | null; username?: string | null; update: jest.Mock };
let mockLoaded = true;
let mockUser: MockUser | null = null;
const mockSignOut = jest.fn(async () => undefined);

jest.mock("@clerk/clerk-expo", () => ({
  useUser: () => ({ isLoaded: mockLoaded, user: mockUser }),
  useAuth: () => ({ signOut: mockSignOut }),
}));

import { NameGate } from "../NameGate";

const gate = () => (
  <NameGate>
    <Text>APP</Text>
  </NameGate>
);

beforeEach(() => {
  mockLoaded = true;
  mockSignOut.mockClear();
  mockUser = { firstName: null, update: jest.fn() };
  // What Clerk does: the saved name lands on the user the hook hands out.
  mockUser.update.mockImplementation(async (fields: { firstName: string }) => {
    mockUser = { ...mockUser!, ...fields };
  });
});

it.each([{ firstName: "Ada" }, { firstName: null, username: "ada" }])(
  "leaves a person who already has a name alone: %j",
  async (named) => {
    mockUser = { ...named, update: jest.fn() };
    await render(gate());
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("What should people call you?")).toBeNull();
  },
);

it("renders nothing until Clerk has loaded the user", async () => {
  mockLoaded = false;
  await render(gate());
  expect(screen.queryByText("APP")).toBeNull();
  expect(screen.queryByText("What should people call you?")).toBeNull();
});

it("asks a nameless person, saves to Clerk, then shows the app", async () => {
  const update = mockUser!.update;
  await render(gate());
  expect(screen.getByText("What should people call you?")).toBeTruthy();
  expect(screen.queryByText("APP")).toBeNull();

  // A first name is required: Continue does nothing without one.
  await fireEvent.press(screen.getByText("Continue"));
  expect(update).not.toHaveBeenCalled();

  await fireEvent.changeText(screen.getByLabelText("First name"), " Sam ");
  await fireEvent.changeText(screen.getByLabelText("Last name (optional)"), "Rivera");
  await fireEvent.press(screen.getByText("Continue"));

  await waitFor(() => expect(update).toHaveBeenCalledWith({ firstName: "Sam", lastName: "Rivera" }));
  await screen.rerender(gate()); // Clerk re-renders its hooks' users with the saved name
  expect(screen.getByText("APP")).toBeTruthy();
});

it("stays on the question and says so when the save fails", async () => {
  mockUser!.update.mockRejectedValue(new Error("offline"));
  await render(gate());
  await fireEvent.changeText(screen.getByLabelText("First name"), "Sam");
  await fireEvent.press(screen.getByText("Continue"));
  expect(await screen.findByText("We couldn't save your name. Try again.")).toBeTruthy();
  expect(screen.queryByText("APP")).toBeNull();
});

it("offers a way out", async () => {
  await render(gate());
  await fireEvent.press(screen.getByText("Sign out"));
  expect(mockSignOut).toHaveBeenCalled();
});
