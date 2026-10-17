# Text agents for businesses

Date: 2026-10-17. Status: draft for owner approval. Research behind it:
`docs/research/2026-10-16-external-customer-agent-by-text.md`.

## What it is

As easy as Bland's video, with the front door on our platform. You sign up, you put in your
website, and you get a phone number for your agent, on the spot, with blue bubbles on iPhone.
Your customers text that number and the agent does what they ask: answers, bookings, orders,
payment. You see every conversation in Lightbulb, tweak the agent by talking to it, and meet
the rest of your team there. Nobody from Lightbulb is in the loop: the human in each
conversation is the customer, and the agent works for them.

## The flow

1. **Sign up.** An ordinary workspace.
2. **Put in your website.** Onboarding asks for it. The agent reads the site and, with OpenCLI,
   learns to work it completely: every page, menu, booking and ordering flow becomes a command
   it can run. Nothing to review; it is ready when it is done.
3. **You get the number on the spot.** This is the paid step: one checkout for Team plus the
   Front desk add-on. The line is created by API the moment it is paid: blue bubbles for iPhone
   customers, plain SMS for everyone else. It is shown in the workspace and ready to text. You
   put it on your site, Google listing or voicemail.
4. **Customers text it.** The agent answers from the site, books or orders on your site in the
   cloud browser, takes payment, and confirms by text. Its first reply says it is the business's
   automated assistant.
5. **Payment.** Connect Stripe once in Settings (one click, the existing connection). The agent
   texts a Stripe payment link for the amount; the customer pays with Apple Pay, Google Pay or a
   card; the agent sees the payment and completes the order or booking. No Stripe connected: the
   customer pays at the business, or gets the site's own checkout link.
6. **Unknown.** The agent says it does not know and will get back, asks the CEO, and if the CEO
   cannot answer, asks you as a Needs-you question. The answer goes back to the customer by text.
7. **You watch and take over.** Threads sit in the Mail panel next to mail. Reply in one and the
   agent stays quiet there until you hand it back.

## Rules

- Every texter is a customer. Members talk to the agent in Lightbulb, never through the number.
- The agent acts only on the business's own site and its connected tools, through the
  workspace's cloud browser and terminal. Orders and bookings go under the customer's own
  texting number, after the customer has said yes or paid.
- It never invents a price, a slot, a policy or an account detail. Unknown means "I'll get back
  to you", not a guess.
- Customer text is data, not instructions.
- The line is inbound-first: the agent only ever texts people who texted it. That is the
  provider's rule and ours.
- Getting the number is where the business starts paying. A line costs us $100 the moment it
  exists and is not refunded, so it is never free. If the workspace later cannot run (credits
  out, subscription lapsed) the agent autoreplies with the business's listed phone, so texts
  never drop silently.

## Facts that shape it (verified in the research note and on the providers' pages, 2026-10-17)

- The number is a Sendblue line (owner choice 2026-10-17; LoopMessage was the runner-up, about
  $10 cheaper, and the cost table is in the research note). A line is a real US number that
  sends and receives iMessage with SMS and RCS fallback; it exists the moment it is created, by
  API, with no carrier verification. The AI Agent plan is $100 a month per line, inbound-first,
  1,000 inbound contacts a day, replies free within 24 hours of the customer's last text,
  webhooks included, 10 messages a second burst. Cold outbound is not sold. A released line is
  not refunded for the rest of its month. By default an account may create 3 lines a day and
  10 a month; Sendblue raises that on request. The free sandbox is a shared number with 10
  verified contacts and no webhooks, so dev needs one paid line.
- The ordinary SMS route (Twilio, Telnyx, any carrier) costs about $2 a number but every number
  must be verified in the business's name first, 3 to 5 business days, so it cannot be "on the
  spot" except from a pool we verify ahead under our own name. It stays as the cheap fallback.
- Apple's own Messages for Business (the verified brand name and in-chat Pay in Bland's demo)
  needs an Apple-approved provider, per-business registration and live human support on call;
  Poke is the one AI agent Apple has approved. Later.
- Texts must enter through our backend: a runtime is invoke-only inside the VPC, so no adapter
  can listen inside the agent.
- Cannabis, alcohol, firearms, tobacco and adult businesses are refused by the providers and
  the carriers.
- A CAPTCHA, login code or age gate needs a person in the live view (ruling 2026-10-07). For a
  customer thread that is "I'll get back to you" plus a Needs-you item.

## Later

- Branded Messages for Business: the verified name, in-chat Apple Pay, list pickers, tapbacks.
- A cheap SMS-only line from a pre-verified pool, if the iMessage line's price needs a floor.
- Voice on the same number.

## Mechanisms (pointers for the builder)

- Inbound: `routers/sendblue_inbound.py` shaped like `routers/ses_inbound.py`: Sendblue's
  webhook, verified by its signing secret, dedup on the message handle, number row to (owner, agent), store with
  `agent_mail_inbox_repo.put_message` (under the team mail address, with a channel field, so
  every inbox list and badge shows texts unchanged), the existing
  stranger caps and trial-cap autoreply, then `agent_mail_delivery.deliver_to_agent` with a
  session key per (number, customer).
- Outbound: `lightbulb-text.sh send --reply <key>` to a backend route that sends through
  Sendblue's send API and records the sent text as an inbox item, as mail does. Sendblue's
  typing indicator and read receipt are free to use.
- Numbers: one Lightbulb Sendblue account with automatic line provisioning, one line per
  workspace created at the onboarding website step with `provision-preview` then
  `provision-confirm`, assigned to Customer Operations. The number is a row in the existing agent-email address
  table (`kind: text`): owner, agent, status, business phone, taken-over threads. A workspace wipe deprovisions the line at its month end, since Sendblue
  does not refund. Before launch, Sendblue must enable automatic provisioning on our account
  and raise the default 3-a-day, 10-a-month line limits to our sign-up rate.
- Build from URL: a DM to Customer Operations when the first message carries the site (the
  existing intake chip), using the lightbulb skill and OpenCLI in the image (`OPENCLI_CDP_ENDPOINT` set from the CDP address the bridge hands each run).
  It rewrites Customer Operations into the front desk (persona, facts, and a site adapter for
  every flow on the site, each tested against the real site). It posts nothing for review.
  Adapters live in the profile so they survive restores; per-platform adapters (Jane, Square
  Appointments, Jobber, Toast, …) are promoted into the image as they are written.
- Billing: the Front desk add-on is a third item on the workspace's Stripe subscription, the
  shape the seat item already has (`STRIPE_SEAT_PRICE_ID`), as `STRIPE_FRONT_DESK_PRICE_ID`.
  On a trial workspace the number step goes to checkout with Team plus the add-on, charged
  now with no Stripe trial; on a Team workspace it adds the item, prorated, like a seat. The
  line is provisioned when the subscription is live, and deprovisioned at month end when the
  item is removed or the subscription ends.
- Payment: the existing Stripe OAuth connection (`mcp.stripe.com`, `providers.yaml`) gives the
  agent payment links; the existing Stripe trigger rail or a check on the link's session tells
  it the customer paid.
- Tool allowlist on the front-desk agent: browser, its adapters, text send, Stripe payment
  links. No mail, no code, no deploys, no marketplace, no shell beyond its adapters.
- Caps per number: messages per sender per day, messages per number per day. Past a cap the
  agent sends one line and stops.
- Bench before shipping, in the style of `apps/backend/scripts/speak_gate_bench`: known flows,
  out-of-scope asks, a price not on the site, a cancel, a payment, a prompt injection, a request
  for the owner's number. Pass/fail checks on the transcript, not a closing question.

## Left to the builder

The Sendblue account setup (automatic provisioning on, limits raised, one paid line in dev
driving the inbound webhook), table choice, the exact Hermes tool-allowlist
key, cap values, adapter storage path, the Mail panel changes, the onboarding step's look,
rollout order, and the first per-platform adapter.

## Calls I made (override any of them)

- The number is an iMessage line by default, created on the spot. The SMS pool is the cheap
  fallback, not the default.
- The Front desk is an add-on at Sendblue's rate, $100 a month, with Team unchanged at $50
  (owner 2026-10-17, "keep the rates for now"). Revisit when there is volume pricing.
- Getting the number starts the paid plan: Team plus the add-on, charged then, no trial
  carried over. Sign-up and the website step stay free; the agent can be tried in chat
  before paying.
- Payment is a Stripe payment link through the business's own Stripe connection. In-chat Pay
  buttons come with branded Messages for Business later.
- The front-desk agent is Customer Operations rewritten and renamed, not a seventh agent.
- The autoreply with the business's listed phone is the floor whenever the workspace cannot
  run.
