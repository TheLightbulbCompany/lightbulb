import type { Metadata } from "next";

import { browseListings } from "@/lib/marketplace/api";
import { AgentsBrowse } from "./AgentsBrowse";
import { JsonLd, absoluteUrl, breadcrumbJsonLd, pageMetadata, softwareApplicationJsonLd } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Browse AI Agents for Business Workflows | Lightbulb",
  description: "Search deployable AI agents for company workflows, including operations, customer support, research, sales, and internal automation.",
  path: "/marketplace/agents",
});

export default async function Agents() {
  let items: Awaited<ReturnType<typeof browseListings>>["items"] = [];
  try {
    const result = await browseListings({ format: "openclaw", limit: 50 });
    items = result.items;
  } catch {
    // Best-effort during local dev.
  }

  return (
    <main>
      <JsonLd data={{ ...softwareApplicationJsonLd, name: "Lightbulb deployable AI agents", url: absoluteUrl("/marketplace/agents") }} />
      <JsonLd data={breadcrumbJsonLd([{ name: "Home", path: "/" }, { name: "Marketplace", path: "/marketplace" }, { name: "Agents", path: "/marketplace/agents" }])} />
      <section className="border-b border-[var(--edge)] px-6 py-14">
        <div className="mx-auto max-w-6xl">
          <div className="mb-4 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--midnight-muted)]">
            Browse the marketplace
          </div>
          <h1 className="font-editorial-serif text-5xl leading-[1.0] text-[var(--midnight)] md:text-6xl">
            Browse <em className="text-[var(--midnight)]">agents.</em>
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-7 text-[var(--midnight-soft)]">
            {items.length} {items.length === 1 ? "agent" : "agents"} ready to deploy into your Lightbulb
            container.
          </p>
        </div>
      </section>

      <AgentsBrowse items={items} />
    </main>
  );
}
