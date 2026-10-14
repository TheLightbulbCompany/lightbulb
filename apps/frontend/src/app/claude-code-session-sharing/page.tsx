import { ComparePageView, compareMetadata } from "@/components/compare/ComparePageView";
import { getComparePage } from "@/content/compare";

const page = getComparePage("shared-claude-code-sessions")!;

// The Lightbulb price cell reads the billing API; refresh with /pricing.
export const revalidate = 300;

export const metadata = compareMetadata(page);

export default function SessionSharingPage() {
  return <ComparePageView page={page} />;
}
