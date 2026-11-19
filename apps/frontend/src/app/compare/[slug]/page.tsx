import { notFound } from "next/navigation";

import { ComparePageView, compareMetadata } from "@/components/compare/ComparePageView";
import { COMPARE_PAGES, COST, getComparePage } from "@/content/compare";
import { getPricing, voicePlanLine } from "@/lib/pricing";

export const dynamicParams = false;
/** The price is re-read every 5 minutes, as on /pricing. */
export const revalidate = 300;

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
  // The real price, read from the billing API like /pricing; the neutral line stays when it can't be read.
  const line = voicePlanLine(await getPricing());
  const cost = line ? `${line} Month to month; cancel yourself in Settings.` : COST;
  return <ComparePageView page={{ ...page, rows: page.rows.map((r) => (r.ours === COST ? { ...r, ours: cost } : r)) }} />;
}
