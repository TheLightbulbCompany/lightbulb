export default function Loading() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading listings">
      <div className="h-8 w-36 animate-pulse rounded-lg bg-card" />
      <div className="h-8 animate-pulse rounded-lg bg-card" />
      {Array.from({ length: 3 }, (_, i) => (
        <div key={i} className="h-36 animate-pulse rounded-lg bg-card" />
      ))}
    </div>
  );
}
