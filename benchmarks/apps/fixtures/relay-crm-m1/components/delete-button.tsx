"use client";
import { useRouter } from "next/navigation";

/** Delete `/api/<path>` after a confirm, then go to `after`. */
export function DeleteButton({ path, after, label }: { path: string; after: string; label: string }) {
  const router = useRouter();
  return (
    <button
      className="rounded border px-3 py-1.5 text-red-700"
      onClick={async () => {
        if (!confirm(`Delete this ${label}?`)) return;
        await fetch(`/api/${path}`, { method: "DELETE" });
        router.push(after);
        router.refresh();
      }}
    >
      Delete {label}
    </button>
  );
}
