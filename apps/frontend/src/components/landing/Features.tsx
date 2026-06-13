const FEATURES = [
  {
    num: "F.01",
    title: (
      <>
        Plugs into <em>your</em> stack
      </>
    ),
    desc: "Gmail, Slack, Salesforce, Notion — your agents connect natively to the tools your team relies on. No new apps to learn, no workflows to rebuild.",
  },
  {
    num: "F.02",
    title: (
      <>
        Always <em>in the loop</em>
      </>
    ),
    desc: "Message your agents on Slack, WhatsApp, or Discord — just like texting a teammate. Your container is always running, so your agents never stop working, even when you log off.",
  },
  {
    num: "F.03",
    title: (
      <>
        Acts <em>autonomously</em>
      </>
    ),
    desc: "Your pod doesn't just answer — it acts. Schedule tasks, run workflows, send messages, and complete multi-step jobs in the background while you focus on what matters.",
  },
];

type Chip = {
  label: string;
  tag?: string;
  logo?: string;
  dot?: boolean;
  ghost?: boolean;
};

const CHIPS_A: Chip[] = [
  { label: "Memory", tag: "installed", dot: true },
  { label: "Gmail", logo: "/logos/gmail.png" },
  { label: "Calendar", logo: "/logos/gcal.png" },
  { label: "Developer", tag: "installed", dot: true },
  { label: "Salesforce", logo: "/logos/salesforce.png" },
  { label: "Perplexity", dot: true },
];

const CHIPS_B: Chip[] = [
  { label: "Notion", dot: true },
  { label: "Browser", tag: "installed", dot: true },
  { label: "Slack", dot: true },
  { label: "Spreadsheets", dot: true },
  { label: "Cron jobs", dot: true },
  { label: "+ 28 more", ghost: true },
];

function ChipRun({ chips }: { chips: Chip[] }) {
  return (
    <>
      {chips.map((chip) => (
        <span
          key={chip.label}
          className={"ld-chip" + (chip.ghost ? " ld-chip--ghost" : "")}
        >
          {chip.logo ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={chip.logo}
              width={14}
              height={14}
              alt=""
              style={{ borderRadius: 3 }}
            />
          ) : chip.dot ? (
            <span className="ld-chip-dot" />
          ) : null}
          <span>{chip.label}</span>
          {chip.tag && <span className="ld-chip-tag">{chip.tag}</span>}
        </span>
      ))}
    </>
  );
}

export function Features() {
  return (
    <section className="ld-features" id="features">
      <div className="ld-section">
        <div className="ld-kicker" data-ld-reveal>
          your pod
          <span className="ld-kicker-index">03 — 08</span>
        </div>
        <h2 className="ld-h2" data-ld-reveal>
          Agents that fit into <em>your</em> tools.
        </h2>

        <div className="ld-feat-list" data-ld-reveal-group>
          {FEATURES.map((f) => (
            <div className="ld-feat-row" key={f.num}>
              <span className="ld-feat-num">{f.num}</span>
              <h3>{f.title}</h3>
              <p>{f.desc}</p>
            </div>
          ))}
        </div>

        <div className="ld-skills" data-ld-reveal>
          <div className="ld-skills-lede">
            <h3>
              Upgrade them <em>endlessly.</em>
            </h3>
            <p>
              Install new skills from a curated library. Your agents learn to
              use them automatically.
            </p>
          </div>
          <div className="ld-skills-rows" aria-hidden="true">
            <div className="ld-chip-track">
              <ChipRun chips={CHIPS_A} />
              <ChipRun chips={CHIPS_A} />
            </div>
            <div className="ld-chip-track ld-chip-track--reverse">
              <ChipRun chips={CHIPS_B} />
              <ChipRun chips={CHIPS_B} />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
