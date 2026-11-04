# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import os
import unittest

from helpers import ROOT, TOOLS, load_tool

S = load_tool("pvesnap")


class FlakyBackend:
    """Task status poll fails `fail` times, then reports the task stopped with `exitstatus`."""

    def __init__(self, fail, exitstatus="OK"):
        self.fail, self.exitstatus, self.polls = fail, exitstatus, 0

    def get(self, path, **params):
        self.polls += 1
        if self.polls <= self.fail:
            raise S.PveError("HTTP 595 connection refused")
        return {"status": "stopped", "exitstatus": self.exitstatus}


class SharedFunctionTests(unittest.TestCase):
    def test_api_url_default_port(self):
        cases = {"pve1": "https://pve1:8006/api2/json", "https://pve1/": "https://pve1:8006/api2/json",
                 "https://pve1:443": "https://pve1:443/api2/json",
                 "https://[fd00::1]": "https://[fd00::1]:8006/api2/json",
                 "https://pve1:8006/api2/json": "https://pve1:8006/api2/json"}
        for url, want in cases.items():
            self.assertEqual(S.ApiBackend(url, "root@pam!t=x").base, want, url)

    def test_wait_task_survives_poll_errors(self):
        upid = "UPID:pve1:0001:0:0:qmdelsnapshot:100:root@pam:"
        orig, S.time.sleep = S.time.sleep, lambda s: None
        try:
            self.assertEqual(S.wait_task(FlakyBackend(4), upid, 60), "")
            with self.assertRaises(S.PveError):
                S.wait_task(FlakyBackend(5), upid, 60)
        finally:
            S.time.sleep = orig


class SharedBlockTests(unittest.TestCase):
    def test_tools_embed_identical_shared_block(self):
        with open(os.path.join(ROOT, "lib", "shared.py")) as f:
            want = f.read()
        for tool in TOOLS:
            with open(os.path.join(ROOT, tool)) as f:
                text = f.read()
            start = text.index("# ==== shared:begin\n") + len("# ==== shared:begin\n")
            end = text.index("# ==== shared:end")
            self.assertEqual(text[start:end], want, f"{tool}: run scripts/sync-shared.sh")


if __name__ == "__main__":
    unittest.main()
