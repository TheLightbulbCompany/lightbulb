import { notFound } from "next/navigation";

import { ComparePageView, compareMetadata } from "@/components/compare/ComparePageView";
import { COMPARE_PAGES, getComparePage } from "@/content/compare";

const routed = COMPARE_PAGES.filter((p) => p.path === `/compare/${p.slug}`);

// The Lightbulb price cell reads the billing API; refresh with /pricing.
export const revalidate = 300;

export const dynamicParams = false;

export function generateStaticParams() {
  return routed.map((p) => ({ slug: p.slug }));
}

type Props = { params: Promise<{ slug: string }> };

export async function generateMetadata({ params }: Props) {
  const page = getComparePage((await params).slug);
  return page ? compareMetadata(page) : {};
}

export default async function ComparePage({ params }: Props) {
  const page = getComparePage((await params).slug);
  if (!page || !routed.includes(page)) notFound();
  return <ComparePageView page={page} />;
}
