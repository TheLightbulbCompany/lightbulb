import { SESSION_NOTIFICATION_DEFAULTS } from "@lightbulb/shared";
import { prefsSaver } from "./sessionNotifications";

const D = SESSION_NOTIFICATION_DEFAULTS;

test("two quick changes go out one at a time, the second carrying both", async () => {
  const sent: unknown[] = [];
  let release!: () => void;
  const put = jest.fn((_path: string, body: unknown) => {
    sent.push(body);
    return sent.length === 1 ? new Promise<void>((resolve) => { release = resolve; }) : Promise.resolve();
  });
  const shown: unknown[] = [];
  const save = prefsSaver({ get: jest.fn(), put }, D, (p) => shown.push(p), jest.fn());
  void save({ finished: "off" });
  const done = save({ sound: false });
  await Promise.resolve();
  expect(put).toHaveBeenCalledTimes(1); // the second waits for the first
  release();
  await done;
  expect(sent).toEqual([
    { sessionNotifications: { ...D, finished: "off", sound: false } },
    { sessionNotifications: { ...D, finished: "off", sound: false } },
  ]);
  expect(shown.at(-1)).toEqual({ ...D, finished: "off", sound: false });
});

test("a failed save shows the server's copy, not an older snapshot", async () => {
  const server = { ...D, waiting: "badge" as const };
  const put = jest.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
  const get = jest.fn().mockResolvedValue({ sessionNotifications: server });
  const shown: unknown[] = [];
  const failed = jest.fn();
  const save = prefsSaver({ get, put }, D, (p) => shown.push(p), failed);
  await save({ finished: "off" });
  expect(failed).toHaveBeenCalledTimes(1);
  expect(shown.at(-1)).toEqual(server);
  await save({ sound: false });
  expect(put).toHaveBeenLastCalledWith("/users/me/prefs", { sessionNotifications: { ...server, sound: false } });
});
