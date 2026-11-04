# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import argparse
import json
import time
import unittest

from helpers import StateCase, load_tool

P = load_tool("pvesnap")

NOW = int(time.time())
DAY = 86400


def snap(name, days, parent=None, **kw):
    d = {"name": name, "snaptime": NOW - days * DAY, "description": kw.pop("desc", ""), "vmstate": kw.pop("vmstate", 0)}
    if parent:
        d["parent"] = parent
    d.update(kw)
    return d


def fixture():
    return {
        "resources": [
            {"type": "qemu", "vmid": 100, "node": "pve1", "name": "web01", "status": "running"},
            {"type": "lxc", "vmid": 101, "node": "pve2", "name": "ct01", "status": "running"},
            {"type": "qemu", "vmid": 102, "node": "pve1", "name": "locked", "status": "stopped",
             "lock": "snapshot-delete"},
            {"type": "qemu", "vmid": 103, "node": "pve1", "name": "broken", "status": "stopped"},
            {"type": "qemu", "vmid": 104, "node": "pve3", "name": "offline", "status": "unknown"},
            {"type": "storage", "id": "storage/pve1/local", "node": "pve1"},
        ],
        "snapshots": {
            # a -> b -> c -> NOW, branch b -> x -> y
            "100": [snap("a", 60), snap("b", 40, "a"), snap("c", 10, "b", vmstate=1),
                    snap("x", 20, "b"), snap("y", 5, "x", desc="branch tip\nsecond line")],
            # manual -> auto-1 -> auto-2 -> auto-3 -> auto-4 -> NOW
            "101": [snap("manual", 50), snap("auto-1", 4, "manual"), snap("auto-2", 3, "auto-1"),
                    snap("auto-3", 2, "auto-2"), snap("auto-4", 1, "auto-3")],
            "103": [snap("z", 2, "gone", snapstate="delete")],
        },
        "current": {"100": "c", "101": "auto-4", "103": "z"},
        "broken_nodes": ["104"],
        "tasks": [],
    }


def guest(vmid, st=None):
    st = st or fixture()
    g = P.Guest(vmid=vmid, node="n", type="qemu")
    entries = list(st["snapshots"].get(str(vmid), []))
    cur = st["current"].get(str(vmid))
    entries.append({"name": "current", "parent": cur} if cur else {"name": "current"})
    return P.build_tree(g, entries)


def delete_args(*argv):
    return P.build_parser().parse_args(["delete"] + list(argv))


class ParseTests(unittest.TestCase):
    def test_age(self):
        self.assertEqual(P.parse_age("30d"), 30 * DAY)
        self.assertEqual(P.parse_age("12h"), 12 * 3600)
        self.assertEqual(P.parse_age("2w"), 14 * DAY)
        self.assertEqual(P.parse_age("90m"), 90 * 60)
        self.assertEqual(P.parse_age("5"), 5 * DAY)
        with self.assertRaises(argparse.ArgumentTypeError):
            P.parse_age("5 months")

    def test_idspec(self):
        self.assertIsNone(P.parse_idspec("all"))
        self.assertEqual(P.parse_idspec("100,105,200-202"), {100, 105, 200, 201, 202})
        for bad in ("abc", "10-5", ","):
            with self.assertRaises(argparse.ArgumentTypeError):
                P.parse_idspec(bad)

    def test_storage_parsers(self):
        zfs = "rpool/data/vm-100-disk-0@a\nrpool/data/vm-100-disk-0@old\nrpool/data/subvol-101-disk-0@manual\n" \
              "rpool/data/vm-100-disk-0@__replicate_100-0_1700000000__\nrpool/ROOT/pve-1@install\n"
        got = [(s.vmid, s.snap) for s in P.parse_zfs_snapshots(zfs)]
        self.assertEqual(got, [(100, "a"), (100, "old"), (101, "manual")])
        vols = "rpool/data/vm-100-disk-0\nrpool/data/vm-100-state-c\nrpool/data/vm-100-state-gone\n"
        self.assertEqual([(s.vmid, s.snap, s.state) for s in P.parse_zfs_volumes(vols)],
                         [(100, "c", True), (100, "gone", True)])
        lvs = "  pve  data\n  pve  vm-100-disk-0\n  pve  snap_vm-100-disk-0_b\n  pve  snap_vm-999-disk-1_x\n" \
              "  pve  vm-100-state-c\n"
        self.assertEqual([(s.kind, s.vmid, s.snap) for s in P.parse_lvs(lvs)],
                         [("lvm-thin snapshot", 100, "b"), ("lvm-thin snapshot", 999, "x"),
                          ("lvm state volume", 100, "c")])


class TreeTests(unittest.TestCase):
    def test_depth(self):
        g = guest(100)
        self.assertEqual({n: s.depth for n, s in g.snaps.items()}, {"a": 1, "b": 2, "c": 3, "x": 3, "y": 4})
        self.assertEqual(P.chain_depth(g), 3)
        self.assertEqual(P.ancestors(g, "c"), ["c", "b", "a"])
        self.assertEqual(sorted(P.descendants(g, "b")), ["b", "c", "x", "y"])

    def test_rows(self):
        labels = [p + ("NOW" if n == P.NOW_NODE else n) for p, n in P.tree_rows(guest(100))]
        self.assertEqual(labels, ["a", "└─ b", "   ├─ x", "   │  └─ y", "   └─ c", "      └─ NOW"])

    def test_broken_parent_and_cycle(self):
        g = guest(103)
        self.assertEqual(g.broken, [("z", "gone")])
        g = P.build_tree(P.Guest(1, "n", "qemu"), [{"name": "p", "parent": "q"}, {"name": "q", "parent": "p"}])
        self.assertEqual(sorted(g.cyclic), ["p", "q"])
        self.assertEqual(len(P.tree_rows(g)), 3)


class SelectTests(unittest.TestCase):
    def sel(self, vmid, *argv):
        names, missing = P.select_snapshots(guest(vmid), delete_args(str(vmid), *argv), NOW)
        return names, missing

    def test_named_and_missing(self):
        self.assertEqual(self.sel(100, "c", "a", "nope"), (["a", "c"], ["nope"]))

    def test_upto_is_older_chain(self):
        self.assertEqual(self.sel(100, "--upto", "b")[0], ["a", "b"])

    def test_subtree(self):
        self.assertEqual(self.sel(100, "--subtree", "x")[0], ["x", "y"])
        self.assertEqual(self.sel(100, "--subtree", "b")[0], ["b", "x", "c", "y"])

    def test_age_and_keep_last(self):
        self.assertEqual(self.sel(100, "--older-than", "30d")[0], ["a", "b"])
        self.assertEqual(self.sel(100, "--keep-last", "2")[0], ["a", "b", "x"])
        self.assertEqual(self.sel(100, "--upto", "c", "--older-than", "30d")[0], ["a", "b"])

    def test_match_keep_last_counts_only_matching(self):
        self.assertEqual(self.sel(101, "--match", "^auto", "--keep-last", "2")[0], ["auto-1", "auto-2"])
        self.assertEqual(self.sel(101, "--all", "--exclude", "^manual$", "--older-than", "2d")[0],
                         ["auto-1", "auto-2", "auto-3"])


class CheckTests(unittest.TestCase):
    def test_findings(self):
        st = fixture()
        guests = []
        for vmid in (100, 101, 103):
            guests.append(guest(vmid, st))
        locked = P.Guest(102, "pve1", "qemu", "locked", lock="snapshot-delete")
        guests.append(locked)
        args = P.build_parser().parse_args(["check", "--max-depth", "4"])
        got = {(f.vmid, f.check, f.severity) for f in P.run_checks(guests, args, NOW, [])}
        self.assertIn((100, "age", "WARN"), got)
        self.assertIn((100, "vmstate", "INFO"), got)
        self.assertIn((101, "depth", "WARN"), got)
        self.assertNotIn((100, "depth", "WARN"), got)
        self.assertIn((102, "lock", "WARN"), got)
        self.assertIn((103, "parent", "ERROR"), got)
        self.assertIn((103, "snapstate", "ERROR"), got)
        running = P.run_checks([locked], args, NOW, [{"id": "102"}])
        self.assertEqual([(f.check, f.severity) for f in running], [("lock", "INFO")])

    def test_orphans(self):
        class LocalBackend:
            local = True

        items = [P.StorageSnap("zfs snapshot", "r/vm-100-disk-0@a", 100, "a", False),
                 P.StorageSnap("zfs snapshot", "r/vm-100-disk-0@old", 100, "old", False),
                 P.StorageSnap("zfs state volume", "r/vm-100-state-b", 100, "b", True),
                 P.StorageSnap("zfs snapshot", "r/vm-555-disk-0@q", 555, "q", False)]
        args = P.build_parser().parse_args(["check", "--orphans"])
        got = P.check_orphans(LocalBackend(), [guest(100)], {100}, args, scanner=lambda: (items, [], True))
        self.assertEqual(sorted(f.detail.split(":")[0] for f in got),
                         ["zfs snapshot r/vm-100-disk-0@old", "zfs snapshot r/vm-555-disk-0@q",
                          "zfs state volume r/vm-100-state-b"])


class EndToEnd(StateCase):
    tool = "pvesnap"

    def fixture(self):
        return fixture()

    def deletes(self):
        return [c[1].rsplit("/", 1)[-1] for c in self.calls("delete")]

    def test_list_tree(self):
        rc, out, err = self.run_cli("list")
        self.assertEqual(rc, 1, err)  # guest 104 unreadable
        self.assertIn("└─ NOW", out)
        self.assertIn("ERROR: pvesh get /nodes/pve3/qemu/104/snapshot: hostname lookup failed", out)
        self.assertIn("LOCK=snapshot-delete", out)  # locked guest shown despite 0 snapshots
        self.assertTrue(out.endswith("5 guest(s), 11 snapshot(s)\n"), out)
        rc, out, _ = self.run_cli("list", "100,102", "--type", "qemu", "--name", "^web")
        self.assertTrue(out.endswith("1 guest(s), 5 snapshot(s)\n"), out)
        rc, out, _ = self.run_cli("list", "100", "--json")
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual([s["name"] for s in data[0]["snapshots"]], ["a", "b", "x", "c", "y"])
        self.assertEqual(data[0]["chain_depth"], 3)

    def test_check(self):
        rc, out, _ = self.run_cli("check", "--json")
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertEqual(data["summary"]["guests"], 5)
        self.assertEqual(data["summary"]["error"], 3)  # 103 parent + snapstate, 104 fetch
        rc, out, _ = self.run_cli("check", "101", "--max-age", "0", "--max-depth", "0")
        self.assertEqual(rc, 0, out)

    def test_dry_run_changes_nothing(self):
        rc, out, _ = self.run_cli("delete", "all", "--older-than", "30d", "--dry-run")
        self.assertEqual(rc, 1)  # 104 unreadable
        self.assertIn("plan: delete 3 snapshot(s) on 2 guest(s)", out)
        self.assertIn("DEL a", out)
        self.assertEqual(self.deletes(), [])

    def test_delete_chain_oldest_first(self):
        rc, out, err = self.run_cli("delete", "100", "--upto", "b", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.deletes(), ["a", "b"])
        st = self.read()
        self.assertEqual(sorted(s["name"] for s in st["snapshots"]["100"]), ["c", "x", "y"])
        self.assertTrue(all("parent" not in s for s in st["snapshots"]["100"] if s["name"] in ("c", "x")))
        self.assertIn("deleted 2, failed 0, skipped 0", out)

    def test_failure_stops_guest(self):
        st = fixture()
        st["fail"] = {"101/auto-2": "zfs error: dataset is busy"}
        self.write(st)
        rc, out, _ = self.run_cli("delete", "101", "--match", "^auto", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual(self.deletes(), ["auto-1", "auto-2"])
        self.assertIn("FAILED 'auto-2': task failed: zfs error: dataset is busy", out)
        self.assertIn("deleted 1, failed 1, skipped 2", out)
        rc, out, _ = self.run_cli("delete", "101", "auto-2", "--force", "-y")
        self.assertEqual(rc, 0, out)

    def test_locked_guest_skipped(self):
        st = fixture()
        st["locks"] = {"100": "backup"}
        self.write(st)
        rc, out, _ = self.run_cli("delete", "100", "a", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("SKIP guest: locked 'backup'", out)
        self.assertEqual(self.deletes(), [])

    def test_guards(self):
        rc, _, err = self.run_cli("delete", "100")
        self.assertEqual(rc, 2)
        self.assertIn("nothing selected", err)
        rc, _, err = self.run_cli("delete", "100", "a")
        self.assertEqual(rc, 2)
        self.assertIn("pass --yes", err)
        rc, _, err = self.run_cli("delete", "100", "nope", "-y")
        self.assertEqual(rc, 2)
        self.assertIn("snapshot(s) not found: nope", err)
        rc, _, err = self.run_cli("delete", "999", "--all", "-y")
        self.assertEqual(rc, 2)
        self.assertEqual(self.deletes(), [])


if __name__ == "__main__":
    unittest.main()
