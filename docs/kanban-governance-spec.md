# Kanban-Governance-Spezifikation und Migrationsplan (P0/P1)

- **Task:** t_ff05f719 (Tenant pscs)
- **Base:** `bb0785e73b0154fcffb449c2f2319d7e49597c18`, Branch `feat/kanban-governance-p0`
- **Status:** Spezifikation (kein Code), rückwärtskompatibel, migrationssicher
- **Ansatz:** Paperclip-inspirierte Governance Hermes-nativ — eine Control Plane (bestehende kanban.db + Dispatcher-Tick + `kanban`-Toolset + Dashboard-Plugin), keine zweite.

---

## 0. Zusammenfassung

Acht Stufen: (1) Kostenledger je `task_run` plus Monatsbudgets je Profile/Project/Tenant mit Warnschwelle und hartem Dispatch-Stop, (2) generische Approvals mit Subjekt-Fingerprint und Drift-Invalidierung, (3) Aktivierung des bestehenden `project_id` mit Ziel/Owner/Budget/Rollup statt Goal-Engine, (4) Blocked Inbox über die vorhandene Diagnostics-Engine, (5) unabhängiger nicht-reparierender Reviewer/Watchdog mit Outcome-Fingerprint, (6) Cron→Kanban-Routinen mit stabilem Idempotency-Key plus Aktivierung der dormant Workflow-Template-Spalten, (7) strukturierte Progress/ETA-Heartbeats mit Dashboardprojektion, (8) RBAC/mobile explizit außerhalb P0/P1.

Alle Stufen sind **additiv**: neue Tabellen nur per `CREATE TABLE IF NOT EXISTS`, neue Spalten nur per `add_column_if_missing` (bestehendes Migrationsmuster, `hermes_cli/kanban_db_connect.py:797/820/833`), neue Config-Keys nur unter `kanban.*` in `DEFAULT_CONFIG` mit Default `enabled: false` (Deep-Merge, kein `_config_version`-Bump nötig). Mit allen Flags aus verhält sich der Dispatcher byte-identisch zum Ist-Zustand.

---

## 1. Recherche-Befund: Vorhandenes vs. Eigenbau (Belege der Suche)

Vor der Spezifikation wurde im Shared-Repo (Base `bb0785e73b`) nach vorhandenen/dormanten Implementierungen gesucht (`grep`/`search_files` über `hermes_cli/kanban*.py`, `cron/`, `tools/`, `agent/`, `plugins/kanban/`, `tui_gateway/`, `website/docs/`).

| Anforderung | Vorhandene Basis (Beleg) | Dormant/Upstream | Entscheidung |
|---|---|---|---|
| Budgets/Kosten | `session_model_usage` je Profil-state.db mit `estimated_cost_usd`, `actual_cost_usd`, `cost_status`, `cost_source`, Token-Zählern (`hermes_state_usage.py:29-79`, DDL `hermes_state_schema.py:70`); `hermes_cli/model_cost_guard.py` (nur Auswahl-Warnung teurer Modelle) | **Kein** Kanban-Kostenbezug, **kein** Budget, kein Ledger je task_run. „Budget“ in Kanban-Code meint ausschließlich Retry-/Spawn-/Turn-Budgets (`kanban_db_dispatch.py:2282 _tick_spawn_budget`, `kanban_db.py:733`) | Ledger-Tabelle in kanban.db + Attribution über Worker-Env; Wiederverwendung der Usage-Felder/Semantik (`cost_status`/`cost_source`) |
| Approvals | Review-Freigaben: Mensch ist die Instanz, von Evidence-Pflicht befreit (`kanban_db.py:2737-2749`, `:2877`); Tool-Call-Approval-Mode `manual/smart/off` (`hermes_cli/approval_mode.py`) — andere Domäne | **Kein** generisches Approval-Objekt, kein Fingerprint, keine Inbox | Neue `approvals`-Tabelle; Inbox nur als Projektion |
| Projects/Goals | `tasks.project_id` existiert und verankert Worktrees (`kanban_db.py:889-892`); First-class Projects in `hermes_cli/projects_db.py` (id, slug, name, board_slug, primary_path, …); Goal-Loop je Task existiert (`goal_mode`/`goal_max_turns`, `kanban_db.py:946-955`); `/goal`-Engine ist Session-Semantik (`hermes_cli/goal_command.py`) | Projects haben **kein** Ziel/Owner/Budget/Rollup; keine Project-Governance | Bestehendes `project_id` aktivieren; Governance-Felder board-seitig; **keine** neue Goal-Engine |
| Blocked Inbox | `block_kind` ∈ {dependency, needs_input, capability, transient} (`kanban_db.py:107`); Sticky-Block + `BLOCK_RECURRENCE_LIMIT = 2` → triage (`kanban_db.py:111`, `:963-975`); Diagnostics-Engine mit severity warning/error/critical, Alter-Eskalation, read-only (`kanban_diagnostics.py:20`, `:594-620` stuck_in_blocked, `:682-736` stranded_in_ready) | Kein Inbox-Begriff im Kanban-Code (grep „inbox“ in `hermes_cli/kanban*.py`, `tools/kanban_tools.py`, `plugins/kanban` = 0 Treffer); kein Auto-Unblock existiert heute | Inbox = Projektion über blocked-Tasks + Diagnostics; Auto-Unblock-Verbot als Invariante festschreiben |
| Watchdog | `hermes_cli/kanban_diagnostics.py`: stateless, read-only, regelbasiert, konfigurierbar (`kanban.diagnostics.*`), „deliberately mutates nothing“ (`kanban_diagnostics.py:513`); Cron-Inactivity-Watchdog (`cron/AGENTS.md`) | `hermes_startup_watchdog.py` = Gateway-Startup (andere Domäne); **keine** Governance-Regeln (Budget, Approval-Staleness, Review-Runden) | Watchdog = neue Regeln in der bestehenden Diagnostics-Engine; Reparaturverbot beibehalten |
| Reviewer | Review-Spalte + Auto-Claim mit erzwungenem `sdlc-review`-Skill (`kanban_db_dispatch.py:1785-1796`, `:2194-2196`); Verben `request-review/request-changes/reopen-review` (`hermes_cli/kanban.py`); `claim_review_task` mit Parent-Reopen-Guard (`kanban_db.py:2304-2330`); PR-Completion-Contract mit published_pr-Gate (`kanban_pr_acceptance.py:22`) | **Kein** Rundenzähler, kein Outcome-Fingerprint, keine Drift-Invalidierung (grep „round/unchanged“ in `hermes_cli/kanban*.py` = 0 relevante Treffer) | Additive Spalten `review_rounds`/`review_subject_fingerprint`; genau-drei-Runden-Regel analog BLOCK_RECURRENCE_LIMIT |
| Workflow-Templates | `tasks.workflow_template_id` + `current_step_key` existieren als **dormant** v2-Forward-Compat: „In v1 the kernel writes these … the dispatcher doesn't consult them“ (`kanban_db.py:918-922`, `:1005-1007`); Parser/Dashboard-Query exponieren sie bereits (`kanban_parser.py:245`, `plugins/kanban/dashboard/plugin_api.py:287/352`) | **Kein** Template-Store, kein Routing, keine Cron→Kanban-Brücke (grep „kanban“ in `cron/*.py` = nur Kommentare) | Dormante Spalten aktivieren; Cron-Occurrence-Ledger als Idempotency-Quelle |
| Progress/ETA | `kanban_heartbeat`-Tool mit Freitext-Note (`tools/kanban_tools.py:497-555`); `heartbeat_worker` (`kanban_db_dispatch.py:610-645`); TUI-Projektion `KanbanActivityRun` mit `last_heartbeat_at`/`outcome` (`tui_gateway/contracts/kanban_activity.py`) | **Kein** strukturiertes Progress/ETA-Feld; `max_runtime_seconds` ist das einzige Zeitbudget | Strukturierte Felder additiv auf `task_runs`/`tasks` + Tool-Parametern |
| Budget-Konfiguration | `kanban.*` in `DEFAULT_CONFIG` (`hermes_cli/config_defaults.py:1861+`): `dispatch_in_gateway`, `review_dispatch`, `failure_limit`, `max_in_progress`, `max_in_progress_per_profile`, Claim-Allowlist je Home | — | Budget-Limits **nicht** in config.yaml (Profil-lokal), sondern in der geteilten Board-DB |

Dokumentation konsultiert: `website/docs/user-guide/features/kanban.md` (u. a. „Idempotent create (for automation / webhooks)“, PR-Completion-Contracts), `cron/AGENTS.md`, `tools/AGENTS.md`, `plugins/AGENTS.md`, `tui_gateway/AGENTS.md`, `hermes_cli/AGENTS.md`.

---

## 2. Architekturprinzipien

1. **Eine Control Plane.** Alle Governance-Zustände leben in der Board-DB (kanban.db); Enforcement läuft im Dispatcher-Tick; Sichtbarkeit über die existierenden Flächen (CLI `hermes kanban`, Toolset `tools/kanban_tools.py`, Dashboard-Plugin `plugins/kanban/dashboard/`, TUI-Aktivitätsprojektion). Keine zweite Engine, kein zweiter Scheduler, kein separater Governance-Dienst.
2. **Board = harte Grenze, Tenant = weicher Namespace.** Unverändert (`cron/AGENTS.md` Kanban-Abschnitt). Governance-Zeilen tragen `board` und respektieren `tenant`.
3. **Additive Migration.** Nur `CREATE TABLE IF NOT EXISTS` + `add_column_if_missing`; alte DBs öffnen sich immer sicher (Muster `projects_db.py`, `kanban_db_connect.py`). Kein Verändern/Löschen bestehender Spalten.
4. **Feature-Flags je Stufe**, Default off (`kanban.budgets.enabled` usw.). Flags wirken im Dispatcher-Heim (Config ist Profil-lokal); die *Daten* (Limits, Approvals) liegen board-seitig, damit alle Homes dieselben Zeilen sehen.
5. **Footprint-Ladder.** Kein neues Core-Tool; Worker-seitige Erweiterungen gehen in das bestehende `kanban`-Toolset (Rung 3), CLI-Verben in `hermes_cli/kanban*.py`-Sibling-Struktur, UI in Dashboard-Plugin/TUI-Kontrakt.
6. **Ehrlichkeit vor Gefälligkeit.** Unbekannte Kosten = `cost_status='unknown'` + NULL-Betrag, **nie 0**; nicht messbare Größen heißen `unknown`, nicht „fertig“.
7. **Nicht geschwächt werden:** completion_contract-/PR-Acceptance-Gate (`kanban_pr_acceptance.py`), Claim-Fence (PID + Startzeit-Fingerprint, `kanban_db_dispatch.py:361-406`), Review-Exemptions, Tenant-/Board-Isolation, Artifact-Semantik, Claim-Allowlist, Sticky-Block-/Recurrence-Routing.

---

## 3. Stufe 1 — Kostenledger je task_run und Monatsbudgets (P0)

### Datenmodell (Board-DB)

```sql
CREATE TABLE IF NOT EXISTS task_run_costs (
    run_id            INTEGER PRIMARY KEY,       -- FK task_runs.id
    task_id            TEXT NOT NULL,
    board             TEXT NOT NULL,
    tenant            TEXT,
    project_id        TEXT,
    profile           TEXT,                      -- ausführendes Worker-Profil
    input_tokens      INTEGER,                   -- NULL = nicht gemessen
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens  INTEGER,
    api_call_count    INTEGER,
    estimated_cost_usd REAL,                     -- NULL wenn unknown (nie 0)
    actual_cost_usd   REAL,
    cost_status       TEXT NOT NULL DEFAULT 'unknown',
                      -- 'estimated' | 'actual' | 'unknown'
    cost_source       TEXT,                      -- z. B. 'session_model_usage:<profile>'
    period            TEXT NOT NULL,             -- 'YYYY-MM' (UTC) zum Lauf-Start
    recorded_at        INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trc_period ON task_run_costs(period);
CREATE INDEX IF NOT EXISTS idx_trc_task ON task_run_costs(task_id);

CREATE TABLE IF NOT EXISTS kanban_budgets (
    board             TEXT NOT NULL,
    scope             TEXT NOT NULL,   -- 'profile' | 'project' | 'tenant' | 'board'
    ref               TEXT NOT NULL,   -- Profilname | project_id | Tenant | '*'
    period            TEXT NOT NULL,   -- 'YYYY-MM'; 'persist' = monatlich wiederkehrend
    limit_usd         REAL NOT NULL,
    warn_ratio        REAL NOT NULL DEFAULT 0.8, -- Warnung bei MTD >= limit*warn_ratio
    created_by        TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    PRIMARY KEY (board, scope, ref, period)
);
```

- Feldnamen/Semantik bewusst identisch zu `session_model_usage` (`hermes_state_usage.py:54-79`), damit Reconciliation und UI keine zweite Wertesemantik erlernen.
- `period` wird beim Schreiben aus Lauf-Start berechnet und gespeichert (Monatswechsel mitten im Lauf bleibt der Startperiode zugeordnet — deterministisch, kein Update-Sturm).
- Backfill bestehender `task_runs`: eine Ledger-Zeile mit `cost_status='unknown'`, NULL-Kosten (ehrlich, nie 0). Kein Rückschluss aus Token-losigkeit auf „kostenlos“.

### Attribution (Mechanismus, minimal)

1. Dispatcher schreibt beim Claim die Run-ID zusätzlich in die Worker-Umgebung: `HERMES_KANBAN_RUN_ID` (neben bestehendem `HERMES_KANBAN_TASK`).
2. Der Worker (Agent-Loop) kennt seine Session-Usage (`agent/turn_usage.py`-Pfad); beim Lauf-Ende (Completion/Block/Timed-out-Handler) schreibt er **genau eine** Ledger-Zeile über die bestehende kanban-DB-Verbindung — gleiches Fail-open-Muster wie `heartbeat_worker` (`kanban_db_dispatch.py:610`): Cost-Flush darf den Lauf niemals kippen (Lektion: „ein Bericht darf nie die Ursache eines Abbruchs sein“).
3. Reconciliation (optional, P1): Ein Lesepfad vergleicht Worker-Session-Usage aus `session_model_usage` des Assignee-Profils mit der Ledger-Zeile und aktualisiert `actual_cost_usd`/`cost_source` — niemals `estimated` überschreiben, wenn `actual` fehlt.

### Enforcement (Dispatcher-Tick, kein zweiter Pfad)

- Vor jedem Claim: MTD-Summe je Scope aus `task_run_costs` (nur `cost_status IN ('estimated','actual')`) gegen `kanban_budgets`.
- `MTD >= limit` → Task wird **nicht** gespawnt; genau **ein** `budget_stopped`-Event je (task, period) (Idempotenz über Event-Payload-Prüfung), Task bleibt `ready`. **Kein** Statuswechsel, kein Block (Block ist menschliche Semantik; der Stop ist ein Dispatch-Gate wie `max_in_progress`).
- `MTD >= limit * warn_ratio` → ein `budget_warn`-Event je (task, period) + Zustellung über bestehende Notify-Subs (`kanban_notify_subs`).
- Unknown-Anteil: Der Budget-Report weist `unknown_runs`/`unknown_share` aus. Policy-Entscheidung (P0): Hard-Stop zählt **nur bekannte** Kosten; ein konfigurierbares `kanban.budgets.unknown_policy: allow|flag` (Default `allow`) markiert Perioden mit hohem Unknown-Anteil im Dashboard. Kein heimliches Uminterpretieren von unknown als 0.

### Migration

- Neue Tabellen (oben), neue Env `HERMES_KANBAN_RUN_ID`, neue Config-Keys `kanban.budgets.{enabled,unknown_policy}` (Default `false`/`allow`).
- Ledger-Backfill für historische Runs: eine `unknown`-Zeile je historischem `task_runs`-Eintrag, idempotent per `INSERT OR IGNORE` auf `run_id`.
- Kein `_config_version`-Bump (nur neue Keys, Deep-Merge).

### Flächen

- **CLI:** `hermes kanban budget set <scope> <ref> --limit USD [--warn 0.8] [--monthly]`, `budget show [--period YYYY-MM]`, `budget rm`. Ausgabe inkl. MTD, Unknown-Anteil, betroffener Tasks.
- **Tools (Worker):** `kanban_budget_show` — read-only (Worker dürfen Limits sehen, **nie** setzen).
- **Dashboard:** `GET /board/…/budgets` + Budget-Panel im Plugin; TUI-Aktivität erhält `budget_state` je Board (additiver Kontrakt-Feld-Anhang).

### Sicherheitsinvarianten

- Ledger schreibbar nur für den Claim-Owner der `run_id` (Fence analog `expected_run_id` in `heartbeat_worker`); Fremd-Updates werden abgelehnt.
- Budget-Limits nur setzbar über CLI/Dashboard/Gateway durch den Menschen bzw. ein Profil mit Board-Schreibrecht; **nicht** über das Worker-Toolset.
- Budget-Stop schwächt weder Claim-Fence noch Allowlist noch `max_in_progress`; er ist ein zusätzliches Gate davor.
- Kein Selbst-Freikaufen: ein Worker kann seine eigenen Kosten nachträglich nicht senken (Ledger-Zeilen sind append/eigentümergebunden; Korrekturen nur durch Reconciliation mit `cost_source`-Beleg).

### Tests

- E2E gegen Temp-`HERMES_HOME` + frische Board-DB: (a) Completion schreibt exakt eine Ledger-Zeile mit echten Token-Zählern; (b) Limit unterschritten→Claim normal; (c) Limit überschritten→kein Spawn + genau ein `budget_stopped`-Event bei zwei Ticks (Idempotenz); (d) Warnschwelle→`budget_warn` einmalig; (e) Modell ohne Preisdaten→`cost_status='unknown'`, `estimated_cost_usd IS NULL` (nie 0); (f) historische DB öffnet sich, Backfill idempotent; (g) Flags off → Dispatcher-Verhalten byte-identisch (keine Ledger-Interaktion).

### Binäre Akzeptanzkriterien

1. `sqlite3 kanban.db "SELECT COUNT(*) FROM task_run_costs WHERE cost_status='unknown' AND estimated_cost_usd=0"` = 0 (unknown ist nie 0).
2. Bei überschrittenem Limit erzeugt `dispatch_tick` über zwei aufeinanderfolgende Ticks **genau ein** `budget_stopped`-Event und spawnt **keinen** Worker (rc/Event-Log prüfbar).
3. Jede Completion erzeugt genau eine Ledger-Zeile je Run (`run_id` ist PRIMARY KEY).
4. Alte Board-DB (Snapshot vor Migration) öffnet nach Migration mit `hermes kanban list` ohne Fehler.

---

## 4. Stufe 2 — Generische Approvals (P0)

### Datenmodell (Board-DB)

```sql
CREATE TABLE IF NOT EXISTS approvals (
    id                  TEXT PRIMARY KEY,          -- 'ap_<hex>'
    board               TEXT NOT NULL,
    tenant              TEXT,
    type                TEXT NOT NULL,             -- 'strategy'|'hire'|'budget'|'action'|'release'
    subject_kind        TEXT NOT NULL,             -- 'task'|'budget'|'document'|'release_plan'
    subject_id          TEXT,                      -- task_id / budget-scope-key / doc-Hash
    subject_fingerprint TEXT NOT NULL,            -- sha256(canonical(subject))
    requester           TEXT NOT NULL,             -- Profil
    requested_at        INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
                        -- 'pending'|'approved'|'rejected'|'revision_requested'|'invalidated'
    approver            TEXT,
    decided_at          INTEGER,
    decided_via         TEXT,                      -- 'cli'|'dashboard'|'gateway'
    decision_note       TEXT,
    invalidation_reason TEXT,
    period              TEXT                        -- für type='budget': 'YYYY-MM'
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(board, status);
```

- **Subjekt-Fingerprint:** kanonische JSON-Serialisierung des Entscheidungssubjekts, dann sha256. Für `task`: `{task_id, title, body, completion_contract, result_sha256, artifacts:[{name,sha256}]}`; für `budget`: `{board, scope, ref, period, limit_usd}`; für `document`/`strategy`: Dokumentinhalt; für `release`: Release-Plan + referenzierte Commit-/Artifact-SHAs.
- **Drift-Invalidierung:** bei jeder Entscheidung (approve/reject/revision) und bei jeder Inbox-/Status-Ablesung wird der Fingerprint aus dem **aktuellen** Subjekt neu berechnet. Abweichung → `status='invalidated'`, `invalidation_reason='subject_drift'`, ein `approval_invalidated`-Event; es bedarf einer neuen Anfrage. Eine Entscheidung auf driftendes Subjekt ist ein No-op mit Fehlermeldung (fail-closed).
- **Inbox:** keine eigene Tabelle — Projektion (SQL über `approvals` pending + blocked Tasks + Review-Anfragen, siehe Stufe 4).

### Workflow

1. **Request:** Worker/Orchestrator stellt Anfrage (Tool) oder Mensch via CLI. Anfragen mit `type='action'/'release'` können optional einen Task blockieren: `kanban_block(kind='needs_input', reason='approval:<ap_id>')` — existierende Sticky-Block-Semantik, kein neuer Blocktyp.
2. **Decision:** nur über Menschengesteuerte Flächen (CLI `hermes kanban approval approve|reject|revise <id>`, Dashboard, Gateway-Chat). `requester == approver` ist verboten (keine Selbstgenehmigung); Worker-Toolset hat **kein** Approve-Werkzeug.
3. **Wirkung:** Entscheidung schreibt Status + Fingerprint + Event. Bei `approved` werden Tasks, deren Block-Reason exakt `approval:<id>` ist, von der Entscheidungs-Transaktion freigegeben (das ist die menschliche Entscheidung, kein Auto-Unblock durch Cron/Dispatcher).
4. **Revision:** `revision_requested` hält die Anfrage offen mit neuem Kommentarpfad; Subjekt-Änderung erzeugt neuen Fingerprint → alte Anfrage invalidiert, neue nötig.

### Migration

Nur die neue Tabelle + CLI-Verben + Tool-Parameter; Config `kanban.approvals.enabled` (Default false). Keine Änderung an `block_kind`-Werten.

### Flächen

- CLI: `hermes kanban approval request|list|approve|reject|revise|show <id>`.
- Tools: `kanban_approval_request(type, subject_kind, subject_ref, note)` für Worker; `kanban_show` erweitert um Approval-Hinweis auf blockierte Tasks.
- Dashboard/TUI: Approval-Inbox-Sektion (Bestandteil der Inbox-Projektion aus Stufe 4).

### Sicherheitsinvarianten

- Approve ist **nie** automatisierbar; Dispatcher/Cron/Watchdog dürfen Approvals nie entscheiden.
- Fingerprints werden serverseitig (Kernel) berechnet, nie vom Aufrufer übernommen.
- Invalidierung ist fail-closed: im Zweifel `invalidated`, nie „gilt weiter“.
- Vorhandene Review-Exemptions (Mensch voucht, `kanban_db.py:2877`) bleiben unberührt — Approvals sind ein **zusätzlicher** Typ, kein Ersatz des Review-Flows.

### Tests

- (a) approve nach Body-Änderung → `invalidated`, zweite approve abgelehnt; (b) `requester==approver` abgelehnt; (c) approve gibt genau die Tasks mit `approval:<id>` frei, keine anderen; (d) Worker-Toolset enthält kein Approve (Schema-Contract-Test); (e) Fingerprint deterministisch (zwei identische Subjekte → gleicher Hash); (f) alle Statusübergänge erzeugen Events (Audit vollständing).

### Binäre Akzeptanzkriterien

1. `hermes kanban approval approve <id>` auf gedriftetem Subjekt endet mit Exit != 0 und `status` bleibt/`invalidated`, es gibt genau ein `approval_invalidated`-Event.
2. Keine Approve-Funktion im Worker-Toolset erreichbar (Registry-Test).
3. Approve verändert nur Tasks mit exaktem Block-Reason `approval:<id>` (Vorher/Nachher-Statusdiff).

---

## 5. Stufe 3 — Projects/Goals: `project_id` aktivieren (P0)

### Datenmodell (Board-DB; projects.db bleibt unangetastet)

```sql
CREATE TABLE IF NOT EXISTS kanban_project_goals (
    board          TEXT NOT NULL,
    project_id     TEXT NOT NULL,     -- referenziert tasks.project_id (id aus projects_db)
    goal           TEXT,              -- Ziel-Vertrag (Freitext, Teil des Rollups)
    owner          TEXT,              -- verantwortliches Profil (Anzeige)
    monthly_budget_usd REAL,          -- optional; speist Stufe-1-Budget scope='project'
    status         TEXT NOT NULL DEFAULT 'active',  -- 'active'|'achieved'|'abandoned'
    created_by     TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (board, project_id)
);
```

- `projects.db` (Desktop-Registry, `hermes_cli/projects_db.py`) ist **nicht** Governance-Speicher: sie ist Profil-lokal, Board-DB ist die geteilte Ebene. Die Ziel-Tabelle liegt daher board-seitig; `project_id` bleibt der gemeinsame Schlüssel.
- **Rollup = reine Leseprojektion** (SQL über `tasks` + `task_run_costs` + `kanban_project_goals`): offene Tasks je Status, blocked-Anzahl, MTD-Kosten, letzte Aktivität, Zielerreichung (`status`), Budgetauslastung. Kein materieller Cache; die Dashboard-Query ist eine einzige SQL mit Indizes auf `tasks(project_id)` (Index additiv).
- **Keine Goal-Engine:** Ziele sind Vertrag + Rollup. Die bestehende `goal_mode`-Schleife (je Task) und die `/goal`-Session-Semantik bleiben genau wie sie sind; ausdrücklich kein Decomposition-/Judge-Zusatz auf Project-Ebene.

### Migration

- Neue Tabelle + `CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)`.
- Falls `monthly_budget_usd` gesetzt wird, legt der Kernel die korrespondierende `kanban_budgets`-Zeile (scope='project') an/aktualisiert — ein Schreibpfad.
- CLI: `hermes kanban project goal <project_id> [--text …] [--owner …] [--budget USD]`, `hermes kanban project rollup <project_id>`.

### Sicherheitsinvarianten

- Worktree-Anker-Semantik von `project_id` (`kanban_db.py:889-892`) wird nicht verändert.
- Rollup ist read-only; keine abgeleiteten Statuswechsel.
- Ziele/Budgets nur über CLI/Dashboard/Gateway setzbar (Mensch), nicht über Worker-Tools.

### Tests

- (a) Rollup zählt Tasks/Daten einer Beispieldatenbank korrekt (Beziehungstest, kein Snapshot); (b) Budget-Set schreibt genau eine `kanban_budgets`-Zeile (idempotent bei Doppelaufruf); (c) alte DB mit project_id-freien Tasks → Rollup liefert 0/leer, kein Fehler; (d) `hermes kanban project goal` ohne existierenden Project-Eintrag in projects.db: Kernel akzeptiert den project_id als opaken Schlüssel (Board-first), Dokumentation nennt die Herkunft.

### Binäre Akzeptanzkriterien

1. `hermes kanban project rollup <id>` liefert für eine frisch erstellte Project-Aufgabe: ≥1 offener Task, MTD-Kosten = Ledger-Summe des Projects (Vergleich SQL vs. CLI-Ausgabe identisch).
2. `monthly_budget_usd=10` erzeugt (genau) eine `kanban_budgets`-Zeile `(scope='project', ref=<id>)`.
3. Keine neue Tabelle/Spalte in projects.db (`git diff` über `hermes_cli/projects_db.py` = leer).

---

## 6. Stufe 4 — Blocked Inbox (P0)

### Datenmodell

**Keine neue Tabelle.** Inbox ist eine Projektion über:

- `tasks` mit `status='blocked'` (+ `block_kind`, letzter `blocked`-Event-Zeitstempel),
- `approvals` mit `status='pending'` (Stufe 2),
- Diagnostics mit blocked-relevanten Regeln (`stuck_in_blocked`, `block_unblock_cycling`, `review_dependency_deadlock` — alle bereits vorhanden, `kanban_diagnostics.py:594/623/510`).

Abgeleitete Felder der Projektion:

- **severity:** aus `block_kind` + Alter. Basiszuordnung: `capability`→error, `needs_input`→warning, `transient`→warning, `dependency`→info (nur bei Deadlock-Rule→error). Alter-Eskalation wie `stranded_in_ready` (`kanban_diagnostics.py:712-718`): <2× SLA-Schwelle warning, 2-6× error, >6× critical.
- **action_owner:** `needs_input`→anfragendes Profil/Mensch (aus Block-Reason/Requester), `capability`→Mensch/Ops, `transient`→Dispatcher-Retry, `dependency`→Parent-Task.
- **stopped_age / SLA:** `blocked_since` = `created_at` des letzten `blocked`-Events; optionale per-Task-Schwelle über neue additive Spalte `tasks.block_sla_hours` (NULL = Boardschwelle aus `kanban.diagnostics.blocked_stale_hours`, Default 24).

### Neue Diagnostics-Regeln (Registry-Erweiterung, read-only)

- `approval_pending_stale`: pending-Approval > `kanban.diagnostics.approval_stale_hours` (Default 48) → warning/error.
- `budget_stopped`: Task wurde durch Budget-Stop gegated → info/warning (Sichtbarkeit im Inbox).
- `cost_unknown_share` (Board-Ebene, P1): hoher Unknown-Anteil einer Periode → warning.

### CLI/UI

- `hermes kanban inbox [--severity warning|error|critical] [--kind approval|blocked|review] [--json]` — sortiert nach severity, dann Alter.
- Dashboard: Inbox-Panel im bestehenden Plugin (nutzt bereits die Diagnostics-Severity-Filter, `hermes_cli/kanban.py:665-669` Muster).
- Gateway-Chat: `/kanban inbox` — Ausgabe, keine Mutation.

### Sicherheitsinvarianten

- **Kein Auto-Unblock** für `block_kind ∈ {needs_input, capability}` (Menschen-/Credential-/Safety-Gates): der Dispatcher und Cron unblocken **nie**; nur `kanban_unblock` durch eine Menschengesteuerte Fläche oder eine Approval-Entscheidung (Stufe 2) gibt frei. `transient` bleibt beim bestehenden Dispatcher-Retry-Verhalten (das ist heute schon kein Unblock, sondern Reclaim/Retry).
- Sticky-Block + `block_recurrences`/`BLOCK_RECURRENCE_LIMIT` → triage bleibt unverändert.
- Die Inbox mutiert nichts — sie ist ein reiner Leseendpunkt.

### Tests

- (a) `needs_input`-Task älter als 2× SLA → severity error in Inbox-Ausgabe; (b) Dispatcher-Tick über 3 Perioden verändert blocked-Aufgaben mit `needs_input`/`capability` **nie** (Status-Diff leer); (c) Approval-Entscheidung gibt nur den gebundenen Task frei; (d) Inbox-Ausgabe enthält approvals + blocked + review-Anfragen in einer Abfrage (Reihenfolge severity-first); (e) SLA-Override je Task greift (`block_sla_hours=1` → frühere Eskalation).

### Binäre Akzeptanzkriterien

1. Für jeden blocked Task mit `block_kind='needs_input'` gilt nach N≥3 Dispatcher-Ticks ohne menschlichen Eingriff: `status` unverändert `blocked` (Assert über Statusdiff).
2. `hermes kanban inbox --json` ist valides JSON und enthält für jede Zeile `severity`, `action_owner`, `age_seconds`, `source` (approval|blocked|review).
3. Inbox-Aufruf erzeugt **keine** Events (reine Leseprüfung: Event-Count vor/nach identisch).

---

## 7. Stufe 5 — Reviewer/Watchdog (P0 Regeln, P1 Automation)

### Reviewer (bestehender Flow, additiv)

Neue Spalten (additiv):

```sql
-- tasks (add_column_if_missing):
review_rounds            INTEGER NOT NULL DEFAULT 0,  -- Review-Zyklen mit unverändertem Subjekt
review_subject_fingerprint TEXT                       -- letzter reviewed Fingerprint
-- task_runs (add_column_if_missing):
outcome_fingerprint      TEXT                           -- sha256({run_id, outcome, summary_sha, artifacts_shas, commit_sha})
```

- **Outcome-Fingerprint** wird vom Kernel bei jeder Review-Entscheidung (approve/request_changes/reopen) aus dem Ist-Zustand berechnet (Serverseite, wie Approvals). Exactly-once: eine Entscheidung ist an `(run_id, outcome_fingerprint)` gebunden; dieselbe Entscheidung nochmals → idempotentes No-op mit Bestätigung; anderes Fingerprint (neuer Run, geänderte Artifacts, Commit-Drift) → vorherige Freigabe wird als `review_drift`-Event markiert und die Review kehrt auf `review` zurück (Reopen), statt stillschweigend zu gelten.
- **Maximal drei unveränderte Runden:** Zähler `review_rounds` erhöht sich je `request_changes`/`reopen_review` mit **gleichem** `review_subject_fingerprint`; jede Subjektänderung (neuer Fingerprint) setzt auf 0. Bei `review_rounds >= 3` (Konstante `REVIEW_ROUND_LIMIT = 3`, analog `BLOCK_RECURRENCE_LIMIT`) routet der Dispatcher die Karte nach `triage` mit Event `review_round_limit` — genau wie das bestehende Recurrence-Routing; **nie** Auto-Approve, **nie** Auto-Complete.
- Reviewer-Unabhängigkeit: bleibt über `review_dispatch` + erzwungenes `sdlc-review` erhalten (`kanban_db_dispatch.py:1785-1796, 2194-2196`); ein Reviewer-Worker repariert nicht (Skill-Semantik; Vorgabe bleibt „unabhängiger, nicht reparierender Prüfer“ — wird als Skill-/Prompt-Anforderung in `sdlc-review` dokumentiert, kein Codezwang nötig, aber der Kernel verweigert `kanban_complete` durch den Reviewer-Run auf fremde Tasks weiterhin über die bestehenden Claim-Regeln).

### Watchdog (Erweiterung der Diagnostics-Engine)

- Der Watchdog ist **kein** neuer Prozess: er ist (a) der Dashboard-/CLI-Diagnostics-Aufruf (on demand) und (b) optional ein Dispatcher-Tick-Phase-Add-on (`kanban.watchdog.tick_enabled`, Default false), das die bestehende `compute_task_diagnostics` über alle aktiven Tasks laufen lässt und `watchdog`-Events + Inbox-Zeilen erzeugt.
- **Invariant: Der Watchdog repariert nichts.** Er liest und emittiert (Registrierungs-Vertrag von `kanban_diagnostics.py`: „stateless and read-only … deliberately mutates nothing“). Die einzige „Aktion“ sind Events/Diagnostics mit Handlungsempfehlungen (`DiagnosticAction`-Typen `comment`/`cli_hint` — keine `reclaim`-Ausführung).
- Neue Regeln: `review_round_limit` (Triage-Routing-Signal), `approval_pending_stale` (Stufe 4), `budget_warn_surface` (Stufe 1), `progress_stalled` (Stufe 7), `cost_unknown_share` (P1).

### Migration

Zwei additive Spalten + Konstante + Tick-Config; Fingerprint-Backfill für offene Reviews: beim ersten Review-Kontakt berechnet, NULL davor ist legitim („nicht ermittelt“).

### Tests

- (a) 3× request_changes ohne Subjektänderung → Task in `triage`, ein `review_round_limit`-Event; (b) Subjektänderung zwischen Runden → Zähler-Reset; (c) identische Doppel-Entscheidung → idempotent, genau ein Event; (d) Drift (Artifact-Änderung) nach Approve → `review_drift` + Status zurück auf `review`; (e) Watchdog-Tick erzeugt Events, aber keine Task-Mutationen (Statusdiff über alle Tasks leer); (f) completion_contract-PR-Task: veröffentlichtes PR bleibt Gate — Fingerprint berücksichtigt `metadata.published_pr`, `kanban_request_changes` unverändert funktionsfähig.

### Binäre Akzeptanzkriterien

1. Nach drei unveränderten Review-Runden steht der Task in `triage` (nicht `review`, nicht `done`).
2. `kanban_request_changes` mit unverändertem Subjekt erhöht `review_rounds` um exakt 1; mit geändertem Subjekt setzt es auf 0 (Assert-Paar).
3. Watchdog-Lauf: `SELECT COUNT(*) FROM tasks WHERE updated_status != frozen_snapshot` = 0 und ≥1 `watchdog`-Event.

---

## 8. Stufe 6 — Routines/Workflow-Templates (P1)

### Routines: Cron→Kanban ohne zweite Scheduler-Engine

- **Kein neuer Scheduler.** Die Cron-Engine (`cron/jobs.py`, `scheduler.py`) bleibt einziger Taktgeber; ihre Occurrence-Accounting-Ledger mit `scheduled_instant` garantiert at-most-once je Slot (`cron/AGENTS.md`, #107485).
- Neues additives Job-Feld `kanban` (cron-Job-Store):

```yaml
kanban:
  board: <slug>
  title: "Weekly maintenance: {date}"
  body_file: /path/to/spec.md      # oder body_inline
  assignee: worker-maintain
  priority: 100
  idempotency_key: "routine:{job_id}:{scheduled_instant}"
  workspace: {kind: dir, path: …}   # optional
```

- Brückenmechanismus: Der Cron-Tick ersetzt/ergänzt den Agent-Lauf für solche Jobs durch einen **normalen `create_task`-Aufruf** über den bestehenden Kernel; der Idempotency-Key wird deterministisch aus `(job_id, scheduled_instant)` gebildet (stabil über Retries/Crash-Wiederholungen, denn der Occurrence-Ledger verhindert Doppel-Feuern und der Key dedupliziert zusätzlich über `kanban_db.py:1260-1329`). `scheduled_instant` wird im Task-Event protokolliert (Provenienz).
- Migration: nur das neue Job-Feld (additiv, ältere Jobs ignorieren es); kein Schema-Wandel in kanban.db.

### Workflow-Templates: dormant Spalten aktivieren

```sql
CREATE TABLE IF NOT EXISTS kanban_workflow_templates (
    board       TEXT NOT NULL,
    id          TEXT NOT NULL,      -- 'wf_<hex>'
    name        TEXT NOT NULL,
    steps       TEXT NOT NULL,     -- JSON: [{step_key, title, assignee_hint, review: bool, approval_type: str|null}]
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (board, id)
);
```

- Die dormanten `tasks.workflow_template_id`/`current_step_key` (`kanban_db.py:918-922`) werden aktiviert: Completing a Task mit Template erzeugt (in derselben Completion-Transaktion) die Folge-Karte des nächsten `step_key` als **normale** Child-Task mit `task_links`-Kante + Parent-Gating — keine neue Routing-Engine, nur ein weiterer Konsument des bestehenden Promote-Gates. `step_key` auf `task_runs` wird dabei befüllt (bislang nullable/unbenutzt, `kanban_db.py:1005-1007`).
- **Fünf Templates (Referenz-Set, Auslieferung als Daten, nicht Code):**
  1. `research-spec-implement`: research → spec → implement (review nach implement).
  2. `implement-review`: implement → review (Review-Spalte, `sdlc-review`).
  3. `routine-maintenance`: cron-erzeugter Check → fix (falls Diagnose) → verify; Idempotency-Key aus der Routine.
  4. `release-gate`: prepare → review → approval(`release`) → publish; gekoppelt an completion_contract.
  5. `incident-reproduce-fix-verify`: reproduce → fix → verify (verify mit eigenem Assignee).
- Explizit **kein** DSL, keine Parallelen Verzweigungen, keine bedingten Sprünge — lineare Schrittlisten, das Parent-Gate macht die Reihenfolge.

### Sicherheitsinvarianten

- Cron-Job mit `kanban`-Block erzeugt nur Tasks — kein Spawn, keine direkte Ausführung; der Worker-Pfad bleibt identisch.
- Templates sind Daten; Zyklen/ Selbstreferenz werden bei Validierung abgelehnt (Kernel prüft `steps`-Kette).
- Ein Templateschritt mit `approval_type` erzeugt eine Approval-Anfrage (Stufe 2) statt selbst zu genehmigen.

### Tests

- (a) Zwei identische Occurrence-Feuers (simulierter Crash-Nachlauf) → genau eine Task (Idempotency-Key); (b) Completion Schritt k erzeugt Karte k+1 mit korrekter Parent-Kante und `current_step_key`; (c) altes Job-JSON ohne `kanban`-Block läuft unverändert; (d) zyklische Template-Definition abgelehnt; (e) Template-Task-Completion schwächt keine completion_contract-/Review-Regeln (gleiche Gates).

### Binäre Akzeptanzkriterien

1. N Occurrences eines Routine-Jobs erzeugen exakt N Tasks mit paarweise verschiedenen Idempotency-Keys (Count-Assert), ein Retry erzeugt keinen N+1-ten.
2. Ein Task mit 3-Schritt-Template durchläuft completion-gesteuert alle `step_key`s; am Ende existieren 3 verlinkte Karten, alle Gates (review/approval) wurden durchlaufen.
3. Dispatcher-Code enthält keine zweite Scheduler-Loop (Review-Gate: Diff enthält keine neue Takt-Schleife — grep nach `while True.*sleep` im Delta leer).

---

## 9. Stufe 7 — Strukturierte Progress/ETA-Heartbeats und Dashboardprojektion (P0)

### Datenmodell

```sql
-- task_runs (add_column_if_missing):
progress_pct        INTEGER,   -- 0..100, NULL = nicht angegeben
eta_seconds         INTEGER,   -- >=0, NULL = unbekannt
phase               TEXT,      -- freie Kurzbezeichnung (z. B. 'encoding', 'verify')
progress_updated_at INTEGER
-- tasks (denormalisiert, analog current_run_id):
progress_pct        INTEGER
eta_seconds         INTEGER
```

- `kanban_heartbeat`-Tool erhält additive Parameter `progress_pct`, `eta_seconds`, `phase` (bestehender `note` bleibt; Auto-Heartbeats setzen keine strukturierten Felder). Validierung: `0<=pct<=100`, `eta>=0` — Verstoß → Parameter wird ignoriert + Hinweis (best-effort, never raise — bestehendes Muster `tools/kanban_tools.py:497+`).
- Events: strukturierte Heartbeats protokollieren `{"progress_pct":…, "eta_seconds":…, "phase":…}` im bestehenden `heartbeat`-Event — vollständige Audit-Spur ohne neue Event-Tabelle.
- Nicht-monotone Progress-Werte sind erlaubt (Schätzungen dürfen sich verschlechtern), aber jede Änderung ist im Event-Log sichtbar (ehrlich statt geglättet).

### Dashboardprojektion

- TUI-Kontrakt: `KanbanActivityRun`/`KanbanActivityTask` (`tui_gateway/contracts/kanban_activity.py`) erhalten additive Felder `progress_pct`, `eta_seconds`, `phase` — Regenerierung via `scripts/gen_gateway_contracts.py`, `test_generated.py` schlägt bei Stale-Vertrag an (bestehender Schutz).
- Dashboard-Plugin (`plugins/kanban/dashboard/plugin_api.py`): Task-/Board-Endpunkte führen die Felder mit; TUI/Dashboard rendern Fortschrittsbalken + ETA-Spalte.
- Neue Diagnostics-Regel `progress_stalled`: `last_heartbeat_at` frisch, aber `progress_updated_at` älter als `kanban.diagnostics.progress_stall_hours` (Default 1) bei `progress_pct < 100` → warning (Arbeit sichtbar, aber kein messbarer Fortschritt — passt zur Langläufer-Regel: Fortschritt muss beobachtbar sein).

### Migration

Additive Spalten + Tool-Parameter + Vertragsfelder; Config `kanban.progress.enabled` (Default true für Felder, die nur Daten tragen; die *Regel* hängt an `kanban.diagnostics.*`).

### Sicherheitsinvarianten

- Heartbeat-Semantik (Claim-Verlängerung, `expected_run_id`-Fence) unverändert; strukturierte Felder dürfen die Claim-Logik nicht umgehen.
- Kein Auto-Abort wegen ETA-Überschreitung — `max_runtime_seconds` bleibt das einzige harte Laufzeit-Gate (ETA ist Information, kein Kill-Kriterium).

### Tests

- (a) Heartbeat mit `progress_pct=42, eta_seconds=300` → Spalten gesetzt + Event-Payload korrekt; (b) `progress_pct=150` → Felder NULL, Lauf ungestört; (c) Auto-Heartbeat (Worker tritt nicht auf) setzt keine Progress-Felder; (d) TUI-Vertrag generiert und `tsc`/Vertraktstest grün; (e) `progress_stalled` feuert nach Schwellenüberschreitung und clear-t bei Fortschritt.

### Binäre Akzeptanzkriterien

1. Nach einem strukturierten Heartbeat liefert `hermes kanban show <id> --json` `progress_pct`/`eta_seconds` identisch zum Tool-Call.
2. Ungültige Werte verändern keine Spalte und brechen den Worker nicht (rc des Workers = 0 nach Hinweis).
3. Generierter Gateway-Vertrag enthält die drei neuen Felder (`git diff` in `apps/shared/src/gateway-contract.generated.ts` nur additiv; Vertraktstest grün).

---

## 10. Stufe 8 — Explizit außerhalb P0/P1 (Out of Scope)

- **RBAC/Rollenmodell:** kein Berechtigungssystem, keine User-Verwaltung, keine pro-User-Permissions. Die heutige Isolation (Board hart, Claim-Allowlist je Home, Profil-Inseln) ist die P0-Sicherheitsgrenze. Approvals decken Einzelfall-Entscheidungen ab, keine Rollenhierarchie.
- **Mobile Clients:** keine neue Oberfläche; alle Flächen sind CLI/Dashboard/TUI/Gateway-Chat.
- **Harte Multi-Tenant-Isolation:** Tenant bleibt weicher Namespace (Ist-Zustand, `cron/AGENTS.md`); Tenant-Budgets sind Reporting-/Stop-Scopes, keine Isolationsverstärkung.
- **Zweite Goal-/Decomposition-Engine** auf Project-Ebene; **Workflow-DSL** mit Parallelen/Bedingungen; **zweite Scheduler-Engine**; **Auto-Approve/Auto-Unblock** menschlicher Gates; **agentische Selbst-Governance** (Worker, die eigene Limits/Approvals ändern).
- Migration dieser Punkte erst nach Betriebserfahrung mit P0/P1; jede Erweiterung durchläuft erneut die Footprint-Ladder.

---

## 11. Migrationsplan

**Reihenfolge (jede Phase eigenständig rückrollbar, Flags je Stufe):**

| Phase | Inhalt | Voraussetzung |
|---|---|---|
| M0 | Additive DDL: alle `CREATE TABLE IF NOT EXISTS` + `add_column_if_missing` + Indizes; Ledger-Backfill `unknown` | nichts (reines Öffnen-und-Anlegen; alter Dispatcher ignoriert alles) |
| M1 | Stufe 1 Ledger-Schreibpfad (Worker-Env `HERMES_KANBAN_RUN_ID`, Flush bei Lauf-Ende), Flags off | M0 |
| M2 | Stufe 1 Budget-Gate im Dispatcher-Tick + CLI `budget` | M1, Flag `kanban.budgets.enabled` |
| M3 | Stufe 7 strukturierte Heartbeats (Spalten, Tool-Parameter, TUI-Vertrag, Dashboard) | M0 (unabhängig von M1/M2) |
| M4 | Stufe 2 Approvals + Stufe 4 Inbox (Projektion + neue Diagnostics-Regeln) | M0; Approval-basierte Budget-Änderungen brauchen M2 |
| M5 | Stufe 3 Project-Goals + Rollup | M2 (Budget-Kopplung) |
| M6 | Stufe 5 Review-Runden/Fingerprint + Watchdog-Tick-Option | M0 |
| M7 (P1) | Stufe 6 Routines (Cron-Feld) + Workflow-Templates | M0; `routine-maintenance`-Template profitiert von M2/M4 |

**Kompatibilitätsgarantien:**

- Vor-M0-Board-DBs öffnen nach M0 ohne Migrationsschritt und ohne Datenverlust; `hermes kanban list/show/create/complete/block` verhalten sich identisch (nur zusätzliche NULL-Spalten).
- Alle Flags Default off → Dispatcher-Tick führt keine Governance-Queries aus (Performance-Neutralität im Aus-Zustand; Größenordnung des Ticks bleibt ~µs, vergleiche `dispatch_in_gateway`-Begründung in `config_defaults.py`).
- Ein Rückrollen einer Phase = Flag off + Tabelle/Spalten bleiben (additiv unschädlich).
- Kein `_config_version`-Bump: ausschließlich neue Keys unter `kanban.*` (Deep-Merge-Regel, `hermes_cli/AGENTS.md`).

**Nicht-geschwächte Semantiken (Prüfliste je Review):** completion_contract/PR-Gate (`kanban_pr_acceptance.py:22`), Claim-Fence PID+Fingerprint (`kanban_db_dispatch.py:361-406`), Review-Exemptions (`kanban_db.py:2877`), Parent-Gating & Promote, Sticky-Block/Recurrence→triage, Board-/Tenant-Isolation, Claim-Allowlist je Home, Artifact-Semantik (Pfad/Hash/Owner), Notify-Subs-Profil-Scope.

---

## 12. Globale Sicherheitsinvarianten

1. **Keine Selbst-Governance:** Worker-Toolsets können lesen (Budget-Status, eigene Approvals anfragen, Progress melden), aber nie Limits setzen, Approvals entscheiden oder Tasks unblocken, die menschliche Gates halten.
2. **Fail-closed bei Drift:** Fingerprint-Abweichung (Approval-Subjekt, Review-Outcome) → invalidiert/Reopen, nie still weitergelten.
3. **Ehrlichkeit:** unknown bleibt unknown (Kosten, ETA, Fingerprint-Backfill); NULL ist der Wert, nicht 0.
4. **Exactly-once:** Budget-Stop-/Warn-Events, Review-Entscheidungen und Routine-Erzeugungen sind idempotent (Event-Dedup/PRIMARY KEY/Idempotency-Key).
5. **Watchdog repariert nie:** Diagnostics/Watchdog sind read-only; das einzige Routing (Review-Runden-Limit → triage) folgt dem bestehenden Dispatcher-Breaker-Muster und ist kein Eingriff in Laufzustände.
6. **Menschliche Gates bleiben menschlich:** `needs_input`/`capability` werden nur durch explizite menschliche Entscheidung (unblock/approve) geöffnet.
7. **Profil-Scope:** alle neuen Pfade binden Scope wie bestehende (Ticker/Notify/Teardown-Muster, `cron/AGENTS.md`); keine `os.environ`-Identität, keine `~/.hermes`-Literale.

---

## 13. Test-Strategie & globale Akzeptanzkriterien

**Lokale Tests** (pro Stufe, Placement `tests/hermes_cli/test_kanban_*.py` bzw. `tests/tools/`, `tests/plugins/`, `tests/tui_gateway/contracts/`): E2E mit echten Imports, Temp-`HERMES_HOME`, echter Board-DB über `scripts/run_tests.sh`; Verhaltenstests/Beziehungstests, keine Change-Detektoren, keine Verb-/Toolset-Zähler-Asserts (Root-Regeln).

**Globale binäre Akzeptanzkriterien des Gesamtvorhabens:**

1. **Kompat:** Eine vor der Migration erzeugte Board-DB läuft nach der Migration durch den neuen Dispatcher: `hermes kanban list`, `create`, `complete`, `block`, `unblock` ohne Fehler, alter Dispatcher (alter Code) öffnet dieselbe DB ebenfalls (nur additive Spalten).
2. **Flags-off-Neutralität:** Mit allen Governance-Flags aus ist das Dispatcher-Tick-Verhalten (Claims, Spawns, Events) vom Ist-Zustand nicht unterscheidbar (Event-Diff über einen Modelltick leer).
3. **Unknown-Integrität:** `SELECT COUNT(*) FROM task_run_costs WHERE cost_status='unknown' AND (estimated_cost_usd IS NOT NULL OR actual_cost_usd IS NOT NULL)` = 0 und keine unknown-Zeile mit Wert 0.
4. **Keine Gate-Schwächung:** Die Testsuiten für completion_contract (`tests/hermes_cli/test_kanban_pr_acceptance.py`), Claim-Reclaim (`test_kanban_reclaim_claim_lock_guard.py`), Block-Kinds (`test_kanban_block_kinds.py`), Sticky-Block (`test_kanban_blocked_sticky.py`) und Worktree-Isolation laufen unverändert grün.
5. **Review-Runden-Hartes-Limit:** Drei unveränderte Runden → triage (Automattest), Auto-Approve existiert nicht (negativer Registry-Test).
6. **Routinen-Exactly-once:** Retried Occurrence erzeugt keinen zweiten Task (Count-Assert).
7. **Vertragsstabilität:** `scripts/gen_gateway_contracts.py` diff nur additiv; `tests/tui_gateway/contracts/test_generated.py` grün.

---

## 14. Quellen (alle im Repo, Base `bb0785e73b`)

- `hermes_cli/kanban_db.py` — Schema (L874-1076), `VALID_BLOCK_KINDS` (L107), `BLOCK_RECURRENCE_LIMIT` (L111), idempotentes Create (L1260-1329), `claim_review_task` (L2304-2330), Completion/Review-Exemptions (L2737-2877), dormant Workflow-Spalten (L918-922, L1005-1007)
- `hermes_cli/kanban_db_dispatch.py` — Worker-Fingerprint/Claim-Fence (L361-406), `heartbeat_worker` (L610-645), `_error_fingerprint` (L911), `review_dispatch`/`sdlc-review` (L1785-1796, L2194-2196), `_tick_spawn_budget` (L2282)
- `hermes_cli/kanban_db_connect.py` — additive Spaltenmigration (L797, L820, L833)
- `hermes_cli/kanban_diagnostics.py` — Severity/Regeln/read-only-Vertrag (L20, L594-620, L682-736, L805-834)
- `hermes_cli/kanban_pr_acceptance.py` — completion_contract-Validierung (L22)
- `hermes_state_usage.py` / `hermes_state_schema.py` — `session_model_usage`, Kostenfelder/Semantik (usage L29-79; schema L70)
- `hermes_cli/model_cost_guard.py` — Auswahl-Warnung (kein Kanban-Budget)
- `hermes_cli/approval_mode.py` — Tool-Approval-Modes (andere Domäne)
- `hermes_cli/projects_db.py` — Projects-Schema, additive Öffnungs-Garantie
- `hermes_cli/goal_command.py` — `/goal`-Session-Semantik (nicht Project-Governance)
- `tools/kanban_tools.py` — Toolset/Heartbeat-Tool (L497-555, L1180)
- `tui_gateway/contracts/kanban_activity.py` — TUI-Aktivitätsprojektion
- `plugins/kanban/dashboard/plugin_api.py` — Dashboard-Endpunkte (L287, L352)
- `hermes_cli/config_defaults.py` — `kanban.*`-Defaults (L1861+)
- `cron/AGENTS.md` — Occurrence-Ledger/at-most-once (#107485), Kanban-Isolation, Ticker-Scope
- `website/docs/user-guide/features/kanban.md` — Idempotent Create, PR-Completion-Contracts, Dispatcher-Betrieb
- Root `AGENTS.md`, `tools/AGENTS.md`, `plugins/AGENTS.md`, `tui_gateway/AGENTS.md`, `hermes_cli/AGENTS.md` — Footprint-Ladder, Cache-/Scope-Invarianten

*Kein Modellwissen für Preise/Rechtslagen eingeflossen; diese Spezifikation enthält keine Preisdaten, nur Schemata und Feldsemantik.*
