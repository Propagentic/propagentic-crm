---
name: cold-call-prep
description: Pre-cold-call sales due diligence for the Propagentic CRM. Invoked ONLY via the explicit slash command `/cold-call-prep <lead identifier>`. Pulls everything the CRM knows about a lead (contact record, prior emails, prior calls) from the propagentic-crm Firestore project via the Firebase MCP, combines it with live web research via the firecrawl skill (company website, local news, business filings, landlord-forum mentions), and produces a 30-second sales briefing with a suggested opener and three discovery questions. Appends the briefing to the lead's notes field in Firestore so the rest of the team sees it. Do NOT auto-trigger on related-sounding requests like "tell me about Newman" or "what do we know about this lead" — those are general lookups with different intents. This skill requires the explicit slash command.
---

# Cold Call Prep — Propagentic Sales DD Skill

You generate a pre-cold-call briefing for a sales rep, combining Propagentic's CRM data with live web research. Total target: under 30 seconds of work, under $0.10 of cost, briefing of ~250 words that the rep can scan before dialing.

## Inputs

The slash command is invoked as `/cold-call-prep <lead identifier>`. The identifier may be any of:

- Full owner name — `Robert Newman`
- Partial name — `Newman`
- Company name — `Ardmore Realty`
- Email — `newman@ardmorerealty.com`
- Phone number — `(336) 555-0123`
- Firestore lead doc ID — `lead_abc123`

If no identifier is provided, ask the user "Which lead?" and stop.

## Required environment

- Firebase MCP connected to project `propagentic-crm`
- `firecrawl-search` and `firecrawl-scrape` skills available
- Git working directory has a `user.name` configured (used to attribute the briefing in CRM notes)

If any of those are missing, tell the user what's missing and stop. Don't try to work around an unavailable MCP or skill.

---

## Step 1 — Identify the lead in the CRM

1. Use `mcp__firebase__firestore_list_collections` against project `propagentic-crm` to confirm which top-level collection holds leads. It will be one of `leads`, `contacts`, or `propagentic-leads`. Use whichever exists. (Jackson's v2.0 import script loads into one of these.)
2. Use `mcp__firebase__firestore_query_collection` with a `where` filter on the most likely field given the input shape:
   - Looks like an email → filter `email == <input>`
   - Looks like a phone → filter `phone == <normalized E.164>`
   - Looks like a Firestore ID → fetch the doc directly with `mcp__firebase__firestore_get_document`
   - Otherwise → filter `owner_name == <input>` first, then fall back to `company_name == <input>`, then fall back to fetching the first 200 docs and doing a fuzzy in-memory match on owner_name and company_name (Levenshtein distance ≤ 3).
3. Resolve to one of three states:
   - **One match** → proceed to Step 2.
   - **Zero matches** → respond `"No lead found matching '<input>' in the CRM. Closest fuzzy matches: <list 2–3 nearest names with their cities>."` and stop.
   - **Multiple matches** → respond with a numbered list (owner name + city + parcel count for each) and ask the user to pick one. Stop. When they pick, the user will re-invoke with a more specific identifier.

### Do-not-contact gate

If the matched lead has any of these fields set, **stop immediately** and tell the user instead of generating a briefing:

- `status == 'do_not_contact'`
- `dnc == true`
- `crm_dnc_reason` is set (UI field for DNC reason)
- `crm_notes` contains "DNC" or "do not contact" as a standalone phrase

The briefing is a sales tool. If the team marked someone DNC, we respect that — don't burn the relationship by surfacing a fresh pitch angle.

---

## Step 2 — Gather CRM data for the lead

1. **Lead doc**: Read every field from the matched doc. The schema varies because v3-propagentic-explorer supports custom column mapping per import, so don't assume specific field names. Pull whatever is there: owner_name, company_name, mailing_address, property_addresses, parcel_count, total_value, ICP bucket, status, market, phone, email, notes, prior briefings, last_touched_at.

2. **Prior emails**: Query the `emailActivity` collection (created by the email-tracking system spec'd in `docs/CRM_EMAIL_TRACKING_PRD.md`) for `contactIds array-contains <leadId>`. Take the most recent 10. May not exist yet in v1 of the CRM — if the collection doesn't exist, note "No email history available yet" and continue.

3. **Prior calls**: Query the `callActivity` collection for the same. May not exist yet. Same handling.

4. **Property details**: If the lead has a `parcel_ids` or `property_addresses` array, fetch up to 5 properties for context (helps you write a specific opener about their portfolio).

Total Firestore reads should stay under 30. If the lead has 100+ properties, sample 5; don't pull them all.

---

## Step 3 — Live web research via firecrawl

Build a search plan based on what's in the lead doc. Run firecrawl searches in this priority order, stopping when you've spent 5 firecrawl calls or 15 seconds:

1. **`<company_name> <city> property management`** — find their website + Google reviews + Yelp.
2. **`<owner_name> <state> LLC` site:sosbiz.com OR site:opencorporates.com** — state business registry, sister LLCs they own.
3. **`<owner_name OR company_name> biggerpockets`** — landlord-forum mentions, ratings, complaints.
4. **`<company_name> news <last 12 months>`** — recent press, expansion, acquisitions, lawsuits.
5. **If a company website surfaced in #1**: `firecrawl-scrape` their About / Team / Contact / Properties page to extract company size, locations, brand.

Skip any step that isn't applicable (e.g., individual owners with no company name skip step 1).

If firecrawl returns nothing useful, that's a valid result — note it and move on. Don't speculate to fill the gap.

---

## Step 4 — Synthesize the briefing

Produce **exactly** this markdown structure. Skip any section that has nothing real to put in it (don't pad).

```markdown
# Pre-Call Briefing: <Owner Name>
*Generated <YYYY-MM-DD HH:MM ET> · CRM + live web · ~<actual cost>*

## Snapshot
<Two sentences. Who they are, scale, category. Concrete. Specific.>

## What they own
- **Property count:** <n>
- **Markets:** <comma-separated cities/counties>
- **Total appraised value:** <$X, if available>
- **ICP bucket:** <ICP-1 / ICP-2 / ICP-3 candidate / ICP-4 candidate>
- **Management:** <Self-managed | PM company | Mixed | Unknown>

## Prior touches
<Bulleted, most recent first. Format: `<date> · <channel> · <who on our team> · <one-line summary>`. If none, say "No prior contact — this is a true cold first touch.">

## What's new (live web)
<Bullets with source URLs. Recent news, business filings, mentions worth referencing. Skip section entirely if nothing meaningful.>

## The opener (suggested)
<ONE sentence pulling on a specific real detail from the above. Don't say "I noticed you have 14 properties" — say "I noticed you picked up three more units in Pikesville this spring — congrats on the expansion." Specific, true, conversational.>

## Three discovery questions
1. <Question that uncovers maintenance pain — phrased to invite specifics>
2. <Question that qualifies ICP further — self-managed, PM, growth trajectory>
3. <Question that opens the next-step / commitment — "would 15 min Wednesday work?">

## Red flags / careful with
<Skip section if nothing. Otherwise: bad reviews, recent legal trouble, competitor relationship, prior negative interaction with our team.>
```

### Quality bar

- Every claim must trace to either the CRM or a specific source URL. No "I think" or "it looks like."
- The opener must reference a verifiable detail. If you can't find one, write "<no warm hook surfaced — use the standard opener from `docs/SALES_ONBOARDING_SUMMER_2026.html`>" — don't invent.
- Briefing length: 200–300 words. Longer is worse. The rep is reading this in the 30 seconds before they dial.

---

## Step 5 — Write the briefing back to the CRM

Append the full briefing to the lead's **`crm_notes`** field — the CRM UI (`propagentic-crm/docs/index.html`) reads from `crm_notes`, not `notes`. Also set `crm_last_touched` to a server timestamp and `crm_touched_by` to the operator's email so the lead's touch metadata stays accurate. Use `mcp__firebase__firestore_update_document`.

**Project routing**: `firestore_query_collection` honors the *active* Firebase project, not an explicit project path. Before any read or write against propagentic-crm, call `mcp__firebase__firebase_update_environment({ active_project: "propagentic-crm" })`. Skipping this will silently hit `propagentic-prod-2` and return zero results.

Prefix the appended block exactly like this:

```
--- Pre-Call Briefing | <YYYY-MM-DD HH:MM ET> | by <git user.name> ---
<the full briefing as generated above>
---
```

If `crm_notes` is currently empty/null, write the briefing as the new value. If it has prior content, prepend the new briefing so most recent is on top.

This means: when Brian opens Newman's lead drawer tomorrow, he sees Ben's briefing from today at the top of the notes, stamped with timestamp and author.

---

## Step 6 — Output to the user

Print the **full** briefing to stdout (don't summarize it). Then print one short footer line:

```
Briefing written to CRM notes on lead <leadId>. Open in the CRM: https://propagentic-crm.web.app/?lead=<leadId>
```

---

## Privacy & safety rules

- **No personal life info.** Even if firecrawl surfaces it (family, medical, religion, dating, kids), drop it. Sales briefings stay focused on the business.
- **No invented detail.** Every claim sources to CRM data or a specific URL. If you can't source it, drop the claim — don't soften it with "likely" or "appears to."
- **No conflict-of-interest data.** If the lead's own company website lists a competitor product they already use, mention it factually in the "Red flags" section. Don't strategize against it in the body.
- **Respect DNC flags absolutely.** Step 1's DNC check is a hard stop — never bypass it.

---

## Budget

| Resource | Target | Hard cap |
|---|---|---|
| End-to-end latency | < 30 sec | 60 sec |
| Claude API calls | < 3 | 5 |
| Firecrawl calls | < 5 | 8 |
| Firestore reads | < 30 | 50 |
| Cost per briefing | < $0.10 | $0.25 |

If trending over budget mid-execution, cut depth in this order: web research depth → email history depth → property sample size. Never cut the contact-match step or the DNC check.

---

## Edge cases & how to handle them

| Situation | Behavior |
|---|---|
| Lead identifier ambiguous (multiple matches) | List them, ask the user to pick. Stop. |
| No lead found | Suggest 2–3 nearest fuzzy matches. Stop. Don't fabricate a briefing for someone who doesn't exist. |
| Lead is flagged DNC | Hard stop with explanation. No briefing. |
| No prior emails / calls | Write `"No prior contact — true cold first touch."` |
| Firecrawl returns nothing useful | Skip "What's new" section silently. Don't say "no info found" — the absence is the absence. |
| Lead has no associated company (individual owner, no LLC) | Skip steps 3.1, 3.2, 3.5. Search owner name + state + "rental property" instead. |
| CRM project unreachable | Tell user "Cannot reach propagentic-crm Firestore. Check Firebase MCP connection." Stop. |
| Firecrawl skill unavailable | Generate a CRM-only briefing and label it `(CRM-only — web research skipped)`. |
| Same lead briefed within the last 24 hours | Show the prior briefing first, then ask "Generate a fresh one? (recent web changes may exist)" — don't double-up on notes. |

---

## When NOT to trigger this skill

This skill is invoked **only** via the explicit slash command `/cold-call-prep <name>`. Do not auto-trigger on:

- "Tell me about Newman"
- "What do we know about this lead?"
- "Brief me on Ardmore Realty"

Those are general lookups or conversation context — the user may not be about to dial, and triggering a full DD + web search + CRM write would be overkill and noisy. Require the explicit command.

---

## Future hooks (do not implement yet)

These are out of scope for v1 but worth marking so a future maintainer knows the design intent:

- **Auto-trigger from the CRM UI** — eventually, "click a lead → click Brief Me → skill fires server-side via a Cloud Function." Until then, slash command only.
- **Briefing freshness signal** — if a briefing in notes is older than 7 days, mark it stale in the UI.
- **Briefing quality feedback** — after a call, the rep marks the briefing as "useful / not useful" to feed back into prompt iteration.
- **Team-level briefing index** — a `/briefings` page in the CRM showing every briefing across all leads, sortable by author, lead, date.

These are documented here so the skill's contract stays stable even as the surrounding system grows.

---

*v1 · 2026-05-20 · Owner: Ben*
