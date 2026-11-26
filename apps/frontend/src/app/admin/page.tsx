// The admin's home is the Spark tab (the old fleet overview went with the messaging app).
export const dynamic = "force-dynamic";
// The backend sends PostHog 3 queries at a time, so a load with nothing cached can take up to 45 seconds.
export const maxDuration = 60;
export { default } from "./spark/page";
