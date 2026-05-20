# Team skills for propagentic-crm

This directory ships Claude Code skills that work against this CRM. Once you clone this repo and run `claude` inside it, every skill under `.claude/skills/*` is available as a slash command.

## Available skills

- **`/cold-call-prep <lead>`** — Generates a 250-word pre-cold-call briefing for a lead (CRM data + live web research), appends it to the lead's `crm_notes` field, and prints it to your terminal. Identifier can be owner name, company, email, phone, or Firestore lead ID. See `skills/cold-call-prep/SKILL.md` for the full contract.

## One-time setup per teammate

To use the skills, each teammate needs:

### 1. Claude Code
Install: https://docs.claude.com/claude-code — log in with your Anthropic / `@propagenticai.com` account.

### 2. Firebase MCP (for CRM reads/writes)
The Firebase MCP is bundled with Claude Code. You need:

- Run `firebase login` once with your `@propagenticai.com` Google account
- Ask Ben to grant you the `Firebase Develop Admin` role on the `propagentic-crm` project (one-time IAM grant)
- After login, confirm you see `propagentic-crm` in `firebase projects:list`

The skill auto-switches the active project to `propagentic-crm` before any read/write — you don't need to set it manually.

### 3. Firecrawl CLI (for live web research)
```
brew install firecrawl-mcp
firecrawl auth login
```
Or get an API key from Ben and set `FIRECRAWL_API_KEY` in your shell. The skill will degrade to "CRM-only briefing" if Firecrawl isn't installed — usable, but less rich.

### 4. Git identity
```
git config --global user.name "Your Name"
git config --global user.email "you@propagenticai.com"
```
The skill stamps every briefing with `git config user.name` so your teammates can see who wrote each one.

## Daily use

```
cd ~/work/propagentic-crm        # any directory inside the repo
claude                            # starts Claude Code session
> /cold-call-prep Robert Newman   # generates briefing, writes to CRM, prints it
```

When Brian opens Newman's lead drawer in the CRM right after, the briefing appears in the Notes section as a formatted card (with Edit/Preview toggle).

## Adding new skills

Drop a folder under `.claude/skills/<your-skill-name>/SKILL.md` (with YAML frontmatter for `name` / `description`), commit, push. Next teammate to pull gets the new slash command automatically.

## Cost & budget

Each briefing costs roughly $0.05–$0.10 in Anthropic API + Firecrawl spend. Targets are documented in the skill file. If you're hammering 50+ briefings/day, ping Ben — we may want a Cloud Function version that runs serverside on a CRM button click.
