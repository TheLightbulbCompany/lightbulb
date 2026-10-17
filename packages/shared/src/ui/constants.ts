/**
 * Card constants with no dependencies, so the shared barrel can export them without
 * pulling OpenUI's parser into every consumer. The vocabulary itself is `./library`,
 * reached through the `@lightbulb/shared/cards` subpath.
 */
export const CARD_FENCE = "openui-lang";
export const CARD_ROOT = "Stack";
/** A fence past this renders as code, never a card. */
export const CARD_MAX_BYTES = 16 * 1024;
/** A Choice shows at most this many options; the rest fold under "+N more". */
export const CARD_MAX_OPTIONS = 8;
