# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKE_PVESH = os.path.join(HERE, "fake_pvesh")
FAKE_PVESM = os.path.join(HERE, "fake_pvesm")
TOOLS = ("pvesnap", "pvedisk", "pvebackup", "pvenode")


def load_tool(name):
    path = os.path.join(ROOT, name)
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


class StateCase(unittest.TestCase):
    """Runs a tool as subprocess against tests/fake_pvesh with a per-test JSON state."""

    tool = None
    extra_args = ()

    def fixture(self):
        return {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.write(self.fixture())

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, st):
        with open(self.state, "w") as f:
            json.dump(st, f)

    def read(self):
        with open(self.state) as f:
            return json.load(f)

    def run_cli(self, *argv):
        env = dict(os.environ, FAKE_PVE_STATE=self.state, NO_COLOR="1")
        for k in ("PVE_API_URL", "PVE_TOKEN"):
            env.pop(k, None)
        cmd = [sys.executable, os.path.join(ROOT, self.tool), argv[0], "--pvesh", FAKE_PVESH]
        cmd += list(self.extra_args) + list(argv[1:])
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, env=env, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def calls(self, method):
        return [c for c in self.read().get("calls", []) if c[0] == method]
