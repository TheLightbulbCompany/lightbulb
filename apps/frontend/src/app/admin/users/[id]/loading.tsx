export default function Loading() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading user detail">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="h-48 animate-pulse rounded-lg bg-card" />
        ))}
      </div>
    </div>
  );
}
