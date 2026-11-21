// The Mac app's URL scheme: the release app owns `spark://`; the dev build, used with the dev site, `spark-dev://`.
export function sparkScheme() {
  const host = typeof window === "undefined" ? "" : window.location.hostname;
  return host === "localhost" || host.startsWith("dev.") ? "spark-dev" : "spark";
}
