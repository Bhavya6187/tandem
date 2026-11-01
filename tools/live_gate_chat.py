#!/usr/bin/env python3
# tools/live_gate_chat.py
"""Live gate for `tandem`: a three-harness relay with approvals, driven
in tmux on a private socket. Needs real, signed-in claude and codex; the
opencode step runs only when opencode is one of the window's participants
(the bar names it), so a claude+codex pairing gates the other steps alone.

usage: tools/live_gate_chat.py [--bin /path/to/tandem] [--proj DIR]

Steps (each waits on pane text or on the file a harness was asked to touch):
  1. launch `tandem` in an empty, trusted project directory
  2. `/claude Reply with exactly MARLIN.`                    → pane shows MARLIN
  3. `/codex What word did the previous assistant say? Add OSP+REY. Then run: touch gate-codex.txt`
       → approval prompt → `y` → file exists, and codex names MARLIN (and
         says OSPREY, which no prompt spells out)
  4. `/opencode:opencode/big-pickle Which words so far? Then run: touch gate-oc.txt`
       → file exists, and opencode names MARLIN (permission prompt answered
         if shown — opencode's default config auto-allows bash); skipped
         when opencode is not a participant
  5. `/claude Use the tool named exactly Bash to run: touch gate-claude.txt`
       (the default is codex or opencode by now, so this routes back)
       → approval → `y` → file exists, claude replies `OSPREY DONE` — the
       word only codex said, so codex's turn reached claude; and a claude
       turn after the first, which a transcript claude itself wrote must
       not block
  6. Ctrl-C twice → exits cleanly; `tandem status` then prints the pairing.

Each relay step is its own cross-harness sync check: a harness can only name
the previous harness's word if that turn was translated into its own native
session file before the turn ran.

Two things the pane will not tell you, hence the shapes below. A finished
turn prints no line at all unless the harness reported usage (see
`render.turn_finished`), so turn ends are gated on the touched file, never on
the word "completed". And every earlier turn stays on screen, so a bare
`re.search` is satisfied by the PREVIOUS harness's output the instant it is
called — the content checks count occurrences against a baseline taken just
before the prompt was sent, over the whole scrollback so the count can only
grow.
"""
import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default="tandem")
ap.add_argument("--proj", default=None)
a = ap.parse_args()
PROJ = Path(a.proj or tempfile.mkdtemp(prefix="tandem-chat-gate-"))
PROJ.mkdir(parents=True, exist_ok=True)
subprocess.run(["git", "init", "-q"], cwd=PROJ)
S = "chatgate"
# the row offers only what the harness listed: codex leaves acceptForSession
# out of availableDecisions for a plain command, so no `[a]lways` there
APPROVAL = r"\[y\]es(?: \[a\]lways)? \[n\]o"


def t(*args):
    return subprocess.run(["tmux", "-L", "chatgate", *args], capture_output=True, text=True)


def pane(hist=False):
    return t("capture-pane", "-p", "-t", S, *(["-S", "-"] if hist else [])).stdout


def hits(pattern):
    """Occurrences over the whole scrollback: the visible pane scrolls, so a
    baseline taken from it could be larger than the later sample."""
    return len(re.findall(pattern, pane(hist=True)))


def wait(pattern, secs=60, what="", n=1, hist=False):
    t0 = time.time()
    while time.time() - t0 < secs:
        if len(re.findall(pattern, pane(hist))) >= n:
            print(f"  ok: {what or pattern} ({time.time() - t0:.1f}s)")
            return True
        time.sleep(0.5)
    print(f"  !! timeout waiting for {what or pattern}\n{pane()[-1500:]}")
    return False


def send(text, enter=True):
    t("send-keys", "-t", S, "-l", text)
    if enter:
        time.sleep(0.3)
        t("send-keys", "-t", S, "Enter")


def live_row():
    """Whether an approval row is waiting: it is the last line above the
    separator. An answered row stays in the transcript with its result under
    it, so a search of the whole pane keeps finding it after the answer — and
    a key sent at a row that is no longer live is typed into the composer and
    rides along with the next prompt."""
    lines = [l for l in pane().splitlines() if l.strip()]
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].lstrip().startswith("─") and lines[i].count("─") >= 20:
            return re.search(APPROVAL, lines[i - 1]) is not None
    return False


def wait_row(secs, what):
    t0 = time.time()
    while time.time() - t0 < secs:
        if live_row():
            print(f"  ok: {what} ({time.time() - t0:.1f}s)")
            return True
        time.sleep(0.5)
    print(f"  !! timeout waiting for {what}\n{pane()[-1500:]}")
    return False


def answer(key="y"):
    """Answer the approval row if one is live, then wait for it to clear."""
    if not live_row():
        return False
    send(key, enter=False)
    t0 = time.time()
    while time.time() - t0 < 10 and live_row():
        time.sleep(0.2)
    return True


def wait_file(path, secs=120, what=""):
    """A turn really ran its command. Answers an approval row that shows up
    while waiting, for the harnesses that may or may not ask."""
    t0 = time.time()
    while time.time() - t0 < secs:
        if path.exists():
            print(f"  ok: {what} ({time.time() - t0:.1f}s)")
            return True
        answer()
        time.sleep(0.5)
    print(f"  !! timeout waiting for {what}\n{pane()[-1500:]}")
    return False


failures = 0


def check(cond, what):
    global failures
    print(("  ok: " if cond else "  FAIL: ") + what)
    failures += 0 if cond else 1


t("kill-session", "-t", S)
t("new-session", "-d", "-s", S, "-x", "160", "-y", "45", "-c", str(PROJ),
  f"env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT {a.bin}; echo EXIT=$?; sleep 300")
# the empty composer row, which tmux hands back as a bare ">" (it strips the
# trailing space); anchored, or the bar's own text would match
check(wait(r"(?m)^>\s*$", 60, "composer"), "window up")
# the bar lists every participant; nothing else on a fresh pane says opencode
OPENCODE = re.search(r"\bopencode\b", pane()) is not None
print(f"  opencode {'is' if OPENCODE else 'is not'} a participant"
      + ("" if OPENCODE else ": its step is skipped"))

send("/claude Reply with exactly the single word MARLIN and nothing else.")
# on its own line: the echoed prompt names MARLIN too, but that line starts
# with the "you → claude" header
check(wait(r"(?m)^MARLIN\b", 120, "claude reply"), "claude turn")

said = hits(r"MARLIN")
send("/codex What single word did the previous assistant reply with? Reply with that word, a space, "
     "and the word formed by joining OSP and REY. Then run the shell command: touch gate-codex.txt")
check(wait_row(90, "codex approval row"), "codex asked before running the command")
answer()
check(wait_file(PROJ / "gate-codex.txt", 120, "codex ran the command"), "codex ran the command")
check(wait(r"MARLIN", 60, "codex names claude's word", n=said + 1, hist=True),
      "claude's turn reached codex")

if OPENCODE:
    said = hits(r"MARLIN")
    send("/opencode:opencode/big-pickle Which single words did the previous assistants reply with? Then run: touch gate-oc.txt")
    check(wait_file(PROJ / "gate-oc.txt", 180, "opencode ran the command"), "opencode ran the command")
    check(wait(r"MARLIN", 60, "opencode names the relay", n=said + 1, hist=True),
          "the relay reached opencode")

send("/claude Use the tool named exactly Bash to run: touch gate-claude.txt — then reply with "
     "the word codex added after MARLIN, a space, and DONE.")
check(wait_row(90, "claude approval row"), "claude asked before running the command")
answer()
check(wait_file(PROJ / "gate-claude.txt", 120, "claude ran the command"), "claude ran the command")
check(wait(r"(?m)^OSPREY DONE\b", 120, "claude reply"),
      "codex's turn reached claude, and claude's second turn ran")

t("send-keys", "-t", S, "C-c"); time.sleep(0.3); t("send-keys", "-t", S, "C-c")
check(wait(r"EXIT=0", 30, "clean exit"), "exit code 0")

status = subprocess.run([a.bin, "status"], cwd=PROJ, capture_output=True, text=True).stdout
print(status)
t("kill-server")
print("PASS" if failures == 0 else f"FAIL ({failures})")
sys.exit(1 if failures else 0)
