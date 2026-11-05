// Pre-filled scaffold for the Get Started "Create your first task" step. The
// user edits it before hitting Create — the point is a populated, CEO-directed
// first task instead of a blank composer. "[your goal]" is an obvious
// replace-me token left in the editable fields.
export const ONBOARDING_FIRST_TASK_TITLE = "Build out my team for [your goal]";

export const ONBOARDING_FIRST_TASK_DESCRIPTION = `You are my CEO. Help me stand up a team to deliver [your goal].

- Work out what roles and skills this goal needs.
- Break it into concrete tasks and start delegating.
- If a needed skill isn't on the team yet, bring it in.
- Report back with the plan and the first tasks you've kicked off.`;

// The first task goes to the starter root CEO (shared with mobile's one-voice roster).
export { resolveCeoAgentId } from "@lightbulb/shared";
