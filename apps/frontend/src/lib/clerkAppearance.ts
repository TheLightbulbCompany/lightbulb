import type { ComponentProps } from "react";

import type { ClerkProvider } from "@clerk/nextjs";

/**
 * The neutral palette (globals.css `:root[data-chat-theme]`), applied to Clerk's forms.
 *
 * ONE appearance for every Clerk surface (SignIn, SignUp, UserButton,
 * OrganizationSwitcher), passed on <ClerkProvider> in app/layout.tsx, so the
 * form matches the page shell around it instead of shipping Clerk's defaults.
 *
 * It holds no colour literal: `variables` use `var()` tokens, `elements` use
 * Tailwind utilities. Pages must stamp data-chat-theme (components/ThemeStamp)
 * or the tokens are undefined. `__tests__/clerkAppearance.test.ts` guards that.
 *
 * @clerk/types is a transitive dependency under pnpm and does not resolve from
 * this package, and @clerk/nextjs does not re-export `Appearance` — so take the
 * type from the prop that consumes it. Same type, no new dependency.
 */
type ClerkProviderProps = ComponentProps<typeof ClerkProvider>;
type Appearance = NonNullable<ClerkProviderProps["appearance"]>;
type Localization = NonNullable<ClerkProviderProps["localization"]>;
type TaskUrls = NonNullable<ClerkProviderProps["taskUrls"]>;

export const clerkTaskUrls: TaskUrls = {
  "choose-organization": "/session-tasks/choose-organization",
};

export const clerkLocalization: Localization = {
  formFieldLabel__organizationName: "Workspace name",
  formFieldInputPlaceholder__organizationName: "Workspace name",
  createOrganization: {
    formButtonSubmit: "Create workspace",
    title: "Create workspace",
  },
  organizationList: {
    action__createOrganization: "Create workspace",
    createOrganization: "Create workspace",
    titleWithoutPersonal: "Choose a workspace",
  },
  taskChooseOrganization: {
    chooseOrganization: {
      action__createOrganization: "Create a workspace",
      subtitle: "Join an invited workspace or create a new one",
      subtitle__createOrganizationDisabled: "Join an invited workspace",
      title: "Choose a workspace",
    },
    createOrganization: {
      formFieldInputPlaceholder__name: "My workspace",
      formFieldInputPlaceholder__slug: "my-workspace",
      subtitle: "Enter your workspace details to continue",
      title: "Create a workspace",
    },
    organizationCreationDisabled: {
      subtitle: "Ask a workspace admin to invite you.",
      title: "You must belong to a workspace",
    },
  },
};

export const clerkAppearance: Appearance = {
  // Clerk resolves `var()` in variables, so the card follows the neutral
  // ladder (and its dark variant) that app/globals.css stamps on <html>.
  variables: {
    colorPrimary: "var(--fg)",
    // Clerk's default is white, which vanishes on the light primary of the dark ladder.
    colorTextOnPrimaryBackground: "var(--plane-pane)",
    colorBackground: "var(--plane-pane)",
    colorText: "var(--fg)",
    colorTextSecondary: "var(--fg-mute)",
    colorInputBackground: "var(--plane-sidebar)",
    colorInputText: "var(--fg)",
    colorDanger: "var(--alert)",
    colorSuccess: "var(--live)",
    colorWarning: "var(--alert)",
    borderRadius: "8px", // --radius
    fontFamily: "var(--font-sans), 'Source Sans 3', system-ui, sans-serif",
    fontFamilyButtons: "var(--font-sans), 'Source Sans 3', system-ui, sans-serif",
  },
  // What `variables` cannot reach. Tailwind utilities, so these follow the
  // tokens rather than a second copy of them.
  elements: {
    card: "shadow-none border border-edge bg-cream",
    formButtonPrimary: "bg-midnight text-cream hover:bg-midnight-soft",
    footerActionLink: "text-midnight underline",
    formFieldInput: "bg-cream-deep border-edge text-midnight",
    // Apple's and GitHub's marks are solid black: invert them on the dark ladder.
    providerIcon__apple: "[[data-chat-theme=dark]_&]:invert",
    providerIcon__github: "[[data-chat-theme=dark]_&]:invert",
    headerTitle: "font-serif", // §05: Lora for the one heading the user reads
    socialButtonsBlockButton: "border-edge bg-cream text-midnight hover:bg-cream-deep",
  },
};
