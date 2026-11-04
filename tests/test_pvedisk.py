import json
import os
import unittest

from helpers import FAKE_PVESM, StateCase, load_tool

D = load_tool("pvedisk")
GiB = 1024 ** 3


def vol(volid, vmid, size_gib=1, content="images"):
    return {"volid": volid, "vmid": vmid, "size": int(size_gib * GiB), "content": content, "format": "raw"}


def fixture():
    res = [{"type": "node", "node": "pve1", "status": "online"}, {"type": "node", "node": "pve2", "status": "online"}]
    for node in ("pve1", "pve2"):
        res.append({"type": "storage", "node": node, "storage": "local-lvm", "plugintype": "lvmthin", "shared": 0,
                    "status": "available", "content": "images,rootdir"})
        res.append({"type": "storage", "node": node, "storage": "ceph", "plugintype": "rbd", "shared": 1,
                    "status": "available", "content": "images"})
    res += [
        {"type": "storage", "node": "pve1", "storage": "local", "plugintype": "dir", "shared": 0,
         "status": "available", "content": "iso,images"},
        {"type": "storage", "node": "pve1", "storage": "usb", "plugintype": "dir", "shared": 0,
         "status": "unknown", "content": "images"},
        {"type": "storage", "node": "pve1", "storage": "nas", "plugintype": "nfs", "shared": 1,
         "status": "available", "content": "backup"},
        {"type": "qemu", "vmid": 100, "node": "pve1", "name": "web"},
        {"type": "lxc", "vmid": 101, "node": "pve1", "name": "ct"},
        {"type": "qemu", "vmid": 102, "node": "pve2", "name": "db"},
        {"type": "qemu", "vmid": 103, "node": "pve1", "name": "moving", "lock": "migrate"},
        {"type": "qemu", "vmid": 105, "node": "pve1", "name": "clone"},
        {"type": "qemu", "vmid": 106, "node": "pve2", "name": "moved"},
        {"type": "qemu", "vmid": 200, "node": "pve1", "name": "tpl", "template": 1},
    ]
    configs = {
        "100": {"config": {"scsi0": "local-lvm:vm-100-disk-0,size=32G", "scsi1": "ceph:vm-100-disk-1,size=10G",
                           "unused0": "local-lvm:vm-100-disk-2", "ide2": "local:iso/debian.iso,media=cdrom",
                           "efidisk0": "local-lvm:vm-100-disk-3,efitype=4m", "net0": "virtio=BC:24:11:00:00:01"},
                "snapshots": {"pre": {"scsi0": "local-lvm:vm-100-disk-0,size=32G",
                                      "scsi2": "local-lvm:vm-100-disk-4,size=4G",
                                      "vmstate": "local-lvm:vm-100-state-pre"}}},
        "101": {"config": {"rootfs": "local-lvm:vm-101-disk-0,size=8G", "mp0": "/mnt/bind,mp=/data"}},
        "102": {"config": {"scsi0": "local-lvm:vm-102-disk-0,size=20G"}},
        "103": {"config": {"scsi0": "local-lvm:vm-103-disk-0,size=8G"}},
        "105": {"config": {"scsi0": "local-lvm:base-200-disk-0/vm-105-disk-0,size=8G"},
                "pending": [{"key": "scsi1", "pending": "local-lvm:vm-105-disk-1,size=1G"}]},
        "106": {"config": {"scsi0": "local-lvm:vm-106-disk-0,size=16G"}},
        "200": {"config": {"scsi0": "local-lvm:base-200-disk-0,size=8G", "template": "1"}},
    }
    content = {
        "pve1/local-lvm": [
            vol("local-lvm:vm-100-disk-0", 100, 32), vol("local-lvm:vm-100-disk-2", 100, 5),
            vol("local-lvm:vm-100-disk-4", 100, 4), vol("local-lvm:vm-100-state-pre", 100, 2),
            vol("local-lvm:vm-100-disk-9", 100, 3), vol("local-lvm:vm-101-disk-0", 101, 8, "rootdir"),
            vol("local-lvm:vm-102-disk-0", 102, 20), vol("local-lvm:vm-103-disk-0", 103, 8),
            vol("local-lvm:vm-103-disk-1", 103, 8), vol("local-lvm:vm-105-disk-0", 105, 8),
            vol("local-lvm:vm-105-disk-1", 105, 1), vol("local-lvm:base-200-disk-0", 200, 8),
            vol("local-lvm:vm-106-disk-0", 106, 16), vol("local-lvm:vm-999-disk-0", 999, 50),
        ],
        "pve2/local-lvm": [vol("local-lvm:vm-102-disk-0", 102, 20), vol("local-lvm:vm-106-disk-0", 106, 16)],
        "*/ceph": [vol("ceph:vm-100-disk-1", 100, 10), vol("ceph:vm-998-disk-0", 998, 7)],
        "pve1/local": [{"volid": "local:iso/debian.iso", "content": "iso", "size": GiB},
                       vol("local:777/vm-777-disk-0.qcow2", 777, 6)],
        "*/nas": [],
    }
    return {"resources": res, "configs": configs, "content": content, "tasks": [],
            "replication": [{"id": "102-0", "guest": 102, "target": "pve1"}],
            "cluster_status": [{"type": "cluster", "name": "c"}, {"type": "node", "name": "pve1", "local": 1}]}


def conf_text(cfg):
    lines = [f"{k}: {v}" for k, v in cfg["config"].items()]
    for name, snap in cfg.get("snapshots", {}).items():
        lines += ["", f"[{name}]"] + [f"{k}: {v}" for k, v in snap.items()]
    for e in cfg.get("pending", []):
        lines += ["", "[PENDING]", f"{e['key']}: {e['pending']}"]
    return "\n".join(lines) + "\n"


class ParseTests(unittest.TestCase):
    def test_value_volids(self):
        ids = {"local-lvm", "ceph", "local"}
        self.assertEqual(D.value_volids("local-lvm:vm-100-disk-0,size=32G", ids), [("local-lvm", "vm-100-disk-0")])
        self.assertEqual(D.value_volids("file=ceph:vm-1-disk-0,cache=none", ids), [("ceph", "vm-1-disk-0")])
        self.assertEqual(D.value_volids("volume=local:101/vm-101-disk-0.raw,mp=/x", ids),
                         [("local", "101/vm-101-disk-0.raw")])
        self.assertEqual(D.value_volids("virtio=BC:24:11:00:00:01,bridge=vmbr0", ids), [])
        self.assertEqual(D.value_volids("/mnt/bind,mp=/data", ids), [])
        self.assertEqual(D.value_volids("none,media=cdrom", ids), [])
        self.assertEqual(D.value_volids("other:vm-1-disk-0", ids), [])

    def test_linked_clone_keys(self):
        self.assertEqual(D.vol_keys("s", "base-200-disk-0/vm-105-disk-0"),
                         [("s", "vm-105-disk-0"), ("s", "base-200-disk-0")])
        self.assertEqual(D.norm_vol("200/base-200-disk-0.qcow2/105/vm-105-disk-0.qcow2"), "105/vm-105-disk-0.qcow2")
        self.assertEqual(D.norm_vol("vm-100-disk-0"), "vm-100-disk-0")

    def test_parse_conf_sections(self):
        text = "#comment local-lvm:vm-1-disk-7\nscsi0: local-lvm:vm-1-disk-0\nunused0: local-lvm:vm-1-disk-1\n\n" \
               "[PENDING]\nscsi1: local-lvm:vm-1-disk-2\ndelete: scsi3\n\n[snap1]\nscsi0: local-lvm:vm-1-disk-0\n" \
               "vmstate: local-lvm:vm-1-state-snap1\nparent: x\n"
        g = D.GuestCfg(1, "n", "qemu")
        D.apply_refs(g, D.parse_conf(text), {"local-lvm"})
        self.assertEqual(g.refs, {("local-lvm", "vm-1-disk-0"): {"config", "snapshot"},
                                  ("local-lvm", "vm-1-disk-1"): {"unused"},
                                  ("local-lvm", "vm-1-disk-2"): {"pending"},
                                  ("local-lvm", "vm-1-state-snap1"): {"snapshot"}})
        self.assertEqual(g.unused, {("local-lvm", "vm-1-disk-1"): "unused0"})

    def test_remote_path_volume_refused(self):
        class Local:
            local = True

            def get(self, path, **kw):
                return [{"type": "node", "name": "pve1", "local": 1}]

        D._LOCAL_NODE.clear()
        f = D.Finding("WARN", "orphan", "pve2", "local", "local:777/vm-777-disk-0.qcow2", 777, 1, "")
        with self.assertRaisesRegex(D.PveError, "run 'pvesm free local:777/vm-777-disk-0.qcow2' on pve2"):
            D.free_volume(Local(), f, None)
        D._LOCAL_NODE.clear()


class EndToEnd(StateCase):
    tool = "pvedisk"
    extra_args = ("--pvesm", FAKE_PVESM)

    def fixture(self):
        return fixture()

    def check_json(self, *argv):
        rc, out, err = self.run_cli("check", "--json", *argv)
        self.assertIn(rc, (0, 1), err)
        return rc, json.loads(out)

    def kinds(self, data):
        out = {}
        for f in data["findings"]:
            out.setdefault(f["kind"], []).append(f["volid"])
        return {k: sorted(v) for k, v in out.items()}

    def test_classification(self):
        rc, data = self.check_json()
        self.assertEqual(rc, 1)
        self.assertEqual(data["summary"]["config_source"], "api")
        self.assertEqual(self.kinds(data), {
            "orphan": ["ceph:vm-998-disk-0", "local-lvm:vm-100-disk-9", "local-lvm:vm-999-disk-0",
                       "local:777/vm-777-disk-0.qcow2"],
            "stale": ["local-lvm:vm-106-disk-0"],
            "unused": ["local-lvm:vm-100-disk-2"],
            "snapshot-only": ["local-lvm:vm-100-disk-4"],
            "busy": ["local-lvm:vm-103-disk-1"],
            "missing": ["local-lvm:vm-100-disk-3"],
            "scan": [""],
        })
        stale = next(f for f in data["findings"] if f["kind"] == "stale")
        self.assertEqual(stale["node"], "pve1")

    def test_files_source_matches_api(self):
        st = self.read()
        root = os.path.join(self.tmp.name, "pve")
        for r in st["resources"]:
            if r["type"] in ("qemu", "lxc"):
                d = os.path.join(root, "nodes", r["node"], "qemu-server" if r["type"] == "qemu" else "lxc")
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f"{r['vmid']}.conf"), "w") as f:
                    f.write(conf_text(st["configs"][str(r["vmid"])]))
        _, api = self.check_json()
        _, files = self.check_json("--pve-dir", root)
        self.assertEqual(files["summary"]["config_source"], "files")
        self.assertEqual(self.kinds(files), self.kinds(api))

    def test_unreadable_config_protects_its_volumes(self):
        st = fixture()
        del st["configs"]["101"]
        self.write(st)
        _, data = self.check_json()
        self.assertNotIn("local-lvm:vm-101-disk-0", self.kinds(data).get("orphan", []))
        self.assertTrue(any(f["kind"] == "config" and f["vmid"] == 101 for f in data["findings"]))

    def test_tasks_unreadable_is_fatal(self):
        st = fixture()
        st["errors"] = {"/cluster/tasks": "permission denied"}
        self.write(st)
        rc, _, err = self.run_cli("delete", "-y")
        self.assertEqual(rc, 2)
        self.assertIn("refusing to classify", err)
        self.assertEqual(self.calls("delete"), [])

    def test_dry_run(self):
        rc, out, _ = self.run_cli("delete", "-n")
        self.assertEqual(rc, 0)
        self.assertIn("plan: delete 4 volume(s), 66.0 GiB, kinds orphan", out)
        self.assertEqual(self.calls("delete") + self.calls("pvesm"), [])

    def test_delete_orphans(self):
        rc, out, err = self.run_cli("delete", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(c[1].rsplit("/", 1)[-1] for c in self.calls("delete")),
                         ["ceph:vm-998-disk-0", "local-lvm:vm-100-disk-9", "local-lvm:vm-999-disk-0"])
        self.assertEqual([c[2] for c in self.calls("pvesm")], ["local:777/vm-777-disk-0.qcow2"])
        self.assertIn("deleted 4, failed 0, skipped 0", out)
        rc, data = self.check_json()
        self.assertNotIn("orphan", self.kinds(data))

    def test_delete_filters_and_unused(self):
        rc, out, _ = self.run_cli("delete", "999", "-y")
        self.assertIn("deleted 1, failed 0", out)
        rc, out, err = self.run_cli("delete", "--kind", "unused", "-y")
        self.assertEqual(rc, 0, out + err)
        sets = self.calls("set")
        self.assertEqual(sets[0][2], {"delete": "unused0", "digest": "d0"})
        self.assertNotIn("unused0", self.read()["configs"]["100"]["config"])
        rc, out, _ = self.run_cli("delete", "--kind", "orphan", "--storage", "ceph", "-n")
        self.assertIn("plan: delete 1 volume(s)", out)

    def test_failure_reported(self):
        st = fixture()
        st["fail"] = {"local-lvm:vm-999-disk-0": "lvremove failed: device busy"}
        self.write(st)
        rc, out, _ = self.run_cli("delete", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: task failed: lvremove failed: device busy", out)
        self.assertIn("deleted 3, failed 1, skipped 0", out)


if __name__ == "__main__":
    unittest.main()
