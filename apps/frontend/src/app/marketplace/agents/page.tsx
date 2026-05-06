import { browseListings } from "@/lib/marketplace/api";
import { AgentsBrowse } from "./AgentsBrowse";

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
      <section className="border-b border-[var(--edge)] px-6 py-14">
        <div className="mx-auto max-w-6xl">
          <div className="mb-4 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--mute)]">
            Browse the marketplace
          </div>
          <h1 className="font-editorial-serif text-5xl leading-[1.0] text-[var(--ink)] md:text-6xl">
            Browse <em className="text-[var(--forest)]">agents.</em>
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-7 text-[var(--ink-soft)]">
            {items.length} {items.length === 1 ? "agent" : "agents"} ready to deploy into your Isol8
            container.
          </p>
        </div>
      </section>

      <AgentsBrowse items={items} />
    </main>
  );
}
