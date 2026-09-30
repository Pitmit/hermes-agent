// Behavioral probe for the governance helpers shipped in the kanban dashboard
// bundle (governance P1-B2). The bundle has no build step — it IS the source —
// so this extracts the pure helper block verbatim and drives it:
//
//   * govSortInboxRows    — severity desc (critical > high > medium > low),
//                           age desc inside a severity, ref asc tiebreak;
//   * govDecisionEnabled  — only open requests are decidable; a drift-
//                           invalidated or already-decided approval is NOT;
//   * govBeginDecision    — the double-click fence: the second begin on an
//                           in-flight decision is a no-op (null), never a
//                           second POST;
//   * govProgressParts    — unknown ETA renders the explicit "ETA unknown"
//                           marker, never an invented estimate; a known ETA
//                           formats; a run without structured progress
//                           renders nothing at all;
//   * class helpers       — severity / budget-state pill classes resolve.
//
// Run via: node kanban_governance_probe.js <path-to-bundle>
const fs = require("fs");

const bundlePath = process.argv[2];
const src = fs.readFileSync(bundlePath, "utf8");

// --- extract the contiguous pure-helper block -------------------------------
// Anchors are the first const and the last function of the block inserted as a
// whole; a missing anchor means the bundle drifted and the probe fails loudly.
const blockStart = src.indexOf("const GOV_SEVERITY_ORDER");
if (blockStart === -1) { console.error("GOV_SEVERITY_ORDER not found in bundle"); process.exit(1); }
const fnStart = src.indexOf("function govBudgetStateClass", blockStart);
if (fnStart === -1) { console.error("govBudgetStateClass not found in bundle"); process.exit(1); }
const bodyStart = src.indexOf("{", fnStart);
let depth = 0, blockEnd = bodyStart;
for (; blockEnd < src.length; blockEnd++) {
  if (src[blockEnd] === "{") depth++;
  else if (src[blockEnd] === "}") { depth--; if (depth === 0) break; }
}
const helperSrc = src.slice(blockStart, blockEnd + 1);

// Minimal stand-in for the bundle's tx(): dotted-path lookup against a null
// locale table must fall back to the English literal with {var} substitution.
function tx(t, path, fallback, vars) {
  let out = String(fallback);
  if (vars) {
    for (const k of Object.keys(vars)) out = out.split("{" + k + "}").join(String(vars[k]));
  }
  return out;
}

eval(helperSrc);

// --- 1. severity ordering ---------------------------------------------------
const sevRank = { low: 0, medium: 1, high: 2, critical: 3 };
const shuffled = [
  { ref: "t_low", severity: "low", age_seconds: 5000 },
  { ref: "t_med", severity: "medium", age_seconds: 100 },
  { ref: "t_crit", severity: "critical", age_seconds: 10 },
  { ref: "t_high", severity: "high", age_seconds: 900 },
];
const sorted = govSortInboxRows(shuffled);
const sevs = sorted.map(function (r) { return r.severity; });
if (sevs.join(",") !== "critical,high,medium,low") {
  console.error("FAIL: severity order is " + sevs.join(",") + ", expected critical,high,medium,low");
  process.exit(1);
}
// Unknown severity (defensive) never sorts above a known one.
const withUnknown = govSortInboxRows([{ ref: "x", severity: "bogus", age_seconds: 1 }, { ref: "y", severity: "low", age_seconds: 1 }]);
if (withUnknown[0].ref !== "y") {
  console.error("FAIL: unknown severity outranked a known one");
  process.exit(1);
}
// Inside one severity, older first.
const aged = govSortInboxRows([
  { ref: "b_young", severity: "high", age_seconds: 10 },
  { ref: "a_old", severity: "high", age_seconds: 9999 },
]);
if (aged[0].ref !== "a_old") {
  console.error("FAIL: inside one severity the older row must come first");
  process.exit(1);
}
// Input rows are never mutated (pure helper).
if (shuffled[0].ref !== "t_low" || shuffled[3].ref !== "t_high") {
  console.error("FAIL: govSortInboxRows mutated its input");
  process.exit(1);
}

// --- 2. decision gating: stale drift forbids approve -------------------------
for (const open of ["pending", "revision_requested"]) {
  if (govDecisionEnabled(open) !== true) {
    console.error("FAIL: open status " + open + " must be decidable");
    process.exit(1);
  }
}
for (const closed of ["approved", "rejected", "invalidated", "", null, undefined]) {
  if (govDecisionEnabled(closed) === true) {
    console.error("FAIL: status " + closed + " must NOT be decidable (drift/decided rows are terminal)");
    process.exit(1);
  }
}

// --- 3. double-click fence ---------------------------------------------------
let busy = {};
const first = govBeginDecision(busy, "ap_1");
if (!first || first.ap_1 !== true) {
  console.error("FAIL: first decision begin must return the next busy map");
  process.exit(1);
}
const second = govBeginDecision(first, "ap_1");
if (second !== null) {
  console.error("FAIL: a second begin while in flight must be a no-op (null), not " + JSON.stringify(second));
  process.exit(1);
}
// After the decision settles (busy cleared), a new begin works again.
const cleared = {};
const third = govBeginDecision(cleared, "ap_1");
if (!third || third.ap_1 !== true) {
  console.error("FAIL: after clearing the in-flight flag a decision may start again");
  process.exit(1);
}
// Different approvals decide independently.
const other = govBeginDecision(first, "ap_2");
if (!other || other.ap_2 !== true || other.ap_1 !== true) {
  console.error("FAIL: an in-flight decision must not block a different approval");
  process.exit(1);
}

// --- 4. progress rendering: ETA honesty --------------------------------------
// No structured progress at all → nothing to render.
if (govProgressParts({ progress_pct: null, eta_seconds: 300 }, null) !== null) {
  console.error("FAIL: a run without structured progress must render nothing");
  process.exit(1);
}
// Structured progress with an UNKNOWN ETA → the explicit marker, never an
// invented estimate.
const unknownEta = govProgressParts({
  progress_pct: 33, phase: "encoding", completed: 3, total: 9, unit: "files",
  rate: 12, eta_seconds: null, error_count: 0,
}, null);
if (!unknownEta) { console.error("FAIL: structured progress must render parts"); process.exit(1); }
if (unknownEta.indexOf("ETA unknown") === -1) {
  console.error("FAIL: unknown ETA must render 'ETA unknown', got: " + unknownEta.join(" · "));
  process.exit(1);
}
if (unknownEta.some(function (p) { return /ETA \d/.test(p); })) {
  console.error("FAIL: an unknown ETA must never be formatted as a number");
  process.exit(1);
}
// Known values format through: phase, pct, (c/total unit), rate, ETA, errors.
const known = govProgressParts({
  progress_pct: 40, phase: "encoding", completed: 4, total: 10, unit: "files",
  rate: 12, eta_seconds: 1500, error_count: 2,
}, null).join(" · ");
for (const expected of ["encoding", "40%", "(4/10 files)", "12/h", "ETA 25m", "2 errors"]) {
  if (known.indexOf(expected) === -1) {
    console.error("FAIL: progress line missing '" + expected + "': " + known);
    process.exit(1);
  }
}
// ETA formatting bands.
if (govFmtEta(45) !== "45s" || govFmtEta(90) !== "2m" || govFmtEta(-1) !== null || govFmtEta(null) !== null) {
  console.error("FAIL: govFmtEta bands drifted");
  process.exit(1);
}

// --- 5. pill class helpers -----------------------------------------------------
if (govSeverityClass("critical").indexOf("--critical") === -1 ||
    govSeverityClass("HIGH").indexOf("--high") === -1 ||
    govSeverityClass("bogus").indexOf("--low") === -1) {
  console.error("FAIL: severity class mapping drifted");
  process.exit(1);
}
if (govBudgetStateClass("stopped").indexOf("--stopped") === -1 ||
    govBudgetStateClass("warn").indexOf("--warn") === -1 ||
    govBudgetStateClass(null).indexOf("--ok") === -1) {
  console.error("FAIL: budget state class mapping drifted");
  process.exit(1);
}

console.log("PASS");
