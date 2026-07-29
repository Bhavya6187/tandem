import sys

from tandem.ptyrun import run_in_pty


def test_non_tty_fallback_runs_subprocess(capfd):
    # pytest runs with a non-tty stdin, exercising the subprocess fallback.
    code = run_in_pty([sys.executable, "-c", "print('pty-ok')"])
    assert code == 0
    assert "pty-ok" in capfd.readouterr().out


def test_exit_code_propagates():
    assert run_in_pty([sys.executable, "-c", "raise SystemExit(3)"]) == 3
