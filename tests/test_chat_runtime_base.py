import subprocess
import sys

from tandem.chat.events import ApprovalRequest, TurnOutcome
from tandem.chat.runtime import child_env, first_line, summarize_args, terminate


def test_child_env_strips_claude_markers():
    env = child_env({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


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
