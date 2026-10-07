import { browseListings } from "@/lib/marketplace/api";
import { CompaniesBrowse } from "./CompaniesBrowse";

export default async function Companies() {
  let items: Awaited<ReturnType<typeof browseListings>>["items"] = [];
  try {
    const result = await browseListings({ format: "company", limit: 50 });
    items = result.items;
  } catch {
    // Best-effort during local dev.
  }

  return (
    <main>
      <section className="border-b border-[var(--edge)] px-6 py-14">
        <div className="mx-auto max-w-6xl">
          <div className="mb-4 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--midnight-muted)]">
            The open company directory
          </div>
          <h1 className="font-editorial-serif text-5xl leading-[1.0] text-[var(--midnight)] md:text-6xl">
            Deploy a whole <em className="text-[var(--midnight)]">company.</em>
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-7 text-[var(--midnight-soft)]">
            {items.length} pre-built agent {items.length === 1 ? "company" : "companies"} — org
            charts, skills, and workflows included.
          </p>
        </div>
      </section>

      <CompaniesBrowse items={items} />
    </main>
  );
}
