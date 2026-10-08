import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import { createInputQueue, MAX_INPUT_BYTES } from "./sessionInput";

const settle = async () => { for (let i = 0; i < 100; i += 1) await Promise.resolve(); };

describe("createInputQueue", () => {
  it("sends chunks of at most 4096 bytes, one at a time, in order", async () => {
    const sizes: number[] = [];
    let open = 0;
    const queue = createInputQueue(() => {});
    queue(new Uint8Array(10_240), async (part) => { open += 1; expect(open).toBe(1); await Promise.resolve(); sizes.push(part.length); open -= 1; });
    await settle();
    expect(sizes).toEqual([MAX_INPUT_BYTES, MAX_INPUT_BYTES, 2048]);
  });

  it("sends exactly one request when the first chunk of a 10 KB paste is refused", async () => {
    const send = vi.fn().mockRejectedValue(new ApiError(403, null));
    const onRefused = vi.fn();
    const queue = createInputQueue(onRefused);
    queue(new Uint8Array(10_240), send);
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
    expect(onRefused).toHaveBeenCalledTimes(1);
    queue(new Uint8Array(1), send);
    await settle();
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("loses only that batch to an offline Mac", async () => {
    const send = vi.fn().mockRejectedValueOnce(new ApiError(409, { code: "LOCAL_SESSION_OFFLINE" })).mockResolvedValue(undefined);
    const onRefused = vi.fn();
    const queue = createInputQueue(onRefused);
    queue(new Uint8Array(1), send);
    queue(new Uint8Array(1), send);
    await settle();
    expect(send).toHaveBeenCalledTimes(2);
    expect(onRefused).not.toHaveBeenCalled();
  });
});
