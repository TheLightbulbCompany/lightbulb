# Skills

<!--
  MAINTENANCE (Lightbulb engineers): anchored to code.
  - The three dirs ..... apps/backend/core/services/hermes_profiles.py
                         (SKILLS_DIR_LIGHTBULB, SKILLS_DIR_HERMES_BUNDLED, SKILLS_EXTERNAL_DIRS,
                          and build_profile_config's `skills` block: write_approval + external_dirs)
  - Ambiguity rule ..... the image's /opt/hermes/tools/skills_tool.py (_find_all_skills advertises
                         a duplicated name, _locate_skill/skill_view then refuse it) — quoted in
                         SKILLS_DIR_LIGHTBULB's docstring
  - Baked tree ......... apps/infra/hermes/skills-src, built by
                         apps/infra/hermes/build-skills.sh into the image at /opt/hermes-skills
  - Re-apply ops door .. apps/backend/scripts/reapply_hermes_skills_dirs.py
  - Owner UI ........... apps/frontend/src/components/control/panels/SkillsTab.tsx
  - The Paperclip gap .. core/services/paperclip_adapter_config.py (HERMES_GATEWAY_TYPE);
                         paperclip fork server/src/routes/agents.ts (adapter.listSkills exists for
                         claude/codex/cursor/gemini/grok/opencode/pi — not the gateway adapters)
-->

## Skills are folders on disk here

A skill is a folder with a `SKILL.md`. Nothing fetches one at answer time — the
`skills` toolset reads what is already on this machine, from three places:

| Source | Where | Who writes it |
|---|---|---|
| **Lightbulb baked** | `/opt/hermes-skills/` (`lightbulb`, `paperclip`) | baked into the image — you cannot change these |
| **Hermes bundled** | `/opt/hermes/skills/` | shipped with the runtime, read-only |
| **your profile** | your own profile's `skills/` directory | yours — this is the one you can write |

**A name that exists in two of those dirs cannot be loaded at all.** The index
still advertises it, and the load then refuses with `Ambiguous skill name`.
Reach such a skill by its categorised path (e.g. `productivity/pdf`) instead,
and prefer a distinct name when you author one: several Hermes-bundled names
are common (`pdf`, `docx`, `xlsx`, `github`, `notion`, `systematic-debugging`).

Every loaded skill costs prompt space. Prefer a focused subset over everything
a repository contains.

## DO NOT use Paperclip's company-skills library for an agent here

`/opt/hermes-skills/paperclip/references/company-skills.md` documents a company
skill library with `skills install` / `skills agent sync`. **That path does not
work on Lightbulb.** Paperclip syncs skills into a harness directory for
adapters that implement it — claude, codex, cursor, gemini, grok, opencode, pi.
Every Lightbulb agent uses the `hermes_gateway` adapter, which does not, so the
sync endpoint answers `supported: false` with
`"This adapter does not implement skill sync yet."`

That is not a bug to report or retry, and it is not a reason to tell the owner
their request is impossible. It means you were on the wrong plane: Paperclip's
library exists to push skills into OTHER harnesses. Yours are already on the
machine you run on.

## Telling the owner

The owner's own view is **Settings → Skills** (thelightbulb.company/settings):
it lists what is installed and lets them enable or disable one. It shows no
per-skill requirements, because Hermes has no dependency manifest — a skill is a
`SKILL.md`, and whatever it shells out to is the image's business, so nothing is
ever "missing" there.

## Skills you author yourself

- The owner browses, enables, and installs skills in the **Skills** tab
  (Settings → Skills). You cannot install skills for them.
- You CAN create and improve skills yourself via the `skills` toolset
  (list/view/manage): when the owner asks you to make a skill, or teaches you
  a durable workflow, propose it there — never write SKILL.md files directly.
- A skill you create or improve there is saved to your own profile and used
  from your next turn on. There is no separate review queue or proposals tab.
