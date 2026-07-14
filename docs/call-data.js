/**
 * VoiceCallData — the Firestore backbone for the AI Call Center demo page.
 *
 * THE CONTRACT (both sides depend on this; the full machine-readable version
 * is voice-call.schema.json at the repo root, and the integration steps are in
 * BEN-HANDOFF.md):
 *
 *   Collection: voiceCalls   (Firestore project: propagentic-crm)
 *
 *   Lifecycle (the `status` field, written by the agent bridge except step 1):
 *     1. 'requested'    — created by THIS UI when the operator assigns a call.
 *     2. 'dialing'      — agent claimed the request and is placing the call.
 *     3. 'in_progress'  — the line connected; transcript turns are streaming.
 *     4. terminal:      'completed' | 'voicemail' | 'no_answer' | 'failed'
 *
 *   Who writes what:
 *     UI     → creates the doc (status 'requested', brief, instructions),
 *              appends to `guidance[]`, sets `hangupRequested: true`.
 *     Agent  → everything else: status transitions, `agent.*`, `transcript[]`
 *              (arrayUnion, one element per finished turn), `outcome`,
 *              `buckets`, `startedAt` / `endedAt`, `error`.
 *
 *   Transcript turn shape (matches the voice-agent server's existing shape so
 *   the bridge is a one-line arrayUnion in its transcript hooks):
 *     { role: 'assistant' | 'user', text: string, ts: ISO-8601 string }
 *     ('user' = the human callee / contractor; 'assistant' = the AI caller.)
 *
 *   The UI renders ONLY from this data. It never talks to the voice server in
 *   Firestore mode — which is the point: swap the agent freely behind the
 *   contract and the page never changes.
 */
(function (global) {
  "use strict";

  var COLLECTION = "voiceCalls";

  var STATUS = {
    REQUESTED: "requested",
    DIALING: "dialing",
    IN_PROGRESS: "in_progress",
    COMPLETED: "completed",
    VOICEMAIL: "voicemail",
    NO_ANSWER: "no_answer",
    FAILED: "failed"
  };
  var OPEN_STATUSES = [STATUS.REQUESTED, STATUS.DIALING, STATUS.IN_PROGRESS];

  function db() {
    if (!global.firebase || !global.firebase.firestore) {
      throw new Error("Firestore SDK not loaded");
    }
    return global.firebase.firestore();
  }
  function fieldValue() { return global.firebase.firestore.FieldValue; }
  function isOpen(status) { return OPEN_STATUSES.indexOf(status) >= 0; }

  /* ── UI → backbone writes ──────────────────────────────────────────────── */

  /**
   * Create a call request. req = {
   *   toPhone: '+1…' (E.164-ish),
   *   brief: { address, issue, trade, availability: [{day,ranges:[[startHr,endHr]]}], availabilityText, language },
   *   instructions: string   // composed prompt — the agent MAY use its own instead
   * }
   * Resolves with the new doc id (the UI's call handle).
   */
  function requestCall(req) {
    var user = global.firebase.auth().currentUser;
    var doc = {
      status: STATUS.REQUESTED,
      createdAt: fieldValue().serverTimestamp(),
      createdAtIso: new Date().toISOString(),   // display-friendly; serverTimestamp drives ordering
      requestedBy: (user && user.email) || null,
      toPhone: req.toPhone,
      brief: req.brief || null,
      instructions: req.instructions || null,
      guidance: [],
      hangupRequested: false,
      // agent-owned fields, initialized for shape clarity:
      agent: null,           // { name, callSid, version } — set by the bridge on claim
      transcript: [],
      outcome: null,         // structured buckets — see schema
      startedAt: null,
      endedAt: null,
      error: null
    };
    return db().collection(COLLECTION).add(doc).then(function (ref) { return ref.id; });
  }

  /** Live steering note; the agent folds unconsumed notes into its next reply. */
  function sendGuidance(callId, note) {
    return db().collection(COLLECTION).doc(callId).update({
      guidance: fieldValue().arrayUnion({ note: note, ts: new Date().toISOString(), consumed: false })
    });
  }

  /** Ask the agent to end the call; the bridge watches this flag. */
  function requestHangup(callId) {
    return db().collection(COLLECTION).doc(callId).update({ hangupRequested: true });
  }

  /* ── backbone → UI reads (realtime) ────────────────────────────────────── */

  /**
   * Watch the most recent calls (covers the active one + just-finished ones).
   * cb(docs, err): docs = [{id, ...data}] newest first, or null on error.
   * Single orderBy — no composite index needed.
   */
  function watchLatest(cb) {
    try {
      return db().collection(COLLECTION)
        .orderBy("createdAt", "desc").limit(5)
        .onSnapshot(function (snap) {
          cb(snap.docs.map(function (d) { var o = d.data(); o.id = d.id; return o; }));
        }, function (err) { cb(null, err); });
    } catch (e) { cb(null, e); return function () {}; }
  }

  /** Watch call history (newest first). cb(docs, err). */
  function watchHistory(cb, limit) {
    try {
      return db().collection(COLLECTION)
        .orderBy("createdAt", "desc").limit(limit || 300)
        .onSnapshot(function (snap) {
          cb(snap.docs.map(function (d) { var o = d.data(); o.id = d.id; return o; }));
        }, function (err) { cb(null, err); });
    } catch (e) { cb(null, e); return function () {}; }
  }

  /* ── Adapters ──────────────────────────────────────────────────────────── */

  /**
   * Map a voiceCalls doc onto the row shape the page's render pipeline already
   * understands (the voice-agent server's legacy call-record shape), so the UI
   * code is identical across both data sources.
   */
  function toLegacyRow(doc) {
    var o = doc.outcome || {};
    var appt = o.appointment || {};
    var pricing = o.pricing || {};
    var contact = o.contact || {};
    var flags = o.flags || {};
    var transcript = doc.transcript || [];
    var ts = doc.createdAtIso
      || (doc.createdAt && doc.createdAt.toDate ? doc.createdAt.toDate().toISOString() : null);
    return {
      callSid: doc.id,
      timestamp: ts,
      toPhone: doc.toPhone || null,
      transcript: transcript,
      turns: transcript.length,
      userTurns: transcript.filter(function (t) { return t.role === "user"; }).length,
      finalStatus: doc.status,
      brief: doc.brief || null,
      outcome: {
        booked: !!appt.scheduled,
        time: appt.window ? ((appt.day ? appt.day + " " : "") + appt.window) : (appt.day || null),
        fee: pricing.serviceFee || pricing.hourlyRate || null,
        name: contact.spokeWith || null,
        callback_number: contact.callbackNumber || null,
        wants_human: !!flags.wantsHuman,
        human_request: o.humanRequest || null,
        next_step: o.nextStep || null,
        voicemail: doc.status === STATUS.VOICEMAIL || o.result === "voicemail",
        flagged: !!(flags.reviewNeeded || flags.sensitiveRequest),
        flagReason: o.flagReason || null
      }
    };
  }

  /* ── Fast buckets (deterministic, no LLM) ──────────────────────────────── */
  // Reference implementation of the regex layer of the bucketing system: cheap,
  // runs anywhere (browser or agent), fills what pattern-matching can. The LLM
  // pass (see BEN-HANDOFF.md) fills the judgment fields and overrides these.
  var RE_MONEY = /\$\s?\d[\d,]*(?:\.\d{2})?(?:\s?(?:an hour|\/hr|per hour))?|\b\d+\s?dollars\b/gi;
  var RE_TIME = /\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|this week|next week)\b(?:\s+(?:morning|afternoon|evening|night))?(?:\s+(?:at|around|between)\s+[\w:]+(?:\s+and\s+[\w:]+)?)?|\bbetween\s+\w+\s+and\s+\w+\b|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b/gi;
  var TRADES = [
    { re: /\b(a\/?c|air condition|cooling|heat pump|furnace|hvac)\b/i, label: "HVAC" },
    { re: /\b(plumb|drain|clog|sink|toilet|water heater|pipe|disposal)\b/i, label: "Plumbing" },
    { re: /\b(breaker|circuit|electrical|electrician|outlet|panel|wiring)\b/i, label: "Electrical" },
    { re: /\b(roof|flashing|shingle|gutter)\b/i, label: "Roofing" },
    { re: /\b(dishwasher|washer|dryer|fridge|refrigerator|oven|appliance)\b/i, label: "Appliance" }
  ];

  function fastBuckets(transcript) {
    var money = {}, times = {}, spokeWith = null, referral = null, trade = null;
    (transcript || []).forEach(function (t) {
      var text = t.text || "";
      var m;
      RE_MONEY.lastIndex = 0;
      while ((m = RE_MONEY.exec(text))) money[m[0].trim()] = true;
      RE_TIME.lastIndex = 0;
      while ((m = RE_TIME.exec(text))) { if (m[0].trim().length > 4) times[m[0].trim()] = true; }
      if (t.role === "user") {
        var nm = text.match(/\bthis is (\w+)\b/i);
        if (nm && nm[1].length > 1 && !spokeWith) spokeWith = nm[1][0].toUpperCase() + nm[1].slice(1);
        var rf = text.match(/\btry (\w[\w\s]{2,40}?)(?:,|\.|$)/i);
        if (rf && !referral) referral = rf[1].trim();
      }
      if (!trade) {
        for (var i = 0; i < TRADES.length; i++) {
          if (TRADES[i].re.test(text)) { trade = TRADES[i].label; break; }
        }
      }
    });
    return {
      pricing: { mentions: Object.keys(money) },
      scheduling: { mentions: Object.keys(times) },
      contact: { spokeWith: spokeWith },
      vendor: { trade: trade, referral: referral },
      meta: { extractedBy: "fast", extractedAt: new Date().toISOString() }
    };
  }

  global.VoiceCallData = {
    COLLECTION: COLLECTION,
    STATUS: STATUS,
    OPEN_STATUSES: OPEN_STATUSES,
    isOpen: isOpen,
    requestCall: requestCall,
    sendGuidance: sendGuidance,
    requestHangup: requestHangup,
    watchLatest: watchLatest,
    watchHistory: watchHistory,
    toLegacyRow: toLegacyRow,
    fastBuckets: fastBuckets
  };
})(window);
