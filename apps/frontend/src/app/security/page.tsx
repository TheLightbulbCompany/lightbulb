import Link from "next/link";

import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { SUPPORT_EMAIL, SUPPORT_MAILTO } from "@/lib/domain";
import { JsonLd, breadcrumbJsonLd, pageMetadata } from "@/lib/seo";

// Every line here is either in the Privacy Policy (section 15 and "How can you
// review, update, or delete the data we collect") or one of the three provider
// paths a workspace can choose. Do not add a claim the policy does not make.
export const metadata = pageMetadata({
  title: "What happens to your data — Lightbulb",
  description:
    "Each workspace runs in its own isolated environment on AWS. Your content goes to the model provider your team chose, and we do not sell it or use it to train AI models.",
  path: "/security",
});

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
          <li>
            <strong>Your own key.</strong> Sent to OpenAI or Anthropic under your own agreement with
            them.
          </li>
        </ul>
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
