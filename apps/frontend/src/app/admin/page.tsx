// The admin's home is the Spark tab (the old fleet overview went with the messaging app).
export const dynamic = "force-dynamic";
// The backend may take up to 25 seconds (the first load after a query changes); the API gateway allows 30.
export const maxDuration = 60;
export { default } from "./spark/page";
