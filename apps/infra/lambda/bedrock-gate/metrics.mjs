// EMF metrics to stdout — same shape as the meter Lambda's _emit_metric, so the
// gate's metrics land on CloudWatch with an env dimension the alarms key off.
// Namespace Isol8/BedrockGate. Never throws (telemetry must not break the path).
export function emit(name, env, extra = {}) {
  try {
    const meterEnv = env?.GATE_ENV || "unknown";
    const line = {
      _aws: {
        Timestamp: Date.now(),
        CloudWatchMetrics: [
          { Namespace: "Isol8/BedrockGate", Dimensions: [["env"]], Metrics: [{ Name: name, Unit: "Count" }] },
        ],
      },
      env: meterEnv,
      [name]: 1,
      ...extra,
    };
    process.stdout.write(JSON.stringify(line) + "\n");
  } catch {
    /* metrics must never break the request path */
  }
}
