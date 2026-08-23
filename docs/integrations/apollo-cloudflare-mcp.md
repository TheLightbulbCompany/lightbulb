# Apollo and Cloudflare MCP integrations

Isol8 exposes Apollo and Cloudflare through hosted remote MCP servers, using the
same `mcp_oauth` credential rail as Attio, Stripe, Notion, Linear, Sentry, and
Intercom. The provider registry is the source of truth for the catalog and the
runtime materializer.

## Apollo

- Provider key: `apollo`
- Remote MCP URL: `https://mcp.apollo.io/mcp`
- OAuth metadata observed 2026-08-23:
  - Authorization endpoint: `https://mcp.apollo.io/mcp/oauth_metadata/redirect_to_authorize`
  - Token endpoint: `https://mcp.apollo.io/api/v1/oauth/token`
  - Dynamic client registration endpoint: `https://mcp.apollo.io/api/v1/oauth/applications/register_oauth_client`
- Requested scopes are deliberately read/prospecting oriented: profile/app scope
  discovery, people/company search, contact read/search, and enrichment. Do not
  add send, campaign approval, sequence enrollment, or other write/send scopes
  unless a separate product/security review explicitly asks for them.

Apollo MCP was chosen over a CLI/API wrapper because Apollo publishes a hosted
MCP server for search/prospecting/enrichment/contact workflows and Isol8 already
has native hosted-MCP connection, tool discovery, and mcporter vault seeding.

Safe verification: connect a dev/test Apollo account, confirm the MCP server is
published under `mcp.servers.apollo`, and perform only tool discovery or safe
read/list/search probes. Never send outreach, enroll contacts in sequences, or
mutate real leads during verification.

## Cloudflare

- Provider key: `cloudflare`
- Remote MCP URL: `https://mcp.cloudflare.com/mcp`
- OAuth metadata observed 2026-08-23:
  - Authorization endpoint: `https://mcp.cloudflare.com/authorize`
  - Token endpoint: `https://mcp.cloudflare.com/token`
  - Dynamic client registration endpoint: `https://mcp.cloudflare.com/register`

Cloudflare's hosted MCP was chosen over local npm/third-party wrappers because it
is Cloudflare-owned, OAuth-protected, and fits Isol8's existing hosted-MCP rail.

Safe verification: connect only a dev/test Cloudflare account or a token/account
with read-oriented permissions. Limit smoke tests to discovery/account/zone read
calls. Do not mutate zones, DNS, Workers, routes, accounts, billing/spend, or
production settings.
