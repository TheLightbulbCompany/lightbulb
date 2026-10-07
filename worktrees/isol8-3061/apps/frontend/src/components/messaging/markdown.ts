import { defaultUrlTransform } from "react-markdown";
import type { UrlTransform } from "react-markdown";
import { MENTION_SCHEME } from "@lightbulb/shared";

// react-markdown's default urlTransform blanks any href whose scheme it doesn't
// recognize (http/https/mailto/relative/…), so a `mention:` link never reaches
// an `a` override's href at all unless we carve out an exception for it here.
export const roomUrlTransform: UrlTransform = (value) => (value.startsWith(MENTION_SCHEME) ? value : defaultUrlTransform(value));
