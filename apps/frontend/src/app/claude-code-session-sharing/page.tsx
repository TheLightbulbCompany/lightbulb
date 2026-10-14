import { ComparePageView, compareMetadata } from "@/components/compare/ComparePageView";
import { getComparePage } from "@/content/compare";

const page = getComparePage("shared-claude-code-sessions")!;

export const metadata = compareMetadata(page);

export default function SessionSharingPage() {
  return <ComparePageView page={page} />;
}
