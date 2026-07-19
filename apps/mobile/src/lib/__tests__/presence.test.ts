import { AppState } from "react-native";

import { isPresent } from "../presence";

describe("presence", () => {
  it("is present only when AppState is active", () => {
    (AppState as unknown as { currentState: string }).currentState = "active";
    expect(isPresent()).toBe(true);
    (AppState as unknown as { currentState: string }).currentState = "background";
    expect(isPresent()).toBe(false);
    (AppState as unknown as { currentState: string }).currentState = "inactive";
    expect(isPresent()).toBe(false);
  });
});
