import os
import signal
import subprocess
import sys
import time

from tandem.chat.events import ApprovalRequest, TurnOutcome
from tandem.chat.runtime import child_env, first_line, summarize_args, terminate


def test_child_env_strips_only_claudes_own_nesting_markers():
    """The markers claude sets for its children go; the user's own claude
    settings — the store override tandem itself honors, a Bedrock switch —
    reach the child untouched."""
    env = child_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "1",
                     "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PID": "7",
                     "CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_CODE_USE_BEDROCK": "1",
                     "PATH": "/bin", "HOME": "/h"})
    assert env == {"CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_CODE_USE_BEDROCK": "1",
                   "PATH": "/bin", "HOME": "/h"}


def test_child_env_names_the_session_the_child_belongs_to():
    """A directory can hold many chat sessions, so a `tandem sub` or
    `tandem hook-route` the harness spawns cannot find its session by cwd:
    the window's session id rides the child's environment, replacing any
    outer window's."""
    env = child_env({"PATH": "/bin", "TANDEM_SESSION_ID": "outer"}, tandem_id="tdm-inner")
    assert env == {"PATH": "/bin", "TANDEM_SESSION_ID": "tdm-inner"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_terminate_soft_rung_takes_the_tool_children_down():
    """A harness that exits on stdin EOF while a tool command is still running
    leaves that command in the process group tandem created; the ladder owns
    the group, not just its leader."""
    child_src = ("import subprocess, sys\n"
                 "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                 "print(p.pid, flush=True)\n"
                 "sys.stdin.readline()\n")
    proc = subprocess.Popen([sys.executable, "-c", child_src], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    grandchild = int(proc.stdout.readline())
    try:
        assert terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=3.0) == "soft"
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _alive(grandchild):
            time.sleep(0.02)
        assert not _alive(grandchild)
    finally:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_first_line_truncates_with_ellipsis():
    assert first_line("  hello\nworld ") == "hello"
    assert first_line("x" * 100, limit=10) == "x" * 9 + "…"
    assert first_line("") == ""


def test_summarize_args_prefers_the_meaningful_key():
    assert summarize_args("Bash", {"command": "pytest -q", "description": "run tests"}) == "pytest -q"
    assert summarize_args("Edit", {"file_path": "/a/b.py", "old_string": "x"}) == "/a/b.py"
    assert summarize_args("exec", "touch x") == "touch x"
    assert summarize_args("mystery", {"k": 1}) == '{"k": 1}'
    assert summarize_args("none", {}) == ""


def test_terminate_soft_then_term_then_kill():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    assert terminate(proc, soft_timeout=0.2, term_timeout=1.0) in ("term", "kill")
    assert proc.poll() is not None
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    assert terminate(dead) == "dead"


def test_terminate_honors_a_soft_hook():
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.readline()"],
                            stdin=subprocess.PIPE, start_new_session=True)
    assert terminate(proc, soft=lambda: proc.stdin.close(), soft_timeout=3.0) == "soft"


def test_defaults():
    assert ApprovalRequest("command", "rm -rf x").choices == ("allow", "always", "deny")
    assert TurnOutcome("completed").native_id is None
