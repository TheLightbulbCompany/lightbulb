import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { SUPPORT_EMAIL, SUPPORT_MAILTO } from "@/lib/domain";
import { JsonLd, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

// Every line here is either in the Privacy Policy (section 15 and "How can you
// review, update, or delete the data we collect") or one of the three provider
// paths a workspace can choose. Do not add a claim the policy does not make.
// The memory, capture, upload and evaluator rows are covered by the privacy
// policy rewrite that must land before this page ships; until then the rule
// above stands for every other line.
export const metadata = pageMetadata({
  title: "What happens to your data — Lightbulb",
  description:
    "Each workspace runs in its own isolated environment on AWS. Your content goes to the model provider your team chose, and we do not sell it or use it to train AI models.",
  path: "/security",
});

const CLAIMS: { claim: string; evidence: string }[] = [
  {
    claim: "Your keys never leave your computer",
    evidence:
      "The computer's key is a file only you can read and it is never uploaded. A cloud member's key comes from a secret at boot and is never written to storage.",
  },
  {
    claim: "Relays see ciphertext, keys, addresses, timing and sizes, never text",
    evidence:
      "Messages are encrypted end to end between member keys. A relay carries the ciphertext when a direct route fails.",
  },
  {
    claim: "On the free network, memory lives on members' computers and moves only between them",
    evidence:
      "Memory is a set of signed entries kept on each member's computer. Sync sends entries only to admitted members, and your private entries only to your own computers. In a workspace, the workspace's always-on member keeps a copy of the team entries, never the private entries on your computers, and the facts it mirrors for search are reachable only by that member and our backend. They are stored in AWS (Bedrock AgentCore) in our account. A workspace wipe deletes them.",
  },
  {
    claim: "Nothing from a laptop session leaves without sign-in and a choice",
    evidence:
      "Capture is local by default. Upload is a setting you choose once at sign-in: keep local, metadata only, or full. You can change it with lightbulb sessions upload.",
  },
  {
    claim: "A fact with a secret in it is refused; workspace room turns are scrubbed",
    evidence:
      "Before an agent's remembered fact is saved, on your computer or in a workspace, its text is checked against a list of patterns: API keys, tokens, private-key blocks and credentials in URLs. A match is refused with the reason, not saved with the secret scrubbed out. Workspace room turns are handled differently: secret-shaped text is scrubbed out of each turn before the workspace memory stores it and extracts facts from it. On your computer, a captured prompt, reply or tool-call summary that matches is kept only as the name of the pattern it matched.",
  },
  {
    claim: "Every fact carries who, where and when; disagreements are shown, not settled",
    evidence:
      "Each entry is signed by its author and records the tool, repo and session it came from, and when. When two people hold different current values, recall shows both with names and dates and marks the fact contested. Neither overwrites the other.",
  },
  {
    claim: "You can see exactly what your agent was told",
    evidence:
      "lightbulb memory last shows what was injected at the last session start. The block is logged on your computer.",
  },
  {
    claim: "Forgetting reaches every copy that comes online",
    evidence:
      "A forget is a signed tombstone that keeps the id and drops the content on every copy as each computer receives it. The one limit: a computer that stays offline forever keeps what it held before the forget, as it keeps messages it already received.",
  },
  {
    claim: "Agents ask before they spend",
    evidence: "Spend gates and approvals are shipped.",
  },
  {
    claim: "Teammates see presence, not your sessions or files",
    evidence:
      "Teammates see whether you are online. Capture stays on your computer unless you upload it. In a workspace, uploaded sessions appear in the workspace timeline to its members, because the workspace owns that record.",
  },
  {
    claim: "In a workspace, a wipe deletes everything, including the extracted facts",
    evidence:
      "A workspace wipe removes the workspace's memory, its uploaded sessions and the drafts the evaluator suggested, along with the rest of its data.",
  },
];

const SECTIONS: { title: string; body: React.ReactNode }[] = [
  {
    title: "Where it lives",
    body: (
      <p>
        Each workspace runs in its own private, isolated cloud environment on AWS. Your agents and
        your workspace data are stored there, apart from every other workspace.
      </p>
    ),
  },
  {
    title: "Which model provider sees it",
    body: (
      <>
        <p>
          To do the work you ask for, what you send your agents (messages, files and data in the
          workspace) goes to the model provider your team chose:
        </p>
        <ul>
          <li>
            <strong>Lightbulb-managed.</strong> Sent through OpenRouter to the model&rsquo;s provider.
          </li>
          <li>
            <strong>Your ChatGPT account.</strong> Sent to OpenAI under your ChatGPT plan, and your
            account&rsquo;s own data settings apply.
          </li>
        </ul>
        <p>
          Workspaces already configured with a saved OpenAI or Anthropic key keep using it. New
          workspaces cannot choose BYO keys, and new keys cannot be added.
        </p>
      </>
    ),
  },
  {
    title: "Training",
    body: (
      <p>
        We do not sell your content, and we do not use it to train AI models. Content processed
        through the providers&rsquo; standard API and managed services is not used by them to train
        their models.
      </p>
    ),
  },
  {
    title: "Payments",
    body: <p>Card details are handled and stored by Stripe, not by us.</p>,
  },
  {
    title: "Keeping and deleting",
    body: (
      <p>
        We keep your workspace content while your account is active. Ask us to close your account
        and we deactivate or delete it and its information from our active databases. We may keep
        some records to prevent fraud or to meet legal requirements.
      </p>
    ),
  },
  {
    title: "What we do not claim",
    body: (
      <p>
        We do not hold a security certification such as SOC 2 today. If your team needs an answer
        in writing, email <a href={SUPPORT_MAILTO}>{SUPPORT_EMAIL}</a> and a founder replies within
        a day.
      </p>
    ),
  },
  {
    title: "The network",
    body: (
      <>
        <p>
          The free <Link href="/network">Lightbulb network</Link> runs on your own computers. What it keeps on
          each one: the computer&rsquo;s key (<code>~/.lightbulb/connect/identity.key</code>, which never leaves
          it), the signed list of who is on your network, an outbox for messages to computers that are offline,
          and, only if you linked a workspace, a sign-in proof.
        </p>
        <p>
          What travels: your messages, encrypted end to end between computers. A relay is used only when a
          direct route fails, and it carries ciphertext. Message text, names, keys, codes and folder paths never
          leave your computers in the clear, and Lightbulb runs no server in the message path.
        </p>
        <p>
          Usage counts: the command sends anonymous events (install, update, join, invite, and a daily count of
          messages and connected agents) with the version, operating system, CPU type and a random install id.
          Never message text, names, keys or paths. Turn them off with <code>lightbulb telemetry off</code> or{" "}
          <code>DO_NOT_TRACK=1</code>. To leave: <code>lightbulb leave</code>; to remove everything:{" "}
          <code>lightbulb setup --remove</code>. The full list of what is stored, what travels and what is logged
          is on the <Link href="/docs/agent-network-security">network security page</Link>.
        </p>
      </>
    ),
  },
  {
    title: "What leaves your machine",
    body: (
      <ul>
        <li>
          <strong>Messages.</strong> Encrypted end to end, computer to computer.
        </li>
        <li>
          <strong>Memory.</strong> On the free network, facts your agents save are kept on your
          computers and the computers of your team, and move only between members. An entry marked
          for you alone syncs only to your own computers. In a workspace, the workspace&apos;s
          always-on member keeps a copy of the team entries, and the facts it mirrors for search
          are reachable only by that member and our backend. Workspace memory is processed and
          stored in AWS (Bedrock AgentCore) in our account until it is deleted or the workspace is
          wiped. The details are on{" "}
          <Link href="/docs/memory">memory on your computers and in your workspace</Link>.
        </li>
        <li>
          <strong>Capture.</strong> Your computer records each session (the tool, the repo, token
          counts), each tool call (its name, a 200-character summary, how long it took and
          whether it worked), and your prompts and the agents&apos; replies, each cut to 4 KB. A
          tool-call summary never holds file contents, diffs or command output; a reply can,
          because it is what the agent wrote. Prompts and replies are kept on your computer only,
          never uploaded, and deleted after 90 days. <code>lightbulb sessions turns off</code>{" "}
          stops recording them.
        </li>
        <li>
          <strong>Upload.</strong> Only if you sign in and choose it: metadata only, or full
          sessions. Uploads go to Lightbulb under your identity and are stored in our AWS account.
          We keep them 30 days on a free account and a year in a workspace. An upload never holds
          your prompts or the agents&apos; replies. The evaluator that suggests memories reads full
          uploads and nothing else, and it runs in AWS (Bedrock AgentCore) in our account. We do
          not sell uploads.
        </li>
      </ul>
    ),
  },
  {
    title: "The claims and the evidence for them",
    body: (
      <table aria-label="Security claims and the evidence for them" className="w-full border-collapse text-left text-base leading-7">
        <thead>
          <tr>
            <th scope="col" className="w-2/5 border-b border-current/20 py-2 pr-4 align-top">Claim</th>
            <th scope="col" className="border-b border-current/20 py-2 align-top">What it rests on</th>
          </tr>
        </thead>
        <tbody>
          {CLAIMS.map((c) => (
            <tr key={c.claim}>
              <td className="border-b border-current/10 py-3 pr-4 align-top font-semibold">{c.claim}</td>
              <td className="border-b border-current/10 py-3 align-top">{c.evidence}</td>
            </tr>
          ))}
        </tbody>
      </table>
    ),
  },
];

export default function SecurityPage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd
        data={breadcrumbJsonLd([
          { name: "Home", path: "/" },
          { name: "What happens to your data", path: "/security" },
        ])}
      />
      <Navbar />
      <section className="mx-auto max-w-3xl px-6 py-20 md:py-28">
        <p className="ld-mono text-xs uppercase tracking-[0.18em] text-[var(--midnight-muted)]">
          Your data
        </p>
        <h1 className="mt-5 font-editorial-serif text-5xl leading-[0.98] text-[var(--midnight)] md:text-6xl">
          What happens to your data
        </h1>
        <div className="mt-12 space-y-10">
          {SECTIONS.map((s) => (
            <div key={s.title}>
              <h2 className="font-editorial-serif text-2xl text-[var(--midnight)]">{s.title}</h2>
              <div className="mt-3 space-y-3 text-lg leading-8 text-[var(--midnight-soft)] [&_a]:underline [&_li]:mt-2 [&_ul]:list-disc [&_ul]:pl-6">
                {s.body}
              </div>
            </div>
          ))}
        </div>
        <p className="mt-12 text-base text-[var(--midnight-soft)]">
          The full terms are in our <Link href="/privacy" className="underline">Privacy Policy</Link>.
        </p>
      </section>
      <Footer />
    </main>
  );
}
