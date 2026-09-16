"""`opencode serve --port N --hostname H` stand-in: serves FakeOpencode on the
given port until killed. With $FAKE_OPENCODE_UNHEALTHY=1 it binds and answers
but never reports healthy — a serve that spawns and hangs."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fake_opencode_server import FakeOpencode  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("serve")
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--hostname", default="127.0.0.1")
a = ap.parse_args()
fake = FakeOpencode(port=a.port)                 # binds and serves on a daemon thread
while True:
    time.sleep(1)
