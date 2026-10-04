# Independent Governance Review — Verdict GO

Task: t_8cfd4c85 (Governance Gate) · Reviewer: worker-spec · Host: VM125 (vm-worker-spec) · Run 222
Datum: 2026-10-04 (UTC)

## Ergebnis

**GO.** Alle fuenf harten Gates (Scope/Architektur, Security, Correctness, UX/API, Evidence) bestehen. Fuenf Findings, keines blockierend: 1× medium (Provenanz-Typo im Parent-Receipt, nicht im Code), 3× low, 1× info. Der Branch ist cutover-faehig unter den in `independent-review.json` (`cutover_conditions`) genannten Bedingungen.

## Gepruefte Bytes (Exact-Head)

| Was | Wert |
|---|---|
| source_head (getestet) | `370f02c562fd4ebfe6738a47a377c89af2b9f732` |
| source_head tree | `d97011d58fa62571af7ed3abd6d69255159a935a` |
| committed | 2026-10-03T01:56:33+00:00 |
| Historischer Diff | `bb0785e73b…​..HEAD` — 16 Commits, 92 Dateien, +19076/−134 |
| Live-Basis (korrekt!) | `bffb12a6cec643bc0b80bdffc33b8cbb95f67489` |
| Branch-Relativ | 14 Commits (13 Governance-Patches + Spec `dd08215033`) |
| Working Tree | 0 modifizierte getrackte Dateien |

## Gate-Bewertung (Kurzfassung)

1. **Scope/Architektur — PASS.** Keine zweite Control Plane: Routines materialisieren Occurrences als normale Tasks ueber den cron-Occurrence-Ledger (kein zweiter Scheduler), Ledger/Budgets/Approvals leben im einen Board-DB-Schema, Kostenfluss laeuft durch den einen Session-Flush. Migration additiv (Spalten, INSERT OR IGNORE-Seeds, Index); alte Boards/Karten funktionieren (Legacy-Tests gruen). Alle Flags default-off.
2. **Security — PASS.** Board-/Worker-Fencing fail-closed (kein board-Override auf Worker-Tools, HERMES_KANBAN_TASK-Zaun auf Decide/Goal-Writes, Delegated-Child-Fence); keine Selbstfreigabe (requester==approver verweigert); Drift-Invalidierung server-seitig mit exactly-once Event und Commit-bei-Verweigerung; SQL durchgehend parameterisiert (f-Strings nur fuer konstante Tabellen-/Spaltennamen); kein hardkodiertes `~/.hermes`.
3. **Correctness — PASS.** unknown cost ≠ 0 (NULL-Betraege, Summen nur estimated/actual, unknown_runs ehrlich); Budget-Stop pre-claim, laufende Runs unberuehrt; Watchdog exactly-once per UNIQUE(watchdog,fingerprint) + Rounds-Dedup per stop_event_id + Sticky-Eskalation einmalig; unveranderte Loops eskalieren (Round-Limit 3 → triage sticky; zusaetzlich deterministischer Lifecycle-Loop-Guard); Cron/Template idempotent (deterministische Occurrence-Keys, gebundenes Catch-up, atomarer Workflow-Apply mit in-txn Re-Check); Rollups rechnerisch korrekt; sticky Human Gates bleiben sticky (`_unblock_task_in_txn` ist die byte-gleiche Extraktion der bestehenden unblock-Logik; Watchdog-reopen verweigert needs_input/capability).
4. **UX/API — PASS.** Inbox fail-closed und bounded; doppelte Aktionen idempotent (Dedup/Replay-Verweigerung/exactly-once); Progress fail-closed validiert, ETA nie erfunden, pct nur bei total>0; kanban_complete verlangt echtes summary.
5. **Evidence — PASS.** Parent-Receipt (t_aff92192, baseline-relativ GO) verifiziert: SHA256-Spot-Checks 3/3 byte-identisch, Base-Ancestor bestätigt, Vollsuite BASE/HEAD ohne neue Fehler (611 preexisting, 7 durch Branch gefixt), FE-Gates identisch gruen inkl. Branch-Region, gitleaks 3/3 triagiert (0 echt), ruff/diff-check sauber. Zusaetzlich eigene unabhaengige Testlaeufe auf VM125 mit vorhandener Runtime (s.u.).

## Unabhaengige Tests (VM125, System-Python 3.12.3, SQLite 3.45.1)

Alle Governance-Kernel-Module importieren mit der vorhandenen Runtime; croniter==6.0.0 (projekt-deklariert) als einzelnes Wheel nach /tmp entpackt — keine neue schwere Toolchain.

| Suite | Ergebnis |
|---|---|
| test_kanban_approvals + test_kanban_cost_budget | 49 passed |
| test_kanban_inbox + test_kanban_projects + test_kanban_watchdog | 65 passed |
| test_kanban_workflows + test_kanban_progress_heartbeat | 54 passed, 2 failed → F-03 (SQLite-Artefakt im Test-Helper; Ziel-Runtime gruen) |
| test_kanban_routine (tests/cron) | 59 passed (12 initiale Rots nur wegen fehlendem croniter im System-Python) |
| test_session_spend_totals + test_kanban_lifecycle_loop_guard | 19 passed |
| test_kanban_complete_summary_required | 9 passed |
| test_kanban_redaction + test_kanban_tools + test_write_approval | 70 passed |
| test_kanban_governance_dashboard (plugins) | lokal uebersprungen (fastapi fehlt) — gedeckt durch Parent-Receipt A1/A2 + Dashboard-Receipt |

## Findings

- **F-01 (medium, non-blocking) — Provenanz-Typo im Parent-Receipt.** `artifacts/baseline-relative/receipt.json` base.sha liest `…c33c8…`, korrekt ist `…c33b8…` (Index 27, `git rev-parse bffb12a6`). Testergebnisse unberuehrt (Clone resolvierte den Kurz-SHA korrekt); die Cutover-Karte nutzt bereits den korrekten SHA. Massgeblich fuer Verifikation: git / dieses Review, nicht der Receipt-String.
- **F-02 (low, Folge-Haertung) — Redaktions-Konsistenz.** `kanban_approval_request(note)`, `kanban_watchdog_create(instructions)`, `kanban_watchdog_decide(note)` werden unredigiert persistiert, waehrend derselbe Branch Heartbeat-phase/unit explizit mit dem kanban_comment-Redaktor schuetzt. Praezedenz ambivalent (heartbeat `note` war auch vor dem Branch unredigiert); kein aktiver Leak (gitleaks 0 echt). Empfehlung: chirurgischer Folge-Commit mit `_redact()` an drei Stellen.
- **F-03 (low, test-only) — SQLite-Portabilitaet.** `_drop_progress_columns` (tests/hermes_cli/test_kanban_progress_heartbeat.py:240) scheitert auf SQLite 3.45.1 (`incomplete input` beim Re-Parse wegen Inline-Kommentaren im task_runs-Schema). Produktion nutzt DROP COLUMN nie; Ziel-Runtime gruen.
- **F-04 (low) — dokumentierte Idempotenz-Race.** create_task dedupliziert vor dem write_txn (dokumentiert); Workflow-Apply schliesst die Race in-txn, Routine reitet auf dem Occurrence-Ledger. Restrisiko begrenzt.
- **F-05 (info) — Goal/Budget-Kopplung nach Goal-Commit** (kanban_projects.py:318), dokumentierter Tradeoff, self-heal beim naechsten Aufruf.

## Cutover-Bedingungen

1. source→delivery darf ausschliesslich den einen Review-Artefaktcommit (diese zwei Dateien) enthalten; delivery_head, source_head und SHA256 beider Dateien werden in der kanban_complete-Metadaten dieses Runs uebergeben.
2. Erwartete Live-Basis: exakt `bffb12a6cec643bc0b80bdffc33b8cbb95f67489` — nicht der Receipt-String (F-01).
3. F-02 kann vor oder nach Cutover als eigener Commit erfolgen; blockiert nicht.

Vollstaendige maschinenlesbare Bewertung: `artifacts/independent-review.json`.
