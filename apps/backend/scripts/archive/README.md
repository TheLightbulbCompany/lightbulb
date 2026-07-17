# Archived one-shot scripts

Run-once backfills/migrations that already executed against prod and can never
usefully run again (the mechanism they operate on is gone, or the data they
backfilled is now written at creation time). Kept as an operational record —
do NOT re-run. Their tests were deleted when they moved here; recover via git
history if a script needs to be resurrected as a template.

Live, re-runnable ops scripts stay in `apps/backend/scripts/`.
