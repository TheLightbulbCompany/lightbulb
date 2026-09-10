import type { ComponentProps } from "react";

import type { ClerkProvider } from "@clerk/nextjs";

/**
 * Brand Identity Manual v1.0 §05/§06, applied to Clerk's hosted form.
 *
 * ONE appearance for every Clerk surface (SignIn, SignUp, UserButton,
 * OrganizationSwitcher), passed on <ClerkProvider> in app/layout.tsx, so the
 * form matches the page shell around it instead of shipping Clerk's defaults.
 *
 * This is the ONLY file outside the token layer (globals.css / landing.css)
 * allowed to hold a colour literal, and it is on the brand-token scanner's skip
 * list for that reason: Clerk's `variables` are read by its own runtime and
 * cannot resolve CSS custom properties. Keep it the only such file — anything
 * Tailwind can reach belongs in `elements` as a utility class, below.
 * `__tests__/clerkAppearance.test.ts` is the scanner's stand-in here.
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
  variables: {
    colorPrimary: "#0F1A2B", // --midnight
    colorBackground: "#F5EFE2", // --cream
    colorText: "#0F1A2B", // --midnight
    // --midnight-muted: color-mix(in srgb, #0F1A2B 64%, #F5EFE2)
    colorTextSecondary: "#62676D",
    colorInputBackground: "#EFE8D9", // --cream-deep
    colorInputText: "#0F1A2B", // --midnight
    colorDanger: "#96382B", // --err
    colorSuccess: "#3A6B45", // --ok
    colorWarning: "#96501A", // --warn
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
    headerTitle: "font-serif", // §05: Lora for the one heading the user reads
    socialButtonsBlockButton: "border-edge bg-cream text-midnight hover:bg-cream-deep",
  },
};
