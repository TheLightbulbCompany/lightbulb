/**
 * Official third-party LLM-provider logo colours — the one place in
 * components/chat that may hold a colour literal, because a vendor's brand
 * mark is theirs and recolouring it onto our tokens would misrepresent it.
 * Exempt from the brand-token sweep by design; nothing else may import a hex.
 */
export const PROVIDER_BRAND = {
  ai21: "#6336E7",
  amazon: "#FF9900",
  // Wordmark, not a brand colour: AWS draws it in whatever the ground is not.
  // A CSS value because ProviderIcons applies it as an SVG `fill` presentation
  // attribute, so it follows the ground (midnight on cream) instead of the
  // literal #FFFFFF that was invisible on the cream popover.
  amazonWordmark: "var(--foreground)",
  anthropic: "#D4A574",
  cohere: "#39594D",
  cohereViolet: "#D18EE2",
  cohereCoral: "#FF7759",
  deepseek: "#4D6BFE",
  google: "#4285F4",
  meta: "#0082FB",
  metaMark: "#0467DF",
  minimax: "#F23F5D",
  minimaxGradientFrom: "#E2167E",
  minimaxGradientTo: "#FE603C",
  mistral: "#FA520F",
  moonshot: "#8B8BF5",
  nvidia: "#74B71B",
  nvidiaMark: "#76B900",
  openai: "#10A37F",
  qwen: "#615CED",
  qwenGradientFrom: "#6336E7",
  qwenGradientTo: "#6F69F7",
  zhipu: "#3859FF",
} as const;
