# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import fcntl
import glob
import os
import random
import stat
import time
import unittest

from helpers import StateCase, load_tool

P = load_tool("pvesnap")

NOW = int(time.time())
DAY = 86400

MAIN = "boot: order=scsi0\ncores: 2\nscsi0: local-zfs:vm-100-disk-0,size=32G\n"


def section(name, days, parent=None, **kw):
    lines = [f"[{name}]"]
    if parent:
        lines.append(f"parent: {parent}")
    lines += ["scsi0: local-zfs:vm-100-disk-0,size=32G", f"snaptime: {NOW - days * DAY}"]
    lines += [f"{k}: {v}" for k, v in kw.items()]
    return "\n".join(lines) + "\n"


def conf(main_extra="", *sections):
    return MAIN + main_extra + "".join("\n" + s for s in sections)


# aa -> bb -> cc, aa's parent closed into a loop, live state inside the loop, cc stuck
LOOP = conf("lock: snapshot-delete\nparent: cc\n",
            section("aa", 30, "cc"), section("bb", 20, "aa"), section("cc", 10, "bb", snapstate="delete"))


def tree(parents, times=None, current=None):
    entries = [{"name": n, "parent": p, "snaptime": (times or {}).get(n, 1000 + i)}
               for i, (n, p) in enumerate(parents.items())]
    entries.append({"name": "current", "parent": current})
    return P.build_tree(P.Guest(1, "n", "qemu"), entries)


class ConfParseTests(unittest.TestCase):
    def test_roundtrip_is_byte_exact(self):
        text = "#note\n#second\nboot: c\nparent: aa\n\n[PENDING]\ncores: 4\n\n[aa]\nparent: bb\n\n" \
               "[special:cloudinit]\nipconfig0: ip=dhcp\n\n[bb]\nsnaptime: 1\n"
        self.assertEqual(P.render_conf(P.parse_conf_sections(text)), text)
        self.assertEqual(P.render_conf(P.parse_conf_sections("a: 1")), "a: 1")

    def test_duplicate_section_refused(self):
        with self.assertRaisesRegex(P.PveError, "duplicate section"):
            P.parse_conf_sections("a: 1\n\n[aa]\nb: 1\n\n[aa]\nb: 2\n")

    def test_only_real_snapshot_sections_count(self):
        secs = P.parse_conf_sections("a: 1\n\n[PENDING]\nb: 1\n\n[special:cloudinit]\nc: 1\n\n[pve:pending]\nd: 1\n\n"
                                     "[aa]\nsnaptime: 5\nvmstate: local:vm-100-state-aa\n")
        entries = P.conf_entries(secs)
        self.assertEqual([e["name"] for e in entries], ["aa", "current"])
        self.assertEqual(entries[0]["vmstate"], 1)

    def test_conf_set(self):
        lines = ["a: 1", "parent: old", "b: 2", "", ""]
        P.conf_set(lines, "parent", "new")
        self.assertEqual(lines, ["a: 1", "parent: new", "b: 2", "", ""])
        P.conf_set(lines, "parent", None)
        self.assertEqual(lines, ["a: 1", "b: 2", "", ""])
        P.conf_set(lines, "parent", "x")
        self.assertEqual(lines, ["a: 1", "b: 2", "parent: x", "", ""])
        self.assertEqual(P.conf_get(["parent: a", "parent: b"], "parent"), "b")  # PVE parser: last wins


class PlanParentsTests(unittest.TestCase):
    def test_healthy_tree_untouched(self):
        self.assertEqual(P.plan_parents(tree({"aa": None, "bb": "aa", "cc": "bb"}, current="cc")), {})

    def test_two_loop_cuts_oldest(self):
        self.assertEqual(P.plan_parents(tree({"aa": "bb", "bb": "aa"}, current="bb")), {"aa": None})

    def test_self_loop(self):
        self.assertEqual(P.plan_parents(tree({"aa": None, "bb": "bb"})), {"bb": "aa"})

    def test_loop_with_branch_hanging_off(self):
        got = P.plan_parents(tree({"aa": "cc", "bb": "aa", "cc": "bb", "dd": "bb"}))
        self.assertEqual(got, {"aa": None})

    def test_cut_snapshot_rejoins_older_tree(self):
        got = P.plan_parents(tree({"aa": None, "bb": "aa", "cc": "dd", "dd": "cc"}))
        self.assertEqual(got, {"cc": "bb"})

    def test_dangling_reattached_by_time(self):
        got = P.plan_parents(tree({"aa": None, "bb": "aa", "cc": "gone", "dd": "cc"}, current="dd"))
        self.assertEqual(got, {"cc": "bb"})
        self.assertEqual(P.plan_parents(tree({"aa": "gone"})), {"aa": None})

    def test_dangling_never_attaches_to_own_subtree(self):
        got = P.plan_parents(tree({"aa": "gone", "bb": "aa"}, times={"aa": 2000, "bb": 1000}))
        self.assertEqual(got, {"aa": None})

    def test_no_timestamp_is_never_older(self):
        got = P.plan_parents(tree({"aa": None, "bb": "gone"}, times={"aa": 0, "bb": 1000}))
        self.assertEqual(got, {"bb": None})

    def test_live_state_dangling_goes_to_newest(self):
        got = P.plan_parents(tree({"aa": None, "bb": "aa"}, current="gone"))
        self.assertEqual(got, {"": "bb"})
        self.assertEqual(P.plan_parents(tree({}, current="gone")), {"": None})

    def test_fixes_are_ordered_oldest_first(self):
        got = P.plan_parents(tree({"zz": "gone", "mm": "gone", "aa": "gone", "bb": "bb"}, current="gone"))
        self.assertEqual(list(got), ["zz", "mm", "aa", "bb", ""])

    def test_live_state_without_parent_is_legal(self):
        self.assertEqual(P.plan_parents(tree({"aa": None})), {})

    def test_any_graph_ends_as_forest(self):
        rng = random.Random(1)
        for _ in range(500):
            names = [f"s{i}" for i in range(rng.randint(1, 8))]
            targets = names + ["gone", None]
            parents = {n: rng.choice(targets) for n in names}
            times = {n: rng.choice([0, 1000, 2000, 3000]) for n in names}
            g = tree(parents, times, current=rng.choice(targets))
            fixes = P.plan_parents(g)
            entries = [{"name": n, "parent": fixes.get(n, p), "snaptime": times[n]} for n, p in parents.items()]
            entries.append({"name": "current", "parent": fixes.get("", g.current_parent)})
            fixed = P.build_tree(P.Guest(1, "n", "qemu"), entries)
            self.assertEqual((fixed.broken, fixed.cyclic), ([], []), parents)
            self.assertTrue(fixed.current_parent is None or fixed.current_parent in fixed.snaps, parents)


class RenderTests(unittest.TestCase):
    def test_now_visible_inside_loop(self):
        g = tree({"aa": "bb", "bb": "aa"}, current="bb")
        self.assertIn(P.NOW_NODE, [n for _, n in P.tree_rows(g)])


class WriteTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "100.conf")
        self.lock = os.path.join(self.tmp.name, "lock-100.conf")
        with open(self.path, "wb") as f:
            f.write(b"a: 1\n")

    def content(self):
        with open(self.path, "rb") as f:
            return f.read()

    def test_refuses_when_file_changed_since_read(self):
        with self.assertRaisesRegex(P.PveError, "changed since"):
            P.replace_conf(self.path, b"a: stale\n", b"a: 2\n", self.lock)
        self.assertEqual(self.content(), b"a: 1\n")

    def test_replace_leaves_no_temp_file(self):
        P.replace_conf(self.path, b"a: 1\n", b"a: 2\n", self.lock)
        self.assertEqual(self.content(), b"a: 2\n")
        self.assertEqual(glob.glob(self.path + ".tmp.*"), [])

    def test_held_config_lock_times_out(self):
        with open(self.lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaisesRegex(P.PveError, "config lock"):
                P.replace_conf(self.path, b"a: 1\n", b"a: 2\n", self.lock, timeout=0.1)
        self.assertEqual(self.content(), b"a: 1\n")


class RepairCase(StateCase):
    tool = "pvesnap"
    vmid, gtype, subdir = "100", "qemu", "qemu-server"

    def setUp(self):
        super().setUp()
        root = self.tmp.name
        self.pve = os.path.join(root, "pve")
        self.backups = os.path.join(root, "backups")
        os.makedirs(os.path.join(self.pve, "nodes", "pve1", self.subdir))
        os.symlink("nodes/pve1", os.path.join(self.pve, "local"))
        self.conf = os.path.join(self.pve, "nodes", "pve1", self.subdir, f"{self.vmid}.conf")
        self.extra_args = ("--pve-dir", self.pve, "--lock-dir", os.path.join(root, "locks"),
                           "--backup-dir", self.backups)
        self.set_state()

    def set_state(self, node="pve1", **extra):
        st = {"resources": [{"type": self.gtype, "vmid": int(self.vmid), "node": node, "name": "web01",
                             "status": "running"}],
              "conf_files": {self.vmid: self.conf}, "storage_cfg": [], "node_tasks": {}}
        st.update(extra)
        self.write(st)

    def put(self, text):
        with open(self.conf, "w") as f:
            f.write(text)

    def get(self):
        with open(self.conf) as f:
            return f.read()

    def repair(self, *argv):
        return self.run_cli("repair", self.vmid, *argv)

    def backup_files(self):
        return glob.glob(os.path.join(self.backups, "*"))


class RepairEndToEnd(RepairCase):
    def test_the_loop_is_real_without_repair(self):
        self.put(LOOP)
        rc, out, _ = self.run_cli_delete("cc")
        self.assertEqual(rc, 1)
        self.assertIn("SKIP guest: locked", out)

    def run_cli_delete(self, name):
        cmd = [a for a in self.extra_args]
        self.extra_args = ()
        try:
            return self.run_cli("delete", self.vmid, name, "--force", "-y")
        finally:
            self.extra_args = tuple(cmd)

    def test_repairs_loop_lock_and_stuck_snapshot(self):
        self.put(LOOP)
        rc, out, err = self.repair("-y")
        self.assertEqual(rc, 0, out + err)
        secs = P.parse_conf_sections(self.get())
        self.assertNotIn("cc", secs)
        self.assertIsNone(P.conf_get(secs[""], "lock"))
        self.assertIsNone(P.conf_get(secs["aa"], "parent"))
        self.assertEqual(P.conf_get(secs["bb"], "parent"), "aa")
        self.assertEqual(P.conf_get(secs[""], "parent"), "bb")  # relinked by the force delete
        self.assertEqual([c[2] for c in self.calls("delete")], [{"force": "1"}])
        self.assertIn("repaired", out)
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to repair", out)

    def test_write_keeps_foreign_lines_and_backs_up(self):
        text = "#note %C3%A4\n" + conf("lock: snapshot\nparent: gone\n", section("aa", 10)) \
               + "\n[PENDING]\ncores: 4\n\n[special:cloudinit]\nipconfig0: ip=dhcp\n"
        self.put(text)
        rc, out, err = self.repair("-y")
        self.assertEqual(rc, 0, out + err)
        want = text.replace("lock: snapshot\n", "").replace("parent: gone\n", "parent: aa\n")
        self.assertEqual(self.get(), want)
        backups = self.backup_files()
        self.assertEqual(len(backups), 1)
        with open(backups[0]) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(stat.S_IMODE(os.stat(backups[0]).st_mode), 0o600)

    def test_dry_run_changes_nothing(self):
        self.put(LOOP)
        rc, out, _ = self.repair("-n")
        self.assertEqual(rc, 0, out)
        self.assertIn("dry run", out)
        self.assertIn("clear 'snapshot-delete'", out)
        self.assertIn("DEL", out)
        self.assertEqual(self.get(), LOOP)
        self.assertEqual(self.calls("delete"), [])
        self.assertEqual(self.backup_files(), [])

    def test_healthy_guest_untouched(self):
        text = conf("parent: bb\n", section("aa", 20), section("bb", 10, "aa"))
        self.put(text)
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to repair", out)
        self.assertEqual(self.get(), text)
        self.assertEqual(self.backup_files(), [])

    def test_confirmation_required_without_tty(self):
        self.put(LOOP)
        rc, _, err = self.repair()
        self.assertEqual(rc, 2)
        self.assertIn("pass --yes", err)
        self.assertEqual(self.get(), LOOP)

    def test_failed_force_delete_stops_and_hints_drop(self):
        text = conf("lock: snapshot-delete\nparent: bb\n", section("aa", 20), section("bb", 10, "aa", snapstate="delete"))
        self.put(text)
        self.set_state(fail_early={"100/bb": "storage 'gone' does not exist"})
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 1, out)
        self.assertIn("storage 'gone' does not exist", out)
        self.assertIn("--drop", out)
        self.assertIn("[bb]", self.get())

    def test_drop_removes_section_and_relinks(self):
        self.put(conf("lock: snapshot-delete\nparent: cc\n", section("aa", 30), section("bb", 20, "aa", snapstate="delete"),
                      section("cc", 10, "bb")))
        self.set_state(fail_early={"100/bb": "storage 'gone' does not exist"})
        rc, out, err = self.repair("-y", "--drop")
        self.assertEqual(rc, 0, out + err)
        secs = P.parse_conf_sections(self.get())
        self.assertNotIn("bb", secs)
        self.assertEqual(P.conf_get(secs["cc"], "parent"), "aa")
        self.assertIsNone(P.conf_get(secs[""], "lock"))
        self.assertEqual(len(self.backup_files()), 2)

    def test_drop_clears_lock_left_by_failed_task(self):
        self.put(conf("parent: aa\n", section("aa", 10, snapstate="prepare")))
        self.set_state(fail_late={"100/aa": "boom"})
        rc, out, err = self.repair("-y", "--drop")
        self.assertEqual(rc, 0, out + err)
        secs = P.parse_conf_sections(self.get())
        self.assertNotIn("aa", secs)
        self.assertIsNone(P.conf_get(secs[""], "lock"))
        self.assertIsNone(P.conf_get(secs[""], "parent"))

    def test_failure_stops_the_rest_without_drop(self):
        self.put(conf("parent: bb\n", section("aa", 20, snapstate="delete"), section("bb", 10, "aa", snapstate="delete")))
        self.set_state(fail_late={"100/aa": "boom"})
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 1, out)
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in self.calls("delete")], ["aa"])

    def test_drop_continues_with_the_rest(self):
        self.put(conf("parent: bb\n", section("aa", 20, snapstate="delete"), section("bb", 10, "aa", snapstate="delete")))
        self.set_state(fail_late={"100/aa": "boom"})
        rc, out, err = self.repair("-y", "--drop")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in self.calls("delete")], ["aa", "bb"])
        self.assertEqual(P.conf_entries(P.parse_conf_sections(self.get())), [{"name": "current", "parent": None}])

    def test_second_run_recovers_lock_left_by_failed_delete(self):
        text = conf("parent: aa\n", section("aa", 10, snapstate="prepare"))
        self.put(text)
        self.set_state(fail_late={"100/aa": "boom"})
        self.assertEqual(self.repair("-y")[0], 1)
        self.assertIn("lock: snapshot-delete", self.get())
        self.set_state()
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("[aa]", self.get())


class RepairGuards(RepairCase):
    def assert_refused(self, needle, *argv):
        before = self.get()
        rc, out, err = self.repair("-y", *argv)
        self.assertEqual(rc, 2, out + err)
        self.assertIn(needle, err)
        self.assertEqual(self.get(), before)
        self.assertEqual(self.calls("delete"), [])

    def test_running_task(self):
        self.put(LOOP)
        self.set_state(node_tasks={"pve1": [{"id": "100", "type": "qmdelsnapshot", "upid": "UPID:pve1:1"}]})
        self.assert_refused("running task")

    def test_ended_task_and_other_guest_ignored(self):
        self.put(LOOP)
        self.set_state(node_tasks={"pve1": [{"id": "100", "upid": "UPID:pve1:1", "endtime": NOW},
                                            {"id": "999", "upid": "UPID:pve1:2"}]})
        self.assertEqual(self.repair("-y")[0], 0)

    def test_tasks_unreadable(self):
        self.put(LOOP)
        self.set_state(errors={"/nodes/pve1/tasks": "boom"})
        self.assert_refused("cannot read running tasks")

    def test_foreign_lock(self):
        for lock in ("backup", "rollback", "migrate"):
            self.put(conf(f"lock: {lock}\nparent: gone\n"))
            self.assert_refused(f"locked '{lock}'")

    def test_other_node(self):
        self.put(LOOP)
        self.set_state(node="pve2")
        self.assert_refused("run repair on that node")

    def test_unknown_guest(self):
        self.put(LOOP)
        self.set_state(resources=[])
        self.assert_refused("not found")

    def test_duplicate_section(self):
        self.put(conf("", section("aa", 10), section("aa", 5)))
        self.assert_refused("duplicate section")

    def test_needs_local_backend(self):
        self.put(LOOP)
        rc, _, err = self.repair("-y", "--api-url", "https://127.0.0.1:1", "--token", "t")
        self.assertEqual(rc, 2)
        self.assertIn("run it on the guest's node", err)


class RepairLxc(RepairCase):
    vmid, gtype, subdir = "101", "lxc", "lxc"

    def test_container(self):
        self.put("arch: amd64\nlock: snapshot\nparent: gone\n\n[aa]\nparent: aa\nsnaptime: 5\n")
        rc, out, err = self.repair("-y")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.get(), "arch: amd64\nparent: aa\n\n[aa]\nsnaptime: 5\n")


class RepairReports(RepairCase):
    def test_volume_chain_storage_warned(self):
        self.put(conf("parent: aa\n", section("aa", 10)))
        self.set_state(storage_cfg=[{"storage": "local-zfs", "snapshot-as-volume-chain": 1},
                                    {"storage": "other", "snapshot-as-volume-chain": 1}])
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("local-zfs", out)
        self.assertNotIn("other", out)
        self.assertIn("backing-chain", out)

    def test_storage_leftovers_listed_not_deleted(self):
        self.put(conf("parent: aa\n", section("aa", 10)))
        bindir = os.path.join(self.tmp.name, "bin")
        os.mkdir(bindir)
        stub = os.path.join(bindir, "zfs")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\ncase \"$*\" in\n*snapshot*) echo rpool/data/vm-100-disk-0@aa; "
                    "echo rpool/data/vm-100-disk-0@ghost;;\nesac\n")
        os.chmod(stub, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = bindir + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        rc, out, _ = self.repair("-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("rpool/data/vm-100-disk-0@ghost", out)
        self.assertNotIn("disk-0@aa", out)
        self.assertIn("not touched", out)


if __name__ == "__main__":
    unittest.main()
