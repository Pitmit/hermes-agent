"""``hermes cron`` subcommand parser."""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_accept_hooks_flag


def _flag(parser, *names, help, **kw):
    parser.add_argument(*names, action="store_true", help=help, **kw)


def build_cron_parser(subparsers, *, cmd_cron: Callable) -> None:
    """Attach the ``cron`` subcommand (and its sub-actions) to ``subparsers``."""
    cron_parser = subparsers.add_parser(
        "cron", help="Cron job management", description="Manage scheduled tasks")
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command")

    cron_list = cron_subparsers.add_parser("list", help="List scheduled jobs")
    _flag(cron_list, "--all", help="Include disabled and completed jobs")

    cron_create = cron_subparsers.add_parser(
        "create", aliases=["add"], help="Create a scheduled job")
    cron_create.add_argument("schedule", help="Schedule like '30m', 'every 2h', or '0 9 * * *'")
    cron_create.add_argument(
        "prompt", nargs="?", help="Optional self-contained prompt or task instruction")
    cron_create.add_argument("--name", help="Optional human-friendly job name")
    cron_create.add_argument("--deliver",
        help="Delivery target: origin, local, telegram, discord, signal, "
            "platform:chat_id, or bot-chat[:profile] (inject output into a "
            "local profile's canonical Bot Chat as a message the bot responds to)")
    cron_create.add_argument("--failure-deliver", dest="failure_deliver",
        help="Override target for FAILURE notices only (same grammar as "
            "--deliver). 'local' suppresses failure notices entirely; run "
            "state stays visible in `hermes cron list`. Omit = failures "
            "follow --deliver.")
    cron_create.add_argument("--repeat", type=int, help="Optional repeat count")
    cron_create.add_argument("--skill", dest="skills", action="append",
        help="Attach a skill. Repeat to add multiple skills.")
    cron_create.add_argument("--script",
        help="Path to a script under ~/.hermes/scripts/. Default mode: "
            "script stdout is injected into the agent's prompt each run. "
            "With --no-agent: the script IS the job and its stdout is "
            "delivered verbatim. .sh/.bash files run via bash, everything "
            "else via Python.")
    _flag(cron_create, "--no-agent", dest="no_agent", default=False,
        help="Skip the LLM entirely — run --script on schedule and deliver "
            "its stdout directly. Empty stdout = silent. Classic watchdog "
            "pattern (memory alerts, disk alerts, CI pings).")
    cron_create.add_argument("--monitor-script", dest="monitor_script",
        help="Monitor mode: path to a cheap source script under "
            "~/.hermes/scripts/ that runs each tick BEFORE the agent. "
            "Unchanged output (exact-bytes hash) suppresses the agent run "
            "entirely; changed output injects a MONITOR CHANGE DETECTED "
            "diff into the prompt. Script output must be stable (no "
            "timestamps). Mutually exclusive with --monitor-url; "
            "incompatible with --no-agent.")
    cron_create.add_argument("--monitor-url", dest="monitor_url",
        help="Monitor mode: http(s) URL fetched with a bounded GET each tick "
            "instead of a script. Same hash-suppression semantics as "
            "--monitor-script.")
    cron_create.add_argument("--workdir",
        help="Absolute path for the job to run from. Injects AGENTS.md / CLAUDE.md / .cursorrules from that directory and uses it as the cwd for terminal/file/code_exec tools. Omit to preserve old behaviour (no project context files).",
    )
    cron_create.add_argument("--model",
        help="Pin this job to a specific inference model (user-owned; the "
            "agent's cronjob tool cannot set this). Omit to follow "
            "cron.model, then the main agent model (`hermes model`), at fire time.")
    cron_create.add_argument("--pin", dest="pinned", action="store_true", default=None,
        help="Lock the CURRENT main agent model (and its provider) onto this job so later "
            "`hermes model` changes never touch it. Ignored when --model is given.")
    cron_create.add_argument("--provider", dest="model_provider",
        help="Inference provider paired with --model (e.g. 'openrouter', 'nous').")
    cron_create.add_argument("--reasoning-effort", dest="reasoning_effort",
        help="Pin this job's reasoning (thinking) effort: none, minimal, low, "
            "medium, high, xhigh, max, or ultra. Overrides agent.reasoning_effort "
            "and agent.reasoning_overrides for this job; unsupported levels are "
            "clamped by the provider at request time. Omit to follow config.")
    cron_create.add_argument("--interpreter",
        help="Absolute or ~ path to a Python in your own venv (e.g. ~/venvs/report/bin/python) "
            "for a .py --script / --monitor-script, so it can import packages Hermes does not "
            "ship. .sh/.bash still run under bash. Omit to use Hermes' Python.")
    cron_create.add_argument(
        "--continuity", dest="continuity", action="store_const", const=True, default=None,
        help="Each run wakes up with the job's own previous output injected "
            "into its prompt, so it can dedupe against what was already "
            "reported and continue where the last run left off (scouts, "
            "monitors, incremental digests). First run is unchanged.")

    _flag(cron_create, "--paused", default=False,
        help="Create disabled in one write; resume to schedule, or explicitly run now.")
    cron_create.add_argument("--paused-reason", help="Auditable reason; requires --paused.")

    # Kanban routine (governance stage 6): any --kanban-* flag turns the job
    # into a deterministic routine — each occurrence creates ONE task on the
    # given board instead of running an agent.
    cron_create.add_argument("--kanban-board",
        help="Kanban board slug the recurring task is created on (enables routine mode)")
    cron_create.add_argument("--kanban-title",
        help="Recurring task title ('{date}' is replaced with the occurrence's UTC date)")
    cron_create.add_argument("--kanban-body-inline",
        help="Task body text (mutually exclusive with --kanban-body-file)")
    cron_create.add_argument("--kanban-body-file",
        help="Absolute path to a file whose content becomes the task body (read at fire time)")
    cron_create.add_argument("--kanban-assignee", help="Worker profile the task is assigned to")
    cron_create.add_argument("--kanban-priority", type=int, help="Task priority (default 0)")
    cron_create.add_argument("--kanban-catch-up", choices=["skip", "once", "all-bounded"],
        help="Missed-occurrence policy: skip = create nothing for a missed slot; "
             "once = one catch-up card (default); all-bounded = one card per missed "
             "slot, bounded by --kanban-catch-up-bound")
    cron_create.add_argument("--kanban-catch-up-bound", type=int,
        help="Max cards per catch-up fire for all-bounded (1-20, default 5)")
    cron_create.add_argument("--kanban-workspace-kind", choices=["scratch", "dir", "worktree"],
        help="Workspace kind for the created task (default scratch)")
    cron_create.add_argument("--kanban-workspace-path",
        help="Workspace path (requires kind dir or worktree)")
    cron_create.add_argument("--kanban-idempotency-key",
        help="Custom key template; must contain {scheduled_instant} (default "
             "'routine:{job_id}:{scheduled_instant}')")

    cron_edit = cron_subparsers.add_parser("edit", help="Edit an existing scheduled job")
    cron_edit.add_argument("job_id", help="Job ID to edit")
    cron_edit.add_argument("--schedule", help="New schedule")
    cron_edit.add_argument("--prompt", help="New prompt/task instruction")
    cron_edit.add_argument("--name", help="New job name")
    cron_edit.add_argument("--deliver", help="New delivery target")
    cron_edit.add_argument("--failure-deliver", dest="failure_deliver",
        help="Override target for failure notices (same grammar as --deliver; "
            "'local' suppresses; '' clears the override)")
    cron_edit.add_argument("--repeat", type=int, help="New repeat count")
    cron_edit.add_argument("--skill", dest="skills", action="append",
        help="Replace the job's skills with this set. Repeat to attach multiple skills.")
    cron_edit.add_argument("--add-skill", dest="add_skills", action="append",
        help="Append a skill without replacing the existing list. Repeatable.")
    cron_edit.add_argument("--remove-skill", dest="remove_skills", action="append",
        help="Remove a specific attached skill. Repeatable.")
    _flag(cron_edit, "--clear-skills", help="Remove all attached skills from the job")
    cron_edit.add_argument("--script",
        help="Path to a script under ~/.hermes/scripts/. Pass empty string to clear. "
            "With --no-agent the script IS the job; otherwise its stdout is "
            "injected into the agent's prompt each run.")
    cron_edit.add_argument(
        "--no-agent", dest="no_agent", action="store_const", const=True, default=None,
        help="Enable no-agent mode on this job (requires --script or an "
            "existing script on the job).")
    cron_edit.add_argument("--agent", dest="no_agent", action="store_const", const=False,
        help="Disable no-agent mode on this job (reverts to LLM-driven execution).")
    cron_edit.add_argument(
        "--continuity", dest="continuity", action="store_const", const=True, default=None,
        help="Turn on run-to-run continuity: each run sees the job's own "
            "previous output (dedupe, continue where it left off).")
    cron_edit.add_argument("--no-continuity", dest="continuity", action="store_const", const=False,
        help=("Turn off run-to-run continuity (other context_from job refs are preserved)."))
    cron_edit.add_argument("--monitor-script", dest="monitor_script",
        help="Set/replace the monitor source script (see `hermes cron create "
            "--monitor-script`). Pass empty string to clear.")
    cron_edit.add_argument("--monitor-url", dest="monitor_url",
        help=("Set/replace the monitor source URL. Pass empty string to clear."))
    cron_edit.add_argument("--workdir",
        help="Absolute path for the job to run from (injects AGENTS.md etc. and sets terminal cwd). Pass empty string to clear.",
    )
    cron_edit.add_argument("--model",
        help="Pin this job to a specific inference model (user-owned; the "
            "agent's cronjob tool cannot set this). Pass empty string to "
            "clear the pin and follow cron.model, then the main agent model.")
    _pin = cron_edit.add_mutually_exclusive_group()
    _pin.add_argument("--pin", dest="pinned", action="store_true", default=None,
        help="Lock the CURRENT main agent model (and its provider) onto this job.")
    _pin.add_argument("--unpin", dest="pinned", action="store_false",
        help="Release the job's model pin so it follows the main agent model again.")
    cron_edit.add_argument("--provider", dest="model_provider",
        help="Inference provider paired with --model. Pass empty string to clear.")
    cron_edit.add_argument("--reasoning-effort", dest="reasoning_effort",
        help="Pin this job's reasoning (thinking) effort: none, minimal, low, "
            "medium, high, xhigh, max, or ultra. Pass empty string to clear "
            "the pin and follow config resolution.")
    cron_edit.add_argument("--interpreter",
        help="Absolute or ~ path to a Python for a .py script / monitor script. "
            "Pass empty string to clear (back to Hermes' Python).")

    # Kanban routine editing: --kanban-* merges over the stored block;
    # --kanban-clear removes it (the job reverts to its own payload).
    cron_edit.add_argument("--kanban-board", help="Set the routine's board slug")
    cron_edit.add_argument("--kanban-title", help="Set the recurring task title")
    cron_edit.add_argument("--kanban-body-inline",
        help="Set the task body text (mutually exclusive with --kanban-body-file)")
    cron_edit.add_argument("--kanban-body-file",
        help="Set the body-file path (absolute; read at fire time)")
    cron_edit.add_argument("--kanban-assignee", help="Set the task's assignee")
    cron_edit.add_argument("--kanban-priority", type=int, help="Set the task priority")
    cron_edit.add_argument("--kanban-catch-up", choices=["skip", "once", "all-bounded"],
        help="Set the missed-occurrence policy (see cron create --kanban-catch-up)")
    cron_edit.add_argument("--kanban-catch-up-bound", type=int,
        help="Set the all-bounded card bound (1-20)")
    cron_edit.add_argument("--kanban-workspace-kind", choices=["scratch", "dir", "worktree"],
        help="Set the workspace kind for created tasks")
    cron_edit.add_argument("--kanban-workspace-path", help="Set the workspace path")
    cron_edit.add_argument("--kanban-idempotency-key",
        help="Set a custom key template (must contain {scheduled_instant})")
    _flag(cron_edit, "--kanban-clear",
        help="Remove the kanban routine block from this job")

    # lifecycle actions
    cron_pause = cron_subparsers.add_parser("pause", help="Pause a scheduled job")
    cron_pause.add_argument("job_id", help="Job ID to pause")

    cron_resume = cron_subparsers.add_parser("resume", help="Resume a paused job")
    cron_resume.add_argument("job_id", help="Job ID to resume")
    cron_resume.add_argument("--at", dest="run_at", help="Re-arm at an ISO-8601 time")
    _flag(cron_resume, "--run-now", help="Re-arm to run now")

    cron_run = cron_subparsers.add_parser("run", help="Run a job on the next scheduler tick")
    cron_run.add_argument("job_id", help="Job ID to trigger")
    add_accept_hooks_flag(cron_run)

    cron_remove = cron_subparsers.add_parser(
        "remove", aliases=["rm", "delete"], help="Remove a scheduled job")
    cron_remove.add_argument("job_id", help="Job ID to remove")

    # cron status
    cron_subparsers.add_parser("status", help="Check if cron scheduler is running")

    cron_runs = cron_subparsers.add_parser(
        "runs", aliases=["history"], help="Show durable execution attempts")
    cron_runs.add_argument("job_id", nargs="?", help="Optional job ID filter")
    cron_runs.add_argument("--limit", type=int, default=20, help="Rows to show (1-500)")

    # cron incidents — durable failure incidents (list/ack)
    cron_incidents = cron_subparsers.add_parser(
        "incidents", help="List or acknowledge durable cron failure incidents")
    cron_incidents.add_argument("--state", choices=["detected", "alerted", "resolved", "closed"],
        help="Filter incidents by lifecycle state")
    cron_incidents.add_argument(
        "incident_action", nargs="?", default="list", choices=["list", "ack"],
        help="Action (default: list)")
    cron_incidents.add_argument("incident_id", nargs="?", help="Incident ID to acknowledge (ack)")

    # notepad: per-job durable KV, injected into the job prompt each run.
    cron_notepad = cron_subparsers.add_parser(
        "notepad", help="Read/write a job's durable notepad (persistent KV across runs)")
    cron_notepad.add_argument("job_id", help="Job ID the notepad belongs to")
    cron_notepad.add_argument(
        "notepad_action", nargs="?", default="list", choices=["get", "set", "delete", "list"],
        help="Action (default: list)")
    cron_notepad.add_argument("key", nargs="?", help="Notepad key (get/set/delete)")
    cron_notepad.add_argument("value", nargs="?", help="Value to store (set)")

    cron_subparsers.add_parser("doctor", help="Check scheduled jobs for common health issues")

    cron_tick = cron_subparsers.add_parser("tick", help="Run due jobs once and exit")
    add_accept_hooks_flag(cron_tick)
    add_accept_hooks_flag(cron_parser)
    cron_parser.set_defaults(func=cmd_cron)
