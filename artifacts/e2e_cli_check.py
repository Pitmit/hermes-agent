"""One-shot E2E: drive the REAL kanban slash parser (argparse tree + handlers)
for `project goal|rollup|list` against a temp home."""
import os
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kp-e2e-"))
os.environ["HERMES_HOME"] = str(TMP / "hermes-home")
os.environ["HOME"] = str(TMP / "home")
os.environ.pop("HERMES_KANBAN_TASK", None)
os.environ.pop("HERMES_KANBAN_DB", None)
os.environ.pop("HERMES_KANBAN_BOARD", None)
import pathlib
pathlib.Path.home = lambda: TMP

from hermes_cli import kanban_db as kb
from hermes_cli import kanban
from hermes_cli import projects_db as pdb

kb.init_db()
with pdb.connect_closing() as pconn:
    repo = TMP / "repo"
    repo.mkdir()
    pid = pdb.create_project(pconn, name="E2E", primary_path=str(repo))

out = kanban.run_slash("project list")
print("LIST:", out)
out = kanban.run_slash(f"project goal {pid} --text 'ship the rollup' --owner peter --budget 15")
print("GOAL SET:", out)
out = kanban.run_slash(f"project goal {pid}")
print("GOAL SHOW:", out)
out = kanban.run_slash(f"project rollup {pid}")
print("ROLLUP:", out)
out = kanban.run_slash("project rollup p_unknown_e2e")
print("UNKNOWN (expect error):", out[:120])
out = kanban.run_slash("project")
print("NO-ACTION (expect usage):", out[:80])
print("E2E_CLI_DONE")
