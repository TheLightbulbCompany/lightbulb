// teamsData imports expo-router (useFocusEffect) at module level; stub it so
// this pure-function test doesn't drag the router through the transform.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

import { looksLikeMarkdown } from "../teamsData";

describe("looksLikeMarkdown", () => {
  it.each([
    ["# heading", true],
    ["- bullet", true],
    ["**bold** claim", true],
    ["see [docs](https://x.co)", true],
    ["plain sentence, nothing special", false],
    // Bare URLs must route through the markdown renderer (linkify makes them
    // tappable); a plain <Text> renders them dead.
    ["deployed to https://example.com/app", true],
    ["http://plain.example", true],
  ])("%s -> %s", (text, expected) => {
    expect(looksLikeMarkdown(text)).toBe(expected);
  });
});
