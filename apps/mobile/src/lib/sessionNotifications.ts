import { SESSION_NOTIFICATION_DEFAULTS, type SessionNotifications } from "@lightbulb/shared";

type Api = { get: (path: string) => Promise<unknown>; put: (path: string, body: unknown) => Promise<unknown> };

const read = async (api: Api): Promise<SessionNotifications> => ({
  ...SESSION_NOTIFICATION_DEFAULTS,
  ...((await api.get("/users/me/prefs")) as { sessionNotifications?: Partial<SessionNotifications> }).sessionNotifications,
});

/**
 * The phone's saves of the whole `sessionNotifications` object, one at a time: each change merges
 * onto the latest choices (never an older snapshot), and each PUT sends what is current when it
 * goes. A failed save re-reads the server's copy instead of restoring a stale one.
 */
export function prefsSaver(api: Api, initial: SessionNotifications, show: (prefs: SessionNotifications) => void, failed: () => void) {
  let latest = initial;
  let chain: Promise<unknown> = Promise.resolve();
  return (change: Partial<SessionNotifications>): Promise<unknown> => {
    latest = { ...latest, ...change };
    show(latest);
    chain = chain
      .then(() => api.put("/users/me/prefs", { sessionNotifications: latest }))
      .catch(async () => {
        failed();
        latest = await read(api).catch(() => latest);
        show(latest);
      });
    return chain;
  };
}

export { read as readSessionNotifications };
