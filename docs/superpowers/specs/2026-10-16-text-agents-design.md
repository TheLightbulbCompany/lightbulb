# Text agents for businesses

Date: 2026-10-17. Status: settled by owner rulings; provider changed to Photon 2026-10-17. Research behind it:
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

- One number, two kinds of texter (owner 2026-10-17). A texter whose number a workspace member has
  linked as theirs is a member: their text is posted into the workspace's main room as that
  member, marked "sent by text", and the normal routing (listen modes, Jev) picks the agent, who
  answers in the room and texts back. Every other texter is a customer and goes to the front desk.
  Members link their number by texting LINK and the six-digit code the Mail panel shows to the
  business's number (one live code per member, 15 minutes); the webhook decides from the line's own
  row. A member who leaves the workspace is unlinked by the membership webhook; a text the main room
  refuses goes to the front desk and the link stays.
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

- The number is a Photon dedicated iMessage line (owner ruling 2026-10-17, replacing Sendblue:
  Photon carries WhatsApp, Telegram and voice on the same platform and Hermes has a native
  Photon plugin; facts in `docs/research/2026-10-17-photon-integration-facts.md`). A line is a
  real US number that sends and receives iMessage with SMS and RCS fallback, created by API on
  Photon's Business plan at $250 a line a month ($250 monthly minimum, 50 lines a project,
  pro-rated on release; cash refund terms unconfirmed). Photon has two API products; we use
  Photon 2.0 (`api.photon.codes/v1`, Bearer project key, public beta since 2026-10-15), the
  only one with an HTTP send path, typing, read receipts, attachment download and
  Standard-Webhooks delivery. Its line creation is asynchronous (a 202 operation that "can stay
  running for hours" when capacity is short), so "on the spot" is the fast case and "your number
  is being set up" the slow one; one dev purchase measures the wait. Inbound media arrives as metadata; bytes are a
  second call. There is no sandbox: dev buys one line against the $100 starting credit.
  Hermes's own Photon plugin is a personal always-on gateway (a Node sidecar holding a gRPC
  stream); it does not fit an invoke-only runtime, so texts still enter through our backend.
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

- Inbound: `routers/photon_inbound.py` shaped like `routers/ses_inbound.py`: Photon's
  Standard-Webhooks delivery (`webhook-id`/`webhook-timestamp`/`webhook-signature`, HMAC over
  `id.timestamp.body`, 5-minute window), dedup on the message id, line row to (owner, agent), store with
  `agent_mail_inbox_repo.put_message` (under the team mail address, with a channel field, so
  every inbox list and badge shows texts unchanged), the existing
  stranger caps and trial-cap autoreply, then `agent_mail_delivery.deliver_to_agent` with a
  session key per (number, customer).
- Outbound: `lightbulb-text.sh send --reply <key>` to a backend route that sends through
  Photon's `POST /v1/projects/{id}/messages` (sender = the line's agent user, space = the
  customer's DM space from the stored inbound row, an `Idempotency-Key` per send) and records
  the sent text as an inbox item, as mail does. Typing and read receipts are one call each.
- Numbers: one Lightbulb Photon 2.0 project on the Business plan. The Stripe webhook asks Photon
  for a dedicated line (an asynchronous operation), waits a few seconds for the fast case, and
  otherwise records the workspace as pending; the Mail panel's "Setting up your number" read
  completes the operation when Photon has finished, the way the Stripe provisioning workflows
  advance on read. No pool of idle lines and no sweeper. The number is a row in the existing
  agent-email address table (`kind: text`): owner, agent, status, business phone, taken-over
  threads, Photon resource and user ids. A workspace wipe releases the line.
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

The Photon project setup (Business plan, a `pho_sk_` project key, the webhook destination,
one paid line in dev against the starting credit, the pool size), the exact inbound event
schema (fetched from Photon's event-types endpoint before the parser is written), the Hermes
tool-allowlist key, cap values, adapter storage path, the Mail panel changes, the onboarding
step's look, rollout order, and the first per-platform adapter.

## Calls I made (override any of them)

- The number is an iMessage line by default, created on the spot. The SMS pool is the cheap
  fallback, not the default.
- The Front desk is an add-on at $200 a month (owner 2026-10-18), below the $250 a month the
  line costs on Photon, with Team unchanged at $50 (owner 2026-10-17, "keep the rates for now").
  WhatsApp is a second $250 line and a connected Meta
  business account; voice is $0.01 a minute. Revisit when there is volume pricing.
- Getting the number starts the paid plan: Team plus the add-on, charged then, no trial
  carried over. Sign-up and the website step stay free; the agent can be tried in chat
  before paying.
- Payment is a Stripe payment link through the business's own Stripe connection. In-chat Pay
  buttons come with branded Messages for Business later.
- The front-desk agent is Customer Operations rewritten and renamed, not a seventh agent.
- The autoreply with the business's listed phone is the floor whenever the workspace cannot
  run.
