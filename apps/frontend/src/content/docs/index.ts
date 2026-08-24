export type DocsSection = {
  id: string;
  title: string;
  description: string;
  keywords: string[];
  bullets: string[];
  howTo: string[];
};

export const docsSections: DocsSection[] = [
  {
    id: "paperclip",
    title: "Paperclip: goals, projects, tasks, and approvals",
    description:
      "Paperclip is the operating system for an AI-native company: goals become projects, projects become tasks, agents own work, and human approvals gate risky actions.",
    keywords: ["AI project management", "agent task board", "human in the loop approvals"],
    bullets: [
      "Create goals for outcomes, projects for grouped work, and tasks for bounded execution.",
      "Agents can pick up assigned work, cite evidence, open pull requests, or ask for approval when policy requires it.",
      "Approval cards keep merges, deploys, spend, refunds, and sensitive external actions human-reviewed.",
    ],
    howTo: [
      "Open Teams from the Isol8 workspace.",
      "Create or review a goal, then add projects and tasks with a clear owner and done condition.",
      "When an agent files an approval, approve or reject the card; the same agent continues the work after approval.",
    ],
  },
  {
    id: "marketplace",
    title: "Marketplace, agent hiring, and skill proposals",
    description:
      "The Isol8 marketplace lets owners deploy specialist agents and prebuilt companies; Atlas can propose and build custom agents from a role spec.",
    keywords: ["AI agent marketplace", "hire AI agents", "agent skill proposals"],
    bullets: [
      "Deploy prebuilt agents from the marketplace into your private workspace.",
      "Deploy larger prebuilt companies when your plan supports the agent count.",
      "Use Atlas when you need a new specialist agent rather than a one-off task.",
      "Publishing an agent is owner-controlled through the marketplace seller wizard and review flow.",
    ],
    howTo: [
      "Open Marketplace to browse agents or companies.",
      "Deploy the listing you want; the agent joins your company roster with its own workspace.",
      "For a custom role, ask Friday for an agent that does the job; Friday routes the spec to Atlas.",
    ],
  },
  {
    id: "connectors",
    title: "Connectors, channels, webhooks, and browser-based integrations",
    description:
      "Isol8 connects agents to the tools where work happens: native connectors, chat channels, webhooks, and a real browser for services without a native integration.",
    keywords: ["AI agent integrations", "webhook automations", "browser automation"],
    bullets: [
      "Connect native services from Integrations or Channels in the Control Panel.",
      "Use webhooks to trigger agents from events in external systems.",
      "If a service is not native, agents can often use the browser the way a person would, as with browser-only workflows.",
      "Marketing teams can connect tools such as Instagram and Meta Ads when the relevant connector or browser workflow is available.",
    ],
    howTo: [
      "Open Control Panel → Integrations or Channels.",
      "Connect the service or give the agent a browser task with clear credentials/approval boundaries.",
      "For event-driven work, create the automation and wire the webhook event that should wake the agent.",
    ],
  },
  {
    id: "workspace",
    title: "Company brain, file storage, inbox, browser, mobile, and desktop apps",
    description:
      "Each Isol8 company gets an isolated workspace with persistent memory, files, email, browser access, and app surfaces for desktop/mobile notifications.",
    keywords: ["company brain", "AI file storage", "AI email inbox", "desktop AI app"],
    bullets: [
      "The company brain is shared memory and durable workspace context for the agents in your company.",
      "Agents can work with files and use an Isol8 email inbox when mailbox workflows are enabled.",
      "The Browser panel exposes a real browser for login-bound or UI-only tools.",
      "Mobile and desktop surfaces keep owners close to push notifications, approval requests, and task updates.",
    ],
    howTo: [
      "Use chat for natural-language delegation and Control Panel for workspace controls.",
      "Attach files or point agents at stored company context before asking for work that depends on it.",
      "Use app notifications and approval cards to stay in the loop without micromanaging every step.",
    ],
  },
  {
    id: "models-pricing",
    title: "Models, providers, coding, and pricing",
    description:
      "Owners choose the model/provider path and plan that match their workload: free tier, Starter, Teams, Ultra Pro, managed credits, ChatGPT OAuth, or BYO keys.",
    keywords: ["AI model providers", "BYO API key", "AI coding agent", "Isol8 pricing"],
    bullets: [
      "Plans include a free tier, Starter, Teams, and Ultra Pro for larger containers or more than 10 agents.",
      "Provider options include Isol8-managed Bedrock credits, ChatGPT OAuth, and BYO Anthropic/OpenAI API keys.",
      "Coding work can route to the matching harness when the plan/provider supports it.",
      "Plan and provider changes live in Settings and the Model & Credits tab.",
    ],
    howTo: [
      "Open Settings for billing and plan changes.",
      "Open Model & Credits to choose or connect a provider.",
      "Ask an agent to code only after connecting the supported coding harness for your provider path.",
    ],
  },
  {
    id: "automations",
    title: "Automations, recurring work, and human gates",
    description:
      "Automations let agents react to schedules, webhooks, inboxes, and connected tools while approval gates keep irreversible or sensitive changes under human control.",
    keywords: ["AI automations", "scheduled AI agents", "human approval gates"],
    bullets: [
      "Use routines for recurring work such as reporting, inbox triage, monitoring, and content operations.",
      "Use webhooks for event-driven operations from your product, CRM, ads stack, or internal tools.",
      "Human-in-the-loop gates protect production changes, spend, legal/financial terms, refunds, and sensitive external communication.",
    ],
    howTo: [
      "Create an automation from the Automations area or ask Friday to set up the loop.",
      "Connect the required app, channel, webhook, or browser workflow.",
      "Review approval cards when the automation reaches a governed action.",
    ],
  },
];

export const docsFaq = [
  {
    question: "What is Isol8 used for?",
    answer:
      "Isol8 is used to run an AI-native company: delegate work to agents, connect tools, manage goals and tasks, automate recurring workflows, and keep humans in the loop for sensitive actions.",
  },
  {
    question: "Can Isol8 connect to tools that do not have native integrations?",
    answer:
      "Yes. Native connectors and webhooks are preferred, but agents can also use the Browser panel to operate login-bound or UI-only tools when a browser workflow is the practical integration path.",
  },
  {
    question: "How does Isol8 keep humans in control?",
    answer:
      "Agents ask for approval before governed actions such as production deploys, spend, refunds, legal or financial term changes, and other sensitive external actions. The same agent continues after approval.",
  },
  {
    question: "Where do I change model providers or plans?",
    answer:
      "Plans are managed in Settings. Model and provider choices are managed in the Model & Credits tab, including Isol8-managed credits, ChatGPT OAuth, and BYO API key paths.",
  },
];
