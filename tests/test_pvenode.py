# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import json
import time
import unittest

from helpers import StateCase, load_tool

N = load_tool("pvenode")
NOW = int(time.time())
DAY, GiB = 86400, 1024 ** 3


def certs(days_left):
    return [{"filename": "pve-root-ca.pem", "notafter": NOW + 3000 * DAY},
            {"filename": "pve-ssl.pem", "notafter": NOW + days_left * DAY}]


def fixture():
    return {
        "resources": [{"type": "node", "node": n, "status": s}
                      for n, s in (("pve1", "online"), ("pve2", "online"), ("pve3", "offline"))],
        "cluster_status": [{"type": "cluster", "name": "c", "quorate": 1, "nodes": 3},
                           {"type": "node", "name": "pve1", "online": 1}, {"type": "node", "name": "pve2", "online": 1},
                           {"type": "node", "name": "pve3", "online": 0}],
        "storage_cfg": [{"storage": "ceph", "type": "rbd", "pool": "vms"}],
        "ceph_status": {"health": {"status": "HEALTH_WARN", "checks": {
            "OSD_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1 osds down"}}}}},
        "node_api": {
            "pve1": {
                "version": {"version": "8.2.4", "release": "8.2"},
                "status": {"current-kernel": {"release": "6.8.12-4-pve"}, "uptime": 40 * DAY,
                           "rootfs": {"used": 10 * GiB, "total": 100 * GiB}},
                "time": {"time": NOW},
                "apt/versions": [{"Package": "pve-manager", "Version": "8.2.4", "CurrentState": "Installed"},
                                 {"Package": "proxmox-kernel-6.8.12-4-pve-signed", "CurrentState": "Installed"},
                                 {"Package": "proxmox-kernel-6.8.12-10-pve-signed", "CurrentState": "Installed"},
                                 {"Package": "proxmox-kernel-6.11.0-1-pve-signed", "CurrentState": "ConfigFiles"}],
                "apt/update": [{"Package": "pve-manager"}, {"Package": "libc6"}],
                "certificates/info": certs(10),
                "disks/list": [{"devpath": "/dev/sda", "model": "SSD1", "health": "PASSED"},
                               {"devpath": "/dev/sdb", "model": "HDD2", "health": "FAILED"},
                               {"devpath": "/dev/sdc", "health": "UNKNOWN"}],
                "disks/zfs": [{"name": "rpool", "health": "ONLINE", "alloc": 90 * GiB, "size": 100 * GiB}],
                "disks/lvmthin": [{"vg": "pve", "lv": "data", "used": 50 * GiB, "lv_size": 100 * GiB,
                                   "metadata_used": 97, "metadata_size": 100}],
            },
            "pve2": {
                "version": {"version": "8.1.3", "release": "8.1"},
                "status": {"kversion": "Linux 6.5.13-1-pve #1 SMP PREEMPT_DYNAMIC PMX 6.5.13-1", "uptime": 3 * DAY,
                           "rootfs": {"used": 90 * GiB, "total": 100 * GiB}},
                "time": {"time": NOW + 60},
                "apt/versions": [{"Package": "pve-kernel-6.5.13-1-pve", "CurrentState": "Installed"}],
                "apt/update": [],
                "certificates/info": certs(200),
                "disks/list": [],
                "disks/zfs": [{"name": "tank", "health": "DEGRADED", "alloc": 1, "size": 100}],
                "disks/lvmthin": [],
            },
        },
    }


class ParseTests(unittest.TestCase):
    def test_kernel(self):
        self.assertGreater(N.kernel_key("6.8.12-10-pve"), N.kernel_key("6.8.12-4-pve"))
        self.assertEqual(N.running_kernel({"kversion": "Linux 5.15.102-1-pve #1 SMP"}), "5.15.102-1-pve")
        self.assertEqual(N.running_kernel({"current-kernel": {"release": "6.8.12-4-pve"}}), "6.8.12-4-pve")
        self.assertIsNone(N.running_kernel({}))
        self.assertEqual(N.newest_kernel(fixture()["node_api"]["pve1"]["apt/versions"]), "6.8.12-10-pve")
        self.assertIsNone(N.newest_kernel([{"Package": "proxmox-kernel-6.8"}]))

    def test_ceph_expected(self):
        self.assertTrue(N.ceph_expected([{"type": "rbd", "pool": "x"}]))
        self.assertFalse(N.ceph_expected([{"type": "rbd", "monhost": "10.0.0.1"}, {"type": "dir"}]))


class EndToEnd(StateCase):
    tool = "pvenode"

    def fixture(self):
        return fixture()

    def findings(self, *argv):
        rc, out, err = self.run_cli("check", "--json", *argv)
        self.assertIn(rc, (0, 1), err)
        return rc, sorted((f["severity"], f["node"], f["check"]) for f in json.loads(out)["findings"])

    def test_check(self):
        rc, got = self.findings()
        self.assertEqual(rc, 1)
        self.assertEqual(got, sorted([
            ("WARN", "pve1", "kernel"), ("INFO", "pve1", "updates"), ("WARN", "pve1", "cert"),
            ("ERROR", "pve1", "smart"), ("WARN", "pve1", "zfs"), ("ERROR", "pve1", "thin"),
            ("WARN", "pve2", "rootfs"), ("ERROR", "pve2", "zfs"),
            ("WARN", "pve3", "offline"), ("WARN", "", "version"), ("WARN", "", "time"), ("WARN", "", "ceph"),
        ]))

    def test_details(self):
        rc, out, _ = self.run_cli("check", "--node", "pve1")
        self.assertIn("running 6.8.12-4-pve, newest installed 6.8.12-10-pve: reboot pending", out)
        self.assertIn("thin pool pve/data metadata 97% used", out)
        self.assertIn("/dev/sdb HDD2 SMART health FAILED", out)
        self.assertIn("2 update(s) pending: libc6, pve-manager", out)
        self.assertIn("HEALTH_WARN: 1 osds down", out)
        self.assertNotIn("pve2", out)
        rc, out, _ = self.run_cli("check")
        self.assertIn("pve-manager differs: 8.1.3 (pve2); 8.2.4 (pve1)", out)
        self.assertIn("3 node(s), 2 online: 3 error, 8 warn, 1 info", out)

    def test_skip_thresholds_and_errors(self):
        st = fixture()
        st["errors"] = {"/nodes/pve2/disks/list": "permission check failed"}
        st["cluster_status"][0]["quorate"] = 0
        self.write(st)
        rc, got = self.findings("--node", "pve2", "--skip", "zfs", "--skip", "rootfs")
        self.assertEqual(got, [("ERROR", "", "quorum"), ("WARN", "", "ceph"), ("WARN", "pve2", "fetch")])
        rc, got = self.findings("--node", "pve1", "--max-usage", "0", "--cert-warn", "5d", "--skip", "ceph",
                                "--skip", "smart", "--skip", "kernel", "-q")
        self.assertEqual(got, [("ERROR", "", "quorum"), ("ERROR", "pve1", "thin")])
        self.assertFalse([c for c in self.calls("get") if c[1].endswith("/disks/list") and "pve1" in c[1]])
        rc, _, err = self.run_cli("check", "--node", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("node(s) not found: nope", err)

    def test_list(self):
        rc, out, err = self.run_cli("list")
        self.assertEqual(rc, 0, err)
        rows = {l.split()[0]: l.split() for l in out.splitlines() if l.startswith("pve")}
        self.assertEqual(rows["pve1"][1:5], ["online", "8.2.4", "6.8.12-4-pve", "yes"])
        self.assertEqual(rows["pve2"][4], "no")
        self.assertEqual(rows["pve3"][1], "offline")
        rc, out, _ = self.run_cli("list", "--json")
        data = {r["node"]: r for r in json.loads(out)}
        self.assertEqual(data["pve1"]["updates"], 2)
        self.assertEqual(data["pve1"]["cert_expires"], NOW + 10 * DAY)
        self.assertFalse([c for c in self.calls("get") if "/disks/" in c[1]])


if __name__ == "__main__":
    unittest.main()
