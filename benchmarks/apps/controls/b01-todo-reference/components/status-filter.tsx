"use client";
import { useRouter } from "next/navigation";

export function StatusFilter({ value }: { value: string }) {
  const router = useRouter();
  return (
    <select data-testid="filter-status" value={value} onChange={(e) => router.push(e.target.value === "all" ? "/tasks" : `/tasks?status=${e.target.value}`)} className="rounded border px-2 py-1">
      <option value="all">all</option>
      <option value="open">open</option>
      <option value="done">done</option>
    </select>
  );
}
