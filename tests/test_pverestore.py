# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann

import json
import os
import shutil
import subprocess
import unittest

from helpers import FAKE_PVESM, StateCase, load_tool

R = load_tool("pverestore")

HAVE_QEMU = bool(shutil.which("qemu-img") and shutil.which("qemu-io"))
MB = 1024 * 1024

CONFIG = {
    "name": "web01", "memory": 4096, "cores": 4, "bios": "ovmf", "machine": "q35", "scsihw": "virtio-scsi-single",
    "vmgenid": "11111111-2222-3333-4444-555555555555", "onboot": 1, "cipassword": "**********",
    "description": "prod web", "hostpci0": "0000:01:00.0", "usb0": "host=1234:5678", "usb1": "spice",
    "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1", "net1": "virtio=BC:24:11:AA:BB:CD,bridge=vmbr1,link_down=0",
    "scsi0": "nfs:100/vm-100-disk-0.qcow2,discard=on,iothread=1,size=8M",
    "scsi1": "nfs:100/vm-100-disk-1.raw,size=4M",
    "scsi2": "/dev/disk/by-id/ata-X,size=1G",
    "ide2": "local:iso/debian.iso,media=cdrom,size=600M", "ide0": "nfs:100/vm-100-cloudinit.qcow2,media=cdrom",
    "unused0": "nfs:100/vm-100-disk-9.qcow2", "boot": "order=scsi0;ide2;net0;scsi2", "digest": "abc",
}


class ConfigTests(unittest.TestCase):
    def test_split_config(self):
        create, late, disks, notes = R.split_config(CONFIG)
        self.assertEqual(sorted(create), ["bios", "cores", "machine", "memory", "net0", "net1", "scsihw", "usb1"])
        self.assertEqual(create["net0"], "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1,link_down=1")
        self.assertEqual(create["net1"], "virtio=BC:24:11:AA:BB:CD,bridge=vmbr1,link_down=1")
        self.assertEqual(late, {"ide0": "nfs:cloudinit,media=cdrom", "ide2": "local:iso/debian.iso,media=cdrom,size=600M"})
        self.assertEqual([(d.key, d.sid, d.name, d.opts) for d in disks],
                         [("scsi0", "nfs", "vm-100-disk-0.qcow2", ["discard=on", "iothread=1"]),
                          ("scsi1", "nfs", "vm-100-disk-1.raw", [])])
        self.assertEqual(sorted(n.split(" ")[0] for n in notes), ["cipassword", "hostpci0", "scsi2", "usb0"])
        self.assertEqual(R.split_config(CONFIG, "other")[1]["ide0"], "other:cloudinit,media=cdrom")

    def test_boot_params(self):
        present = {"scsi0", "ide2", "net0"}
        self.assertEqual(R.boot_params(CONFIG, present), {"boot": "order=scsi0;ide2;net0"})
        self.assertEqual(R.boot_params({"boot": "order=scsi5"}, present), {})
        self.assertEqual(R.boot_params({"boot": "cdn", "bootdisk": "scsi0"}, present), {"boot": "cdn", "bootdisk": "scsi0"})
        self.assertEqual(R.boot_params({"bootdisk": "virtio0"}, present), {})

    def test_image_format_is_never_probed(self):
        self.assertEqual([R.image_format(n) for n in ("vm-1-disk-0.qcow2", "vm-1-disk-0.raw", "vm-1-disk-0", "a.vmdk")],
                         ["qcow2", "raw", "raw", "vmdk"])


def qemu(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def write(path, byte, fmt="qcow2"):
    qemu("qemu-io", "-f", fmt, "-c", f"write -P {byte} 0 1M", path)


def first_byte(path, fmt):
    raw = path + ".check"
    qemu("qemu-img", "convert", "-f", fmt, "-O", "raw", path, raw)
    with open(raw, "rb") as f:
        b = f.read(1)
    os.unlink(raw)
    return b[0]


def info(path, fmt="qcow2"):
    out = subprocess.run(["qemu-img", "info", "--output=json", "-f", fmt, path], check=True, stdout=subprocess.PIPE)
    return json.loads(out.stdout)


@unittest.skipUnless(HAVE_QEMU, "needs qemu-img and qemu-io (qemu-utils)")
class RestoreTests(StateCase):
    tool = "pverestore"
    extra_args = ("--pvesm", FAKE_PVESM, "--local-node", "pve1")

    def fixture(self):
        nfs = os.path.join(self.tmp.name, "nfs")
        # daily.0: internal snapshot 'before' holds 0x11, active state 0x22; second disk is raw with a qcow2 header
        d = os.path.join(nfs, ".snapshot", "daily.0", "images", "100")
        os.makedirs(d)
        disk0 = os.path.join(d, "vm-100-disk-0.qcow2")
        qemu("qemu-img", "create", "-q", "-f", "qcow2", disk0, "8M")
        write(disk0, "0x11")
        qemu("qemu-img", "snapshot", "-c", "before", disk0)
        write(disk0, "0x22")
        qemu("qemu-img", "create", "-q", "-f", "qcow2", os.path.join(d, "vm-100-disk-1.raw"), "64M")
        qemu("qemu-img", "create", "-q", "-f", "qcow2", os.path.join(d, "vm-100-disk-7.qcow2"), "1M")
        # chain.0: volume chain, snap-s1 holds 0x33, overlay 0x44
        c = os.path.join(nfs, ".snapshot", "chain.0", "images", "100")
        os.makedirs(c)
        base = os.path.join(c, "snap-s1-vm-100-disk-0.qcow2")
        qemu("qemu-img", "create", "-q", "-f", "qcow2", base, "8M")
        write(base, "0x33")
        top = os.path.join(c, "vm-100-disk-0.qcow2")
        qemu("qemu-img", "create", "-q", "-f", "qcow2", "-b", "snap-s1-vm-100-disk-0.qcow2", "-F", "qcow2", top)
        write(top, "0x44")
        shutil.copy(os.path.join(d, "vm-100-disk-1.raw"), c)
        # mixed.0: overlay whose backing file lives outside the filer snapshot
        m = os.path.join(nfs, ".snapshot", "mixed.0", "images", "100")
        os.makedirs(m)
        qemu("qemu-img", "create", "-q", "-f", "qcow2", "-b", base, "-F", "qcow2", os.path.join(m, "vm-100-disk-0.qcow2"))
        shutil.copy(os.path.join(d, "vm-100-disk-1.raw"), m)
        os.makedirs(os.path.join(nfs, "images"))
        self.nfs, self.daily = nfs, d
        return {
            "resources": [{"type": "qemu", "vmid": 100, "node": "pve2", "name": "web01", "status": "running"},
                          {"type": "qemu", "vmid": 300, "node": "pve1", "name": "used", "status": "stopped"}],
            "storage_cfg": [{"storage": "nfs", "type": "nfs", "path": nfs, "content": "images"},
                            {"storage": "isos", "type": "dir", "path": os.path.join(self.tmp.name, "isos"),
                             "content": "iso"},
                            {"storage": "local-lvm", "type": "lvmthin", "content": "images"}],
            "configs": {"100": {"config": dict(CONFIG)}},
        }

    def target(self, n, fmt="qcow2", vmid=9100):
        return os.path.join(self.nfs, "images", str(vmid), f"vm-{vmid}-disk-{n}.{fmt}")

    def test_sources(self):
        rc, out, _ = self.run_cli("sources", "100", "--json")
        self.assertEqual(rc, 0)
        got = {s["snapshot"]: (s["storage"], s["disks"], s["chain_files"]) for s in json.loads(out)["sources"]}
        self.assertEqual(got, {"daily.0": ("nfs", 3, 0), "chain.0": ("nfs", 2, 1), "mixed.0": ("nfs", 2, 0)})
        rc, out, _ = self.run_cli("sources", "777")
        self.assertEqual(rc, 1)
        self.assertIn("no filer snapshot with images of 777", out)

    def test_dry_run_changes_nothing(self):
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "-n")
        self.assertEqual(rc, 0, err)
        self.assertIn("scsi0: snapshots in the source (--snapshot NAME): before", out)
        self.assertIn("not in the guest config, not restored: vm-100-disk-7.qcow2", out)
        self.assertIn("dry run, nothing changed", out)
        self.assertEqual(self.calls("create"), [])
        self.assertFalse(os.path.exists(os.path.join(self.nfs, "images", "9100")))

    def test_restore_active_state(self):
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "-y")
        self.assertEqual(rc, 0, out + err)
        cfg = self.read()["configs"]["9100"]["config"]
        self.assertEqual(cfg["scsi0"], "nfs:9100/vm-9100-disk-0.qcow2,discard=on,iothread=1")
        self.assertEqual(cfg["scsi1"], "nfs:9100/vm-9100-disk-1.raw")
        self.assertEqual(cfg["net0"], "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1,link_down=1")
        self.assertEqual(cfg["boot"], "order=scsi0;ide2;net0")
        self.assertEqual(cfg["ide0"], "nfs:cloudinit,media=cdrom")
        self.assertEqual((cfg["name"], cfg["bios"], cfg["machine"], cfg["memory"]), ("restore-web01", "ovmf", "q35", "4096"))
        self.assertTrue(cfg["description"].endswith("source VM 100.\n\nprod web"))
        for key in ("vmgenid", "onboot", "cipassword", "hostpci0", "usb0", "scsi2", "unused0", "digest"):
            self.assertNotIn(key, cfg)
        self.assertEqual(first_byte(self.target(0), "qcow2"), 0x22)
        self.assertNotIn("snapshots", info(self.target(0)))
        # the raw disk holds a qcow2 header: it must be copied as bytes, not interpreted
        src = os.path.join(self.daily, "vm-100-disk-1.raw")
        self.assertEqual(os.path.getsize(self.target(1, "raw")) // 1024, -(-os.path.getsize(src) // 1024))
        with open(self.target(1, "raw"), "rb") as f:
            self.assertEqual(f.read(4), b"QFI\xfb")
        # source untouched
        self.assertEqual(first_byte(os.path.join(self.daily, "vm-100-disk-0.qcow2"), "qcow2"), 0x22)
        self.assertEqual(len(info(os.path.join(self.daily, "vm-100-disk-0.qcow2"))["snapshots"]), 1)

    def test_restore_internal_snapshot(self):
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "--snapshot", "before",
                                    "--skip-missing", "-y")
        self.assertEqual(rc, 2, out + err)  # scsi1 is raw: no such snapshot, not a missing file
        self.assertIn("no snapshot 'before' (has: none)", out)
        self.assertEqual(self.calls("create"), [])
        st = self.read()
        del st["configs"]["100"]["config"]["scsi1"]
        self.write(st)
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "--snapshot", "before", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(first_byte(self.target(0), "qcow2"), 0x11)
        self.assertNotIn("snapshots", info(self.target(0)))

    def test_restore_volume_chain(self):
        rc, out, err = self.run_cli("vm", "100", "--from", "chain.0", "--as", "9100", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("chain of 2 files", out)
        self.assertEqual(first_byte(self.target(0), "qcow2"), 0x44)
        self.assertNotIn("backing-filename", info(self.target(0)))
        st = self.read()
        del st["configs"]["100"]["config"]["scsi1"]
        self.write(st)
        rc, out, err = self.run_cli("vm", "100", "--from", "chain.0", "--as", "9101", "--snapshot", "s1", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(first_byte(self.target(0, vmid=9101), "qcow2"), 0x33)

    def test_refusals(self):
        rc, out, err = self.run_cli("vm", "100", "--from", "mixed.0", "--as", "9100", "-y")
        self.assertEqual(rc, 2)
        self.assertIn("is outside the source", out)
        self.assertIn("result would mix two points in time", out)
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "300", "-y")
        self.assertIn("--as 300: guest id is in use", err)
        rc, _, err = self.run_cli("vm", "100", "--from", "nope", "--as", "9100", "-y")
        self.assertIn("filer snapshot 'nope' holds no images of 100", err)
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "--storage", "local-lvm", "-y")
        self.assertIn("--storage local-lvm: not a file storage", err)
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100")
        self.assertIn("stdin is not a terminal", err)
        self.assertEqual(self.calls("create"), [])

    def test_missing_disk(self):
        st = self.read()
        st["configs"]["100"]["config"]["scsi3"] = "nfs:100/vm-100-disk-3.qcow2,size=1G"
        self.write(st)
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "-y")
        self.assertEqual(rc, 2)
        self.assertIn("--skip-missing", err)
        self.assertEqual(self.calls("create"), [])
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "--skip-missing", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("scsi3", self.read()["configs"]["9100"]["config"])

    def test_no_config_from_directory(self):
        rc, out, err = self.run_cli("vm", "555", "--from", self.daily, "--as", "9100", "-y")
        self.assertEqual(rc, 2)  # the directory holds images of 100, none of 555
        self.assertIn("no disk to restore", err)
        rc, out, err = self.run_cli("vm", "100", "--from", self.daily, "--as", "9100", "--no-config", "-y")
        self.assertEqual(rc, 2)
        self.assertIn("no target storage known: use --storage", out)
        rc, out, err = self.run_cli("vm", "100", "--from", self.daily, "--as", "9100", "--no-config",
                                    "--storage", "nfs", "-y")
        self.assertEqual(rc, 0, out + err)
        cfg = self.read()["configs"]["9100"]["config"]
        self.assertEqual([cfg[k] for k in ("scsi0", "scsi1", "scsi2")],
                         ["nfs:9100/vm-9100-disk-0.qcow2", "nfs:9100/vm-9100-disk-1.raw", "nfs:9100/vm-9100-disk-2.qcow2"])
        self.assertEqual((cfg["name"], cfg["boot"], cfg["scsihw"]), ("restore-100", "order=scsi0", "virtio-scsi-single"))
        self.assertNotIn("net0", cfg)

    def local_guest(self, status="stopped", **extra):
        st = self.read()
        st["resources"][0].update(node="pve1", status=status, **extra)
        st["vm_status"] = {"100": status}
        self.write(st)

    def test_replace(self):
        self.local_guest()
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-n")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("REPLACE the disks of VM 100 on pve1 in place", out)
        self.assertEqual(self.calls("create") + self.calls("set"), [])
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-y")
        self.assertEqual(rc, 0, out + err)
        cfg = self.read()["configs"]["100"]["config"]
        # indexes 0, 1 and 9 are taken by the config
        self.assertEqual(cfg["scsi0"], "nfs:100/vm-100-disk-2.qcow2,discard=on,iothread=1")
        self.assertEqual(cfg["scsi1"], "nfs:100/vm-100-disk-3.raw")
        self.assertEqual({cfg[k] for k in ("unused0", "unused1", "unused2")},
                         {"nfs:100/vm-100-disk-9.qcow2", "nfs:100/vm-100-disk-0.qcow2", "nfs:100/vm-100-disk-1.raw"})
        self.assertEqual(cfg["vmgenid"], "1")
        for key in ("name", "net0", "net1", "hostpci0", "boot", "ide0", "ide2", "onboot", "scsi2"):
            self.assertEqual(cfg[key], CONFIG[key], key)
        self.assertEqual(first_byte(os.path.join(self.nfs, "images", "100", "vm-100-disk-2.qcow2"), "qcow2"), 0x22)
        self.assertEqual([c[1] for c in self.calls("create")], ["/nodes/pve1/storage/nfs/content"] * 2)
        self.assertIn("scsi0: nfs:100/vm-100-disk-2.qcow2 (was nfs:100/vm-100-disk-0.qcow2, now unusedN)", out)

    def test_replace_skips_used_file_indexes(self):
        self.local_guest()
        d = os.path.join(self.nfs, "images", "100")
        os.makedirs(d)
        for name in ("vm-100-disk-2.qcow2", "snap-old-vm-100-disk-3.qcow2"):
            open(os.path.join(d, name), "w").close()
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-y")
        self.assertEqual(rc, 0, out + err)
        cfg = self.read()["configs"]["100"]["config"]
        self.assertEqual((cfg["scsi0"].split(",")[0], cfg["scsi1"]),
                         ("nfs:100/vm-100-disk-4.qcow2", "nfs:100/vm-100-disk-5.raw"))

    def test_replace_running_vm(self):
        self.local_guest("running")
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-y")
        self.assertEqual(rc, 2)
        self.assertIn("VM 100 is running: stop it, or pass --shutdown", err)
        self.assertEqual(self.calls("create"), [])
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "--shutdown", "-y")
        self.assertEqual(rc, 0, out + err)
        paths = [c[1] for c in self.calls("create")]
        self.assertEqual(paths[-1], "/nodes/pve1/qemu/100/status/shutdown")  # after both copies
        self.assertEqual(len(paths), 3)
        self.assertEqual(self.read()["configs"]["100"]["config"]["scsi0"].split(",")[0], "nfs:100/vm-100-disk-2.qcow2")

    def test_replace_shutdown_fails(self):
        self.local_guest("running")
        st = self.read()
        st["fail_shutdown"] = "VM quit/powerdown failed - got timeout"
        self.write(st)
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "--shutdown", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("VM 100 is unchanged, its disks were not swapped", err)
        self.assertIn("nfs:100/vm-100-disk-2.qcow2, nfs:100/vm-100-disk-3.raw", err)
        self.assertEqual(self.calls("set"), [])
        self.assertEqual(self.read()["configs"]["100"]["config"]["scsi0"], CONFIG["scsi0"])

    def test_replace_fails_after_detach(self):
        self.local_guest()
        st = self.read()
        st["fail_set"] = "boom"
        self.write(st)
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("old disks are detached as unusedN, the restored volumes are NOT attached", err)
        self.assertIn("qm set 100 --scsi0 nfs:100/vm-100-disk-2.qcow2,discard=on,iothread=1", err)
        self.assertIn("qm set 100 --scsi1 nfs:100/vm-100-disk-3.raw", err)

    def test_replace_refusals(self):
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-y")
        self.assertIn("VM 100 is on node pve2, run pverestore there", err)
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "-y")
        self.assertIn("pass exactly one of --as NEWID", err)
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "--replace", "-y")
        self.assertIn("pass exactly one of --as NEWID", err)
        rc, _, err = self.run_cli("vm", "777", "--from", "daily.0", "--replace", "-y")
        self.assertIn("VM 777 does not exist", err)
        self.local_guest(lock="backup")
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "-y")
        self.assertIn("VM 100 is locked (backup)", err)
        rc, _, err = self.run_cli("vm", "100", "--from", "daily.0", "--replace", "--no-config", "-y")
        self.assertIn("not with --no-config", err)
        self.assertEqual(self.calls("create") + self.calls("set"), [])

    def test_failure_reports_leftovers(self):
        st = self.read()
        st["fail_set"] = "boom"
        self.write(st)
        rc, out, err = self.run_cli("vm", "100", "--from", "daily.0", "--as", "9100", "-y")
        self.assertEqual(rc, 1)
        self.assertIn("restore failed", err)
        self.assertIn("nfs:9100/vm-9100-disk-0.qcow2, nfs:9100/vm-9100-disk-1.raw", err)
        self.assertIn("qm destroy 9100 --purge", err)


if __name__ == "__main__":
    unittest.main()
