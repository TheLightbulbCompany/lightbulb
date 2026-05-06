"use client";

import { useRef, useState } from "react";

import type { AdminCatalog, CatalogVersion } from "@/app/admin/_lib/api";

import { CatalogRowActions } from "./CatalogRowActions";
import { fetchVersions } from "./fetchVersions";
import { VersionsPanel } from "./VersionsPanel";

interface CatalogPageClientProps {
  catalog: AdminCatalog;
}

/**
 * Client shell for /admin/catalog. Owns the selected-slug + loaded-versions
 * state for the right-side VersionsPanel. The parent Server Component passes
 * the initial catalog listing (server-rendered with the bearer token) and we
 * lazy-load per-slug version history through a Server Action so the token
 * never leaves the server.
 */
export function CatalogPageClient({ catalog }: CatalogPageClientProps) {
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [versions, setVersions] = useState<CatalogVersion[] | null>(null);
  const [retiredOpen, setRetiredOpen] = useState(false);
  // Increments on every openVersionsFor invocation and on panel close; the
  // in-flight request captures the current value at send time and discards
  // its own result if it no longer matches. Prevents a stale response from
  // overwriting the panel when the operator clicks a second row (or closes
  // the panel) before the first fetch resolves.
  const versionsRequestIdRef = useRef(0);

  async function openVersionsFor(slug: string) {
    const requestId = ++versionsRequestIdRef.current;
    setSelectedSlug(slug);
    setVersions(null);
    const result = await fetchVersions(slug);
    if (requestId !== versionsRequestIdRef.current) {
      // A newer request (or panel close) superseded this one — drop the stale result.
      return;
    }
    setVersions(result);
  }

  return (
    <div className="p-6">
      <h1 className="text-2xl font-semibold text-neutral-100 mb-4">Catalog</h1>

      <section className="mb-8">
        <h2 className="text-xs uppercase tracking-wide text-neutral-500 mb-2">
          Live ({catalog.live.length})
        </h2>
        {catalog.live.length === 0 ? (
          <p className="text-sm text-neutral-400">
            No agents published yet. Publish one from its admin detail page.
          </p>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="text-left text-neutral-500">
              <tr>
                <th className="py-2 px-3">Slug</th>
                <th className="py-2 px-3">Name</th>
                <th className="py-2 px-3">Version</th>
                <th className="py-2 px-3">Published</th>
                <th className="py-2 px-3">By</th>
                <th className="py-2 px-3"></th>
              </tr>
            </thead>
            <tbody>
              {catalog.live.map((e) => (
                <tr
                  key={e.slug}
                  className="border-t border-neutral-800 text-neutral-200"
                >
                  <td className="py-2 px-3">
                    <span aria-hidden className="mr-1">
                      {e.emoji || "🤖"}
                    </span>
                    {e.slug}
                  </td>
                  <td className="py-2 px-3">{e.name}</td>
                  <td className="py-2 px-3">v{e.current_version}</td>
                  <td className="py-2 px-3 text-neutral-400">
                    {e.published_at}
                  </td>
                  <td className="py-2 px-3 text-neutral-400">
                    {e.published_by}
                  </td>
                  <td className="py-2 px-3">
                    <CatalogRowActions
                      slug={e.slug}
                      name={e.name}
                      onOpenVersions={openVersionsFor}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>

      <section>
        <button
          type="button"
          onClick={() => setRetiredOpen((o) => !o)}
          className="text-xs uppercase tracking-wide text-neutral-500 mb-2 hover:text-neutral-300"
        >
          {retiredOpen ? "▾" : "▸"} Retired ({catalog.retired.length})
        </button>
        {retiredOpen && catalog.retired.length > 0 && (
          <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="text-left text-neutral-500">
              <tr>
                <th className="py-2 px-3">Slug</th>
                <th className="py-2 px-3">Last version</th>
                <th className="py-2 px-3">Retired at</th>
                <th className="py-2 px-3">Retired by</th>
              </tr>
            </thead>
            <tbody>
              {catalog.retired.map((r) => (
                <tr
                  key={r.slug}
                  className="border-t border-neutral-800 text-neutral-400"
                >
                  <td className="py-2 px-3">{r.slug}</td>
                  <td className="py-2 px-3">v{r.last_version}</td>
                  <td className="py-2 px-3">{r.retired_at}</td>
                  <td className="py-2 px-3">{r.retired_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>

      <VersionsPanel
        slug={selectedSlug}
        versions={versions}
        onClose={() => {
          versionsRequestIdRef.current += 1;
          setSelectedSlug(null);
          setVersions(null);
        }}
      />
    </div>
  );
}
