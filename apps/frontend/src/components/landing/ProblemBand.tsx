/** The problem, right after the hero. The one figure is third-party and cited. */
export function ProblemBand() {
  return (
    <section className="lb-problem" id="problem">
      <div className="lb-wrap">
        <h2 className="lb-h2">The phone rings while your hands are in someone&rsquo;s hair.</h2>
        <p className="lb-problem-body">
          You can&rsquo;t pick up mid-service, after closing, or while you&rsquo;re checking someone out. Plenty of
          callers won&rsquo;t leave a voicemail. They try the next place on the list. Calling back means stopping
          work, and by then some have already booked somewhere else.
        </p>
        <p className="lb-problem-src">
          Across 70 million business calls, 44% never reached a person. (
          <a href="https://invoca.com/reports/the-invoca-lead-conversion-benchmarks-report-2026" rel="noopener">
            Invoca, 2026 benchmarks
          </a>
          )
        </p>
      </div>
    </section>
  );
}
