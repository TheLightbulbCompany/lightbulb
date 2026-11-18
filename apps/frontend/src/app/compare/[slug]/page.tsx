import { notFound } from "next/navigation";

import { ComparePageView, compareMetadata } from "@/components/compare/ComparePageView";
import { COMPARE_PAGES, getComparePage } from "@/content/compare";

export const dynamicParams = false;

export function generateStaticParams() {
  return COMPARE_PAGES.map((p) => ({ slug: p.slug }));
}

type Props = { params: Promise<{ slug: string }> };

export async function generateMetadata({ params }: Props) {
  const page = getComparePage((await params).slug);
  return page ? compareMetadata(page) : {};
}

export default async function ComparePage({ params }: Props) {
  const page = getComparePage((await params).slug);
  if (!page) notFound();
  return <ComparePageView page={page} />;
}
