import { sessionMobileHref } from "../sessionLink";

const ORIGIN = "https://dev.flickmeon.ai";

test("session permalinks open the native session screen", () => {
  expect(sessionMobileHref(`${ORIGIN}/chat?panel=session&session=abc-123`, ORIGIN)).toEqual({
    pathname: "/(app)/session/[sessionId]", params: { sessionId: "abc-123" },
  });
  expect(sessionMobileHref(`${ORIGIN}/chat?panel=session&session=abc-123`)).toEqual({
    pathname: "/(app)/session/[sessionId]", params: { sessionId: "abc-123" },
  });
});

test("other panels, hosts, and empty links stay outside the session route", () => {
  expect(sessionMobileHref(`${ORIGIN}/chat?panel=chat&session=abc`, ORIGIN)).toBeNull();
  expect(sessionMobileHref("https://evil.example/chat?panel=session&session=abc", ORIGIN)).toBeNull();
  expect(sessionMobileHref(`${ORIGIN}/chat?panel=session`, ORIGIN)).toBeNull();
  expect(sessionMobileHref(null, ORIGIN)).toBeNull();
});
