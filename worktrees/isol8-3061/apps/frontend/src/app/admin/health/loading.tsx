export default function Loading() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading system health">
      <div className="h-8 w-56 animate-pulse rounded-lg bg-card" />
      <div className="h-28 animate-pulse rounded-lg bg-card" />
      <div className="h-36 animate-pulse rounded-lg bg-card" />
      <div className="h-48 animate-pulse rounded-lg bg-card" />
    </div>
  );
}
