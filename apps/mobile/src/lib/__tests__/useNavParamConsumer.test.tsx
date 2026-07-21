import { renderHook } from "@testing-library/react-native";

import { useNavParamConsumer } from "../useNavParamConsumer";

type Props = { value: string | undefined };

// renderHook (and rerender) resolve after an internal act(), so awaiting them
// flushes the hook's passive effects — no waitFor needed.
const render = (
  onValue: jest.Mock,
  clear: jest.Mock,
  value: string | undefined,
) =>
  renderHook(
    ({ value }: Props) => useNavParamConsumer(value, onValue, clear),
    { initialProps: { value } },
  );

describe("useNavParamConsumer", () => {
  it("fires onValue once for a non-empty value and consumes it", async () => {
    const onValue = jest.fn();
    const clear = jest.fn();
    await render(onValue, clear, "abc");
    expect(onValue).toHaveBeenCalledTimes(1);
    expect(onValue).toHaveBeenCalledWith("abc");
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it("does not fire for an undefined value", async () => {
    const onValue = jest.fn();
    const clear = jest.fn();
    await render(onValue, clear, undefined);
    expect(onValue).not.toHaveBeenCalled();
    expect(clear).not.toHaveBeenCalled();
  });

  it("does not re-fire while the same value stays set across re-renders", async () => {
    const onValue = jest.fn();
    const clear = jest.fn();
    const { rerender } = await render(onValue, clear, "abc");
    expect(onValue).toHaveBeenCalledTimes(1);
    await rerender({ value: "abc" });
    await rerender({ value: "abc" });
    // A distinct value must fire — total lands at exactly 2, proving the two
    // same-value re-renders in between fired nothing.
    await rerender({ value: "def" });
    expect(onValue).toHaveBeenCalledTimes(2);
    expect(onValue).toHaveBeenLastCalledWith("def");
  });

  it("re-fires when the same value returns after being cleared", async () => {
    const onValue = jest.fn();
    const clear = jest.fn();
    const { rerender } = await render(onValue, clear, "abc");
    expect(onValue).toHaveBeenCalledTimes(1);
    // The screen persists (tab), so a real re-select re-arrives as the same
    // value only after `clear` dropped it to undefined in between.
    await rerender({ value: undefined });
    await rerender({ value: "abc" });
    expect(onValue).toHaveBeenCalledTimes(2);
  });
});
