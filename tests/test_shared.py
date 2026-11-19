# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import os
import pty
import tempfile
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

    def test_pvesh_never_gets_the_terminal(self):
        # pvesh fork_worker: 'if ($sync && -t STDIN) tcsetpgrp(...)' dies in the new session _run starts
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "pvesh")
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nif [ -t 0 ]; then echo tty; else readlink /proc/self/fd/0; fi\n")
            os.chmod(fake, 0o755)
            master, slave = pty.openpty()
            saved = os.dup(0)
            try:
                os.dup2(slave, 0)
                self.assertTrue(os.isatty(0))
                self.assertEqual(S.PveshBackend(fake).create("/nodes/n/qemu", vmid=1), "/dev/null")
            finally:
                os.dup2(saved, 0)
                for fd in (saved, master, slave):
                    os.close(fd)

    def test_pvesh_create_result_is_the_last_line(self):
        cases = {"Formatting '/x/vm-1-disk-0.qcow2', fmt=qcow2 size=1\\n\"nfs:1/vm-1-disk-0.qcow2\"": "nfs:1/vm-1-disk-0.qcow2",
                 "Formatting '/x', fmt=qcow2\\nnfs:1/vm-1-disk-0.qcow2\\n\\n": "nfs:1/vm-1-disk-0.qcow2",
                 "\"UPID:n:1:2:3:qmcreate:1:root@pam:\"": "UPID:n:1:2:3:qmcreate:1:root@pam:",
                 "{\"a\": 1}": {"a": 1}, "": None}
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "pvesh")
            for text, want in cases.items():
                with open(fake, "w") as f:
                    f.write(f"#!/bin/sh\nprintf '%b' '{text}'\n".replace("'/x/vm-1-disk-0.qcow2'", "/x/vm").replace(
                        "'/x'", "/x"))
                os.chmod(fake, 0o755)
                self.assertEqual(S.PveshBackend(fake).create("/x"), want, text)

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
