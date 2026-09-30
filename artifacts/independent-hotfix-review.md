# Independent Hotfix Review: kanban_complete-Schema und Lifecycle-Loop-Guard

- **Verdict: GO**
- Reviewer: worker-spec (t_f6c0a047), 2026-09-30
- Branch: `hotfix/kanban-lifecycle-loop-guard` @ `b371923540aae46504846c7dafc9552ecd7ce72f`
- Base: `bb0785e73b0154fcffb449c2f2319d7e49597c18` (= pscs/stable HEAD zur Review-Zeit)
- Diff: 8 Dateien, +543/-24, exakt 1 Commit über Base, `git diff --check` CLEAN

## Gates — alle PASS

| Gate | Ergebnis | Beweis |
|---|---|---|
| Schema verlangt nicht-leere `summary`; `result` nicht beworben; kein Auto-Fill | PASS | `required=[] → ["summary"]`, `result`-Property aus Schema entfernt; Handler lehnt None/Whitespace summary strukturiert ab, kein Fallback; `result` bleibt Legacy-Arg via `_UNDECLARED_ARGS` und wird nur neben echter summary als `task.result` gespeichert |
| CLI `--result` bleibt kompatibel | PASS | `hermes_cli/kanban.py::_cmd_complete` ruft `kb.complete_task` direkt — nie `_handle_complete`; CLI unberührt |
| Exakte Fehlerform `{artifacts, board}` reproduziert Handlerfehler | PASS | Test grün gegen echte temp-HERMES_HOME-Kernel-DB; am Base-Commit unabhängig reproduziert: alter Handler meldete „provide at least one of: summary (preferred), result", und result-only komplettierte die Karte (die alte Toleranz) |
| 4. identischer Lifecycle-Call wird vor Ausführung geblockt, Turn endet — auch platform=cli | PASS | Runtime-Tests grün: `handle_function_call` nie beim 4. Call aufgerufen; voller Loop-Replay mit 10 identischen Antworten stoppt nach 3 Ausführungen (`turn_exit_reason=guardrail_halt`); `_decide("block")` ohne hard_stop-Gate (tool_guardrails.py:361) |
| Veränderte Args / mutierender Zwischenfortschritt nicht fälschlich geblockt; Nicht-Lifecycle-Tools unverändert | PASS | Tests zu changed-args, progress-reset, non-lifecycle-Schwellen (soft: nie bei 3; hard: weiterhin 5) grün |
| Keine Karte fälschlich done; Auditgrund secret-safe | PASS | 19 identische fehlerhafte Calls lassen Karte `running`, kein summary/result geschrieben; Blockmeldung ist festes Template (Tool, Anzahl, „NOT completed") — keine Secrets; Redaction-Test grün |
| Eigenständig auf pscs/stable cherry-pickbar, keine Governance-Abhängigkeit | PASS | Base ist pscs/stable HEAD; `rev-list base..head = 1`; alle referenzierten Symbole (`_UNDECLARED_ARGS`, `_goal_gate`, `_progress_since_failure`, `_decide`) existieren am Base; Tests liefen gegen genau diesen Baum |

## RCA-Verifikation an echten Quellen

Bestätigt (empirisch, am Base-Commit in detachtem /tmp-Worktree, System-Runtime):
- Live-Schema hatte `required=[]` → Diff zeigt `[] → ["summary"]`
- `platform=cli` → `hard_stop_enabled=False` (Quelle + `from_mapping({}, platform='cli')` live geprüft)
- Vorher-Repro: **10/10** identische fehlgeschlagene `kanban_complete`-Calls durch `before_call` durchgelassen
- Unattended (platform='cron'): Hard-Stops an, Block erst bei **Versuch 6** (`exact_failure_block_after=5`)
- Alter Handler akzeptierte result-only und setzte die Karte auf `done` — genau die Toleranz, die den Loop ermöglichte

Rezept-bestätigt (SHA256 des Parent-Receipts exakt verifiziert: `20bf2754…a22d`):
- Persistierte GLM-Toolcalls ohne summary/result (Session 20260930_125842_db9c4c, Messages 3262–3297) und Modell-Korrelation (GLM-5.3 51/68 invalid vs. worker-local 0/20, DeepSeek 0/7, Qwen 0/2). Diese State-DB liegt auf dem Orchestrator-Host, von VM125 aus nicht erreichbar; alle codebasierten RCA-Thesen wurden hier unabhängig reproduziert und sind mit der Forensik konsistent. Der Fix ist Defense-in-Depth und hängt nicht an der Korrelations-These.

## Tests auf VM125 (bestehende Runtime)

System-Python 3.12 + leichte apt-Pakete (python3-pytest, -ruamel.yaml, -dotenv, -psutil, -yaml, -httpx) — keine schwere Toolchain, kein venv-Build.

- Neu/geändert: **25/25 grün** — summary-contract (9), lifecycle-loop-guard inkl. 2 voller Runtime-Loop-Tests (11), redaction (5)
- Regressionssweep: **98/98 grün** — tool_guardrails (20), tool_call_guardrail_runtime (15), kanban_tools (45), kanban_stop (8), complete_parents (2), goal-judge-affinity + descendant-scope
- Pre-existing: test_stall_guards (14) + test_delegate_kanban_isolation (1) — **identisch am Base-Commit reproduziert** (15 failed/31 passed, gleiche Runtime) → umgebungsbedingt (Workspace-Layout unter `~/.hermes`), nicht Teil des Diffs; deckt sich mit dem Parent-Receipt

## Nicht-blockende Anmerkungen

- Plugin-Warnungen (httpx vor Installation, nemo_relay) und der SQLite-3.45.1-WAL-Hinweis sind Umgebungseffekte, diff-unabhängig.
- Untracked `.hotfix-sentinel` (Orchestrator-Marker) ist nicht Teil des Commits und wurde nicht angefasst.
- CLI-Hilfertext „--summary falls back to --result" beschreibt bestehendes CLI-Verhalten — durch den Diff nicht geändert.

## Empfehlung

GO für Cutover (t_ccd700de): ff-only aus dem geprüften Hotfix-Clone bzw. Cherry-Pick ausschließlich von `b371923540` plus Review-Doku. Getesteter Source-HEAD: `b371923540aae46504846c7dafc9552ecd7ce72f`, Base unverändert `bb0785e73b0154fcffb449c2f2319d7e49597c18`.
