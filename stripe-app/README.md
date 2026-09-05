# Lightbulb Stripe App

The guided front door for the company-Stripe **API-key path**: an owner installs
this app on their Stripe account, Stripe mints a restricted key carrying exactly
the six read permissions in `stripe-app.json`, and the owner pastes that key into
Lightbulb → Settings → Connections → Add API key. The app itself is one static
settings view; it never sees, stores or transmits the key.

`stripe_api_access_type` is **permanent after the first upload** — do not upload
without an explicit owner go. The permission list mirrors
`core/services/company_stripe.py::validate_api_key` (`Account.retrieve` +
`_READ_PROBES`); change both together.

```bash
cd stripe-app
pnpm install --ignore-workspace   # standalone: NOT a pnpm-workspace package
pnpm typecheck
stripe apps upload                # owner go required
```

`stripe apps grant list-permissions` in CLI 1.18.7 does not list
`connected_account_read` (docs: Core → Accounts); it is written into the manifest
by hand. The CLI needs a real TTY for interactive prompts.
