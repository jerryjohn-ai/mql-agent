"""Starts both MCP servers (against the fake org) on test ports for the whole session."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

READ_PORT, WRITE_PORT = 18101, 18102
APPROVAL_KEY = "test-approval-key"


def _wait(port, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"server on {port} did not start")


@pytest.fixture(scope="session")
def fake_org(tmp_path_factory):
    p = tmp_path_factory.mktemp("org") / "fake_org.json"
    shutil.copy(ROOT / "servers" / "fake_org.json", p)
    return p


@pytest.fixture(scope="session")
def servers(fake_org, tmp_path_factory):
    logs = tmp_path_factory.mktemp("logs")
    env = os.environ | {
        "MQL_FAKE_SALESFORCE": "1", "MQL_FAKE_ORG_PATH": str(fake_org), "MQL_FAKE_PERSIST": "1",
        "MQL_READ_PORT": str(READ_PORT), "MQL_WRITE_PORT": str(WRITE_PORT), "MQL_APPROVAL_KEY": APPROVAL_KEY,
        "PYTHONPATH": str(ROOT),
    }
    procs = [
        subprocess.Popen([sys.executable, "-m", "servers.server_a"], cwd=ROOT, env=env, stdout=open(logs / "a.log", "w"), stderr=subprocess.STDOUT),
        subprocess.Popen([sys.executable, "-m", "servers.server_b"], cwd=ROOT, env=env, stdout=open(logs / "b.log", "w"), stderr=subprocess.STDOUT),
    ]
    try:
        _wait(READ_PORT)
        _wait(WRITE_PORT)
        yield {"read": f"http://127.0.0.1:{READ_PORT}/mcp", "write": f"http://127.0.0.1:{WRITE_PORT}/mcp", "logs": logs}
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)


@pytest.fixture
def settings(servers, tmp_path):
    from mqlagent.settings import Settings

    return Settings(data_dir=tmp_path, read_server_url=servers["read"], write_server_url=servers["write"],
                    approval_key=APPROVAL_KEY, approvers=("jerry",))


@pytest.fixture
def org_state(fake_org):
    return lambda: json.loads(Path(fake_org).read_text())


def load_sample(name: str) -> dict:
    return json.loads((ROOT / "samples" / "leads" / name).read_text())
