import { useWindowDimensions } from "react-native";

/**
 * True once the reader's text size is large enough that a label sitting in a
 * fixed-width column beside its value would break mid-word.
 *
 * UIKit's own list rows do the same thing: below the accessibility sizes a
 * property row is `LABEL   value`, and at them it stacks to
 * `LABEL` / `value`, giving each the full width. The threshold is iOS's own —
 * the accessibility sizes (AX1 and up) begin at a scale of 1.35.
 */
export function useStackedRows(): boolean {
  return useWindowDimensions().fontScale >= 1.35;
}

/**
 * Scales a height written in points for the default text size.
 *
 * A box sized to hold N lines of text has to grow with the text, or a reader
 * on a large size sees one line where everyone else sees five.
 */
export function useScaledHeight(points: number): number {
  return points * useWindowDimensions().fontScale;
}
