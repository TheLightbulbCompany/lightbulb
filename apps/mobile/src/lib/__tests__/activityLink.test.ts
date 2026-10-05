import { activityMobileHref } from "../activityLink";

const ORIGIN = "https://thelightbulb.company";
const FOCUS = "login:0123456789abcdef0123456789abcdef";

test("an Activity permalink with a focus opens the Activity tab on that item", () => {
  const expected = { pathname: "/(app)/(tabs)/activity", params: { focus: FOCUS } };
  expect(activityMobileHref(`${ORIGIN}/chat?panel=activity&focus=${FOCUS}`)).toEqual(expected);
  expect(activityMobileHref(`https://dev.thelightbulb.company/chat?panel=activity&focus=${FOCUS}`)).toEqual(expected);
  expect(activityMobileHref(`/chat?panel=activity&focus=${FOCUS}`)).toEqual(expected);
});

test("an Activity permalink with no focus opens the Activity tab", () => {
  expect(activityMobileHref(`${ORIGIN}/chat?panel=activity`)).toBe("/(app)/(tabs)/activity");
  expect(activityMobileHref("/chat?panel=activity")).toBe("/(app)/(tabs)/activity");
});

test("other panels, hosts, paths and empty links are not Activity links", () => {
  expect(activityMobileHref(`${ORIGIN}/chat?panel=chat&focus=${FOCUS}`)).toBeNull();
  expect(activityMobileHref(`https://evil.example/chat?panel=activity&focus=${FOCUS}`)).toBeNull();
  expect(activityMobileHref(`//evil.example/chat?panel=activity`)).toBeNull();
  expect(activityMobileHref(`${ORIGIN}/other?panel=activity`)).toBeNull();
  expect(activityMobileHref("/ISOA/issues/ISOA-1")).toBeNull();
  expect(activityMobileHref(null)).toBeNull();
});
