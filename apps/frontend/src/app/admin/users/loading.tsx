export default function Loading() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading users">
      <div className="h-8 w-32 animate-pulse rounded-lg bg-zinc-900/40" />
      <div className="h-10 animate-pulse rounded-lg bg-zinc-900/40" />
      <div className="h-96 animate-pulse rounded-lg bg-zinc-900/40" />
    </div>
  );
}
