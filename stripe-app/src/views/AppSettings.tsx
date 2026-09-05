import type { ExtensionContextValue } from "@stripe/ui-extension-sdk/context";
import { Box, Button, Icon, Inline, List, ListItem, SettingsView } from "@stripe/ui-extension-sdk/ui";

// ponytail: static instructions only — the key itself is minted and shown by
// Stripe ("View API keys" on this page); the app never sees or stores it.
// Test-mode and sandbox installs mint rk_test_ keys, which only the dev stack
// (a Sandbox platform key) accepts; production refuses them by design.
const siteFor = (mode: string) => (mode === "live" ? "https://flickmeon.ai" : "https://dev.flickmeon.ai");

const AppSettings = ({ environment }: ExtensionContextValue) => (
  <SettingsView>
    <Box css={{ padding: "large", borderRadius: "medium", width: "fit" }}>
      <Box css={{ font: "heading", stack: "x", alignY: "center", gap: "small", marginBottom: "small" }}>
        <Icon name="sparkle" />
        Connect this Stripe account to Lightbulb
      </Box>
      <Box css={{ marginBottom: "medium" }}>
        Lightbulb reads your charges, customers, products, prices and balance so your
        company&apos;s Money tabs and your agents&apos; briefings reflect real revenue.
        It never moves money and holds read-only access.
      </Box>
      <List>
        <ListItem
          title="1. Generate the key"
          value={<Inline>Click <Inline css={{ font: "bodyEmphasized" }}>View API keys</Inline> above</Inline>}
        />
        <ListItem
          title="2. Copy it"
          value="The restricted key starts with rk_live_ (or rk_test_ in a sandbox)"
        />
        <ListItem
          title="3. Paste it into Lightbulb"
          value="Settings → Connections → Stripe → Add API key"
        />
      </List>
      <Box css={{ stack: "x", gap: "small", marginTop: "medium" }}>
        <Button type="primary" target="_blank" href={`${siteFor(environment.mode)}/settings/connections`}>
          Open Lightbulb Connections
          <Icon name="external" />
        </Button>
        <Button target="_blank" href="https://flickmeon.ai">
          About Lightbulb
          <Icon name="external" />
        </Button>
      </Box>
    </Box>
  </SettingsView>
);

export default AppSettings;
