import json
import time
import unittest

from helpers import StateCase, load_tool

B = load_tool("pvebackup")
NOW = int(time.time())
H, DAY, GiB = 3600, 86400, 1024 ** 3


def bk(storage, vmid, age, verify=None, size_gib=2):
    e = {"volid": f"{storage}:backup/vm/{vmid}/{NOW - age}", "vmid": vmid, "ctime": NOW - age,
         "size": size_gib * GiB, "content": "backup", "format": "pbs-vm"}
    if verify:
        e["verification"] = {"state": verify, "upid": "UPID:x"}
    return e


def fixture():
    res = [{"type": "node", "node": "pve1", "status": "online"}, {"type": "node", "node": "pve2", "status": "online"},
           {"type": "node", "node": "pve3", "status": "offline"}]
    for node in ("pve1", "pve2"):
        res.append({"type": "storage", "node": node, "storage": "pbs", "shared": 1, "status": "available",
                    "content": "backup", "disk": 90 * GiB, "maxdisk": 100 * GiB})
        res.append({"type": "storage", "node": node, "storage": "local", "shared": 0, "status": "available",
                    "content": "iso,backup", "disk": GiB, "maxdisk": 100 * GiB})
    res += [{"type": "qemu", "vmid": v, "node": "pve1", "name": f"g{v}"} for v in (100, 101, 102, 103, 104)]
    res.append({"type": "qemu", "vmid": 200, "node": "pve1", "name": "tpl", "template": 1})
    return {
        "resources": res,
        "content": {
            "*/pbs": [bk("pbs", 100, 2 * H, "ok"), bk("pbs", 101, 3 * DAY), bk("pbs", 104, DAY, "failed"),
                      bk("pbs", 104, 2 * DAY, "ok"), bk("pbs", 900, 30 * DAY)],
            "pve1/local": [bk("local", 100, 10 * DAY), {"volid": "local:iso/x.iso", "content": "iso"}],
            "pve2/local": [],
        },
        "not_backed_up": [{"vmid": 102, "name": "g102", "type": "qemu"}],
        "backup_jobs": [{"id": "backup-1", "enabled": 1}, {"id": "backup-2", "enabled": 0, "schedule": "sun 01:00"}],
        "node_tasks": {
            "pve1": [{"type": "vzdump", "status": "job errors", "starttime": NOW - DAY, "endtime": NOW - DAY + 60,
                      "id": ""},
                     {"type": "vzdump", "status": "OK", "starttime": NOW - H, "endtime": NOW - H + 60},
                     {"type": "vzdump", "status": "unable to open", "starttime": NOW - 20 * DAY,
                      "endtime": NOW - 20 * DAY, "id": "101"}],
            "pve2": [],
        },
    }


class EndToEnd(StateCase):
    tool = "pvebackup"

    def fixture(self):
        return fixture()

    def test_check(self):
        rc, out, err = self.run_cli("check", "--json")
        self.assertEqual(rc, 1, err)
        data = json.loads(out)
        got = sorted((f["severity"], f["vmid"] or 0, f["check"]) for f in data["findings"])
        self.assertEqual(got, sorted([
            ("WARN", 101, "age"), ("WARN", 102, "nojob"), ("ERROR", 103, "none"), ("ERROR", 104, "verify"),
            ("WARN", 0, "usage"), ("INFO", 0, "jobs"), ("WARN", 0, "failed"),
            ("INFO", 900, "deleted-guest"),
        ]))
        self.assertEqual(data["summary"]["guests"], 5)
        self.assertEqual(data["summary"]["backups"], 5)
        self.assertEqual(data["summary"]["storages"], 3)

    def test_check_filters_and_thresholds(self):
        rc, out, _ = self.run_cli("check", "100", "--max-usage", "95", "--failed-since", "0")
        self.assertEqual(rc, 0, out)
        self.assertIn("1 guest(s), 2 backup(s) on 3 storage(s): 0 error, 0 warn, 1 info", out)
        rc, out, _ = self.run_cli("check", "101", "--max-age", "4d", "--failed-since", "30d", "-q", "--json")
        self.assertEqual(rc, 1)
        got = sorted((f["vmid"] or 0, f["check"]) for f in json.loads(out)["findings"])
        self.assertEqual(got, [(0, "failed"), (0, "usage"), (101, "failed")])

    def test_coverage_unknown(self):
        st = fixture()
        st["errors"] = {"/cluster/backup-info/not-backed-up": "permission denied"}
        self.write(st)
        rc, out, _ = self.run_cli("check", "102,103", "--json")
        got = {(f["vmid"], f["check"], f["severity"]) for f in json.loads(out)["findings"]}
        self.assertIn((102, "none", "WARN"), got)
        self.assertIn((None, "jobs", "INFO"), got)

    def test_list(self):
        rc, out, err = self.run_cli("list")
        self.assertEqual(rc, 0, err)
        lines = {l.split()[0]: l.split() for l in out.splitlines() if l[:1].isdigit()}
        self.assertEqual(lines["100"][3], "yes")
        self.assertEqual(lines["100"][7], "2", lines["100"])
        self.assertEqual(lines["102"][3], "no")
        self.assertNotIn("200", lines)
        rc, out, _ = self.run_cli("list", "--templates", "--json")
        self.assertIn(200, [g["vmid"] for g in json.loads(out)])


if __name__ == "__main__":
    unittest.main()
