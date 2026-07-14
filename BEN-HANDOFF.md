# Ben handoff — hook the voice agent into the AI Call Center demo page

**Give this file to Claude Code in the `propagentic-voice-agent` repo.** It describes
exactly what to build, where the hook points are in `server.js`, and how to verify.
The goal: the demo website is finished (UI + data layer + schema); the ONLY remaining
work is a bridge that connects the voice agent to a Firestore contract. No UI work,
no design work — those files are done and should not be modified.

---

## 1. Context — what exists and why

Jackson built a client-facing demo page for a property-management firm:

- **`docs/call-center.html`** (in the `propagentic-crm` repo, hosted on that project's
  Firebase Hosting) — a standalone page, behind the CRM's Google login. It shows a
  live call command center (assign a call → watch the transcript stream in, with a
  progress checklist, captured-detail chips, call brief) and a call-history view
  (table → per-call drawer with transcript + structured outcome).
- It has **two data-source modes** (⚙︎ settings → "Data source"):
  1. **Direct API mode** — talks straight to this voice server's ops endpoints
     (`/api/ops/dial`, `/api/ops/state`, `/events` SSE, `/call/:sid/whisper`) through
     a tunnel. This works today but is fragile for demos (quick-tunnel URLs rotate,
     CORS, shared token).
  2. **Firestore backbone mode** ← **the target.** The page renders purely from
     documents in a `voiceCalls` collection and writes only call *requests*,
     *guidance* notes, and *hang-up* flags. No tunnel, no token, no CORS, realtime
     via `onSnapshot`.

**Your job = make the voice agent read and write `voiceCalls` documents.** This is
the same architecture as your existing `voice-worker.js` (Firestore listener → place
call → write outcome back), pointed at a different project and a cleaner contract.

## 2. The contract (read these two files first)

- **`voice-call.schema.json`** (propagentic-crm repo root) — the full JSON Schema of a
  `voiceCalls` document. Machine-readable; treat it as the source of truth.
- **`docs/call-data.js`** — the UI's client for the same contract (read the header
  comment). Includes `fastBuckets()`, the deterministic half of the bucketing system.

Summary:

**Project:** `propagentic-crm` (Firebase). **Collection:** `voiceCalls`.

**Lifecycle** (`status` field): `requested` → `dialing` → `in_progress` →
`completed` | `voicemail` | `no_answer` | `failed`.

**Field ownership:**

| Writer | Fields |
|---|---|
| UI (already built) | doc creation (`status:'requested'`, `toPhone`, `brief`, `instructions`, `createdAt*`, `requestedBy`), `guidance[]` (arrayUnion), `hangupRequested` |
| **Bridge (you)** | `status` transitions, `agent{name,callSid,version}`, `transcript[]` (arrayUnion), `startedAt`, `endedAt`, `outcome` (the buckets), `error`, marking `guidance[].consumed` |

**Transcript turn shape** — identical to this server's in-memory transcript entries,
so the hook is a passthrough: `{ role: 'assistant'|'user', text: string, ts: ISO string }`.
⚠️ Gotcha: keep `ts` as ISO **strings** — Firestore rejects `serverTimestamp()` inside
`arrayUnion` elements.

**`brief`** is the structured request from the UI sidebar:
`{ address, issue, trade, availability: [{day, ranges:[[startHr,endHrExcl]]}], availabilityText, language }`.
The UI also writes `instructions` (a composed prompt with an AI-honesty rule). You may
use `instructions` as the systemPrompt directly, or build your own production prompt
from `brief` — the brief is canonical; `instructions` is a convenience. **Keep the
honesty behavior**: if asked whether it's an AI, the caller answers honestly (this is
already your rule in `buildCallContext()` in voice-worker.js — reuse that posture).

## 3. What to build — `crm-bridge.js` in the voice-agent repo

One new module, opt-in via env (e.g. `CRM_BRIDGE=1`), started from the bottom of
`server.js` exactly like the `voice-worker` block. Deliberately additive — do not
restructure existing code.

### 3.1 Setup

1. Firebase Admin SDK against **propagentic-crm** (NOT prod-2). Create a service
   account key: Firebase console → propagentic-crm → Project settings → Service
   accounts → Generate new private key. Save as `.crm-bridge-sa.json` in the repo
   (gitignore it, like `.voice-worker-sa.json`).
2. `initializeApp` with a **named app** (`initializeApp(opts, 'crm')`) so it can
   coexist with voice-worker's prod-2 app in the same process.

### 3.2 Request watcher (mirror of voice-worker's ticket listener)

- `onSnapshot` on `voiceCalls` where `status == 'requested'`, ignore the first
  snapshot's backlog older than ~10 minutes (stale requests → mark `failed` with
  `error: 'expired before an agent picked it up'`).
- Claim atomically in a transaction (`status: 'requested'` → `'dialing'`, set
  `agent: {name:'hermes-voice-agent', callSid:null, version:...}`) so a restarted
  bridge can't double-dial — same pattern as `handleTicket()`.
- Respect the existing one-call-at-a-time invariant (`calls.size > 0` → leave the
  request as `requested`; retry when the current call ends).
- Place the call through the local API you already have:
  `POST http://localhost:PORT/call` with `{to, publicUrl, systemPrompt: doc.instructions (or your own from brief), sttLanguage: brief.language || undefined}`.
  Update the doc: `agent.callSid = <sid>`.
- Keep a `callSid → docId` map (like voice-worker's `callMeta`).

### 3.3 Live hooks in `server.js` (each is 1–3 lines calling into the bridge)

| Event | Where in server.js | Bridge action |
|---|---|---|
| Line connected | `wss` `'start'` case (after `state.ws = ws`) | `status:'in_progress'`, `startedAt: ISO now` |
| Agent spoke (opener) | same `'start'` case where the opening line is pushed to `state.transcript` | `transcript: arrayUnion({role:'assistant', text, ts})` |
| Caller turn | `processCallerSpeech()` right after `state.transcript.push({role:'user',…})` | arrayUnion the user turn |
| Agent turn | `processCallerSpeech()` after the `spokenText` push (use `spokenText`, not the raw LLM draft — it's what was actually said) | arrayUnion the assistant turn |
| Voicemail text | the voicemail branch push | arrayUnion + note for terminal status |
| Call ended | `saveCallJson()` (fires on stop + status callbacks; it's idempotent-ish — guard with a `bridged` set like `outcomeEnriched`) | terminal `status` (map: voicemail flag → `'voicemail'`; `userTurns===0` + finalStatus no-answer/busy/failed → `'no_answer'`/`'failed'`; else `'completed'`), `endedAt` |
| Guidance | doc `onSnapshot` while call live: new `guidance[]` entries with `consumed:false` | push `{note, ts}` into `state.whispers` (same shape the `/call/:sid/whisper` endpoint writes — `composePrompt()` already folds whispers into the next turn), then rewrite the array with `consumed:true` |
| Hang-up | same doc listener: `hangupRequested === true` | `twilioClient.calls(sid).update({status:'completed'})` (reuse the `/call/:sid/hangup` logic) |

Simplest wiring: export `bridge.onTranscriptTurn(callSid, turn)`,
`bridge.onCallStatus(callSid, status)` etc., and call them (guarded by
`if (crmBridge) …`) at those points — the exact pattern `voiceWorker.recordOutcome`
already uses at the bottom of `saveCallJson()`.

### 3.4 The bucketing pass (unstructured call → structured data)

After the terminal status is written, run TWO passes and merge into `outcome`:

1. **Fast pass (deterministic, no LLM)** — port of `fastBuckets()` from
   `docs/call-data.js` (regex money/times/names/trade/referral). Write it first so
   the UI shows *something* within a second of hang-up.
2. **LLM pass (judgment)** — one Haiku call, exactly like your `enrichOutcome()`,
   but emitting the full bucket schema. It **overwrites** the fast pass on conflict.
   Set `outcome.meta = {extractedBy:'fast+llm', model:'claude-haiku-4-5', extractedAt}`.

Use this prompt (transcript formatted `AGENT:` / `THEM:` like enrichOutcome):

```
Extract the outcome of this phone call. AGENT is an AI maintenance coordinator
calling a contractor on behalf of a property management office. Return ONLY valid
JSON, no markdown.

Transcript:
{{convo}}

Call brief (what the call was about):
Property: {{brief.address}} · Issue: {{brief.issue}} · Tenant availability: {{brief.availabilityText || 'flexible'}}

Return exactly this shape (null for anything not established on the call; booleans
only when the transcript supports them):
{
  "result": "scheduled" | "callback_requested" | "voicemail" | "no_answer" | "not_scheduled" | "needs_attention",
  "appointment": { "scheduled": bool — true ONLY for a visit BOTH sides confirmed, never a time merely discussed,
                   "day": "e.g. Thursday", "window": "e.g. 2-4 PM", "tentative": bool — held pending confirmation },
  "pricing": { "serviceFee": "trip/service-call fee as quoted, e.g. $95",
               "hourlyRate": "e.g. $85/hr after first hour",
               "quotes": ["any other price statements"],
               "termsAccepted": bool|null — did they accept invoice/net-5/no-card terms },
  "contact": { "spokeWith": "first name if given", "company": null, "callbackNumber": "number they gave, else null",
               "preferredContact": "call"|"text"|null },
  "vendor": { "handlesTrade": bool|null — do they do this kind of work,
              "servesArea": bool|null — do they cover the property's area,
              "referral": "alternative vendor they suggested, else null" },
  "nextStep": "one sentence: the concrete next step agreed on the call",
  "followUps": [ { "type": "text_confirmation"|"text_photos"|"callback"|"owner_review"|"retry_call"|"other", "detail": "..." } ],
  "flags": { "wantsHuman": bool — asked for a person/owner/management,
             "sensitiveRequest": bool — asked for card/SSN/payment details,
             "reviewNeeded": bool — anything a human should look at },
  "humanRequest": "if wantsHuman, one short sentence of what they wanted, else null",
  "flagReason": null,
  "summary": "max two sentences, written for a property manager"
}
```

Consistency rules to enforce in code after parsing: `result:'scheduled'` ⟺
`appointment.scheduled === true`; `status:'voicemail'` forces `result:'voicemail'`;
`flags.wantsHuman` without a booking → `result:'callback_requested'`.

### 3.5 Firestore security rules (one-time deploy)

Admin SDK bypasses rules, so these only govern the browser page. **Append** to the
existing `firestore.rules` in the propagentic-crm repo (do not modify other matches),
then `firebase deploy --only firestore:rules` on the `propagentic-crm` project:

```
match /voiceCalls/{callId} {
  allow read: if request.auth != null
    && request.auth.token.email_verified == true
    && request.auth.token.email.matches('.*@propagenticai[.]com');
  allow create: if request.auth != null
    && request.auth.token.email_verified == true
    && request.auth.token.email.matches('.*@propagenticai[.]com')
    && request.resource.data.status == 'requested';
  allow update: if request.auth != null
    && request.auth.token.email_verified == true
    && request.auth.token.email.matches('.*@propagenticai[.]com')
    && request.resource.data.diff(resource.data).affectedKeys()
         .hasOnly(['guidance', 'hangupRequested']);
}
```

No composite indexes needed — the page queries use a single `orderBy('createdAt','desc')`.

## 4. Test plan (in order — the first two need no phone at all)

1. **UI-only smoke test**: with rules deployed, open the page (⚙︎ → Data source →
   "Firestore backbone"), assign a call. Verify a `voiceCalls` doc appears with
   `status:'requested'`, the brief, and instructions. The page should show the
   "Call assigned" stage with the brief pre-filled.
2. **Bridge simulation**: with a small script (admin SDK), manually walk one doc
   through the lifecycle — `dialing` → `in_progress` → arrayUnion a few transcript
   turns 3s apart → `completed` → write an `outcome`. Watch the page: stepper
   advances, turns stream in, milestones/chips light up, result strip renders from
   your outcome. This validates the entire contract without dialing anyone.
3. **Live end-to-end**: start `server.js` with `CRM_BRIDGE=1`, assign a call from
   the page to a test phone, answer it. Verify transcript latency feels ~1s,
   guidance sent mid-call shows up in the agent's next reply, "End call" hangs up,
   and buckets land within ~10s of hang-up.
4. **Restart resilience**: kill the bridge mid-call; on restart it should mark its
   orphaned `dialing`/`in_progress` docs `failed` (with `error`) rather than leave
   them open forever (mirror voice-worker's startup reconciliation).

## 5. Guardrails

- **Do not modify** `docs/call-center.html`, `docs/call-data.js`, or
  `voice-call.schema.json` field names/semantics. If a field must change, change the
  schema file in the same PR and say so — the UI and the bridge both read it.
- The page's history view renders whatever is in `voiceCalls` — don't write test
  garbage you don't want a client seeing; delete test docs when done.
- Keep the disclosure rule in whatever prompt you use: the agent answers honestly if
  asked whether it's an AI (one light sentence, then back to the job). The demo
  audience may ask.
- The direct-API mode must keep working (it's the fallback if anything goes wrong
  on demo day) — nothing in this work should touch the ops endpoints.

## 6. File map (already in place)

| File | Repo | What it is |
|---|---|---|
| `docs/call-center.html` | propagentic-crm | The demo page (UI done; dual data-source) |
| `docs/call-data.js` | propagentic-crm | Firestore contract client + fastBuckets reference |
| `voice-call.schema.json` | propagentic-crm | The machine-readable contract |
| `dev-server.py` | propagentic-crm | Local static server + proxy for direct-API mode |
| `crm-bridge.js` | propagentic-voice-agent | **← you build this** |
