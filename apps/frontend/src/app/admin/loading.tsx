export default function Loading() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading">
      <div className="h-8 w-48 animate-pulse rounded-lg bg-card" />
      <div className="h-32 animate-pulse rounded-lg bg-card" />
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {Array.from({ length: 5 }, (_, i) => (
          <div key={i} className="h-20 animate-pulse rounded-lg bg-card" />
        ))}
      </div>
      <div className="h-64 animate-pulse rounded-lg bg-card" />
    </div>
  );
}
