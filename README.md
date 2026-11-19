# proxmox-tools

Cluster-wide CLI tools for Proxmox VE. Each tool is one file, Python stdlib only (>= 3.9, PVE 7/8/9).

| Tool | Purpose |
|---|---|
| [`pvesnap`](#pvesnap) | snapshot trees, snapshot audit, delete chains/branches/by age, repair stuck or looping snapshot state |
| [`pvedisk`](#pvedisk) | find and delete orphaned, stale and unused guest volumes on all storages |
| [`pvebackup`](#pvebackup) | backup audit: job coverage, age, PBS verification, failed jobs, storage usage |
| [`pvenode`](#pvenode) | node audit: quorum, version drift, pending reboot, updates, certificates, ZFS, thin pools, SMART, clock skew, Ceph |
| [`pverestore`](#pverestore) | restore a VM from a filer snapshot or image directory into a new VM, flat, with a dry run |

## Install

```sh
scp pvesnap pvedisk pvebackup pvenode pverestore root@pve1:/usr/local/sbin/
```

| Backend | When | Config |
|---|---|---|
| `pvesh` (default) | on any cluster node as root | none |
| API | remote host | `PVE_API_URL=https://pve1:8006`, `PVE_TOKEN='root@pam!tools=<secret>'`, optional `PVE_INSECURE=1` |

| Tool | API token privileges |
|---|---|
| `pvesnap` | `VM.Audit`; delete: `VM.Snapshot`; repair: root on the guest's node, no API token |
| `pvedisk` | `VM.Audit`, `Datastore.Audit`, `Sys.Audit`; delete: `Datastore.Allocate`, `VM.Config.Disk` |
| `pvebackup` | `VM.Audit`, `Datastore.Audit`, `Sys.Audit` |
| `pvenode` | `Sys.Audit`, `Datastore.Audit` (Ceph detection); an unreadable endpoint is a `fetch` WARN |
| `pverestore` | root on a node, no API token |

Common: `GUESTS` = `100`, `100,101`, `200-210`, `all`. `check` exits 1 on WARN/ERROR (`--fail-on warn|error|never`), `-q` hides INFO, `--json` for monitoring. Delete commands: `-n` dry run, `-y` no prompt, refused without `-y` on non-tty stdin, Ctrl-C finishes the running operation then stops. Exit codes: 0 ok, 1 findings/failures, 2 usage or fatal error.

## pvesnap

| Command | Purpose |
|---|---|
| `pvesnap list [GUESTS]` | snapshot trees with age, RAM state, chain depth; `--flat`, `--json`, `-e` |
| `pvesnap check [GUESTS]` | findings table |
| `pvesnap delete GUESTS [SNAP...]` | plan with `DEL` marks in the tree, confirm, delete oldest first, wait per task, verify |
| `pvesnap repair VMID` | fix stale snapshot lock, parent loops, missing parents, stuck snapshots; see [repair](#repair) |

| Check | Severity | Default |
|---|---|---|
| `fetch` snapshot list unreadable (node offline) | ERROR | on |
| `parent` missing parent reference | ERROR | on, fix: `repair VMID` |
| `cycle` parent loop | ERROR | on, fix: `repair VMID` |
| `snapstate` half-finished create/delete | ERROR | on, fix: `repair VMID` |
| `lock` guest lock without running task | WARN | on, fix: `repair VMID` for snapshot locks, else `qm/pct unlock` |
| `age` | WARN | `--max-age 30d` |
| `depth` snapshots between live state and root | WARN | `--max-depth 5` |
| `count` per guest | WARN | `--max-count 0` (off) |
| `vmstate` snapshot holds RAM | INFO | on |
| `orphan` snapshot or state volume on storage but not in config: zfs, lvm-thin, lvm chain, qcow2 internal, qcow2 `snap-*` chain file | WARN | `--orphans`, local node only, ignores `__replicate_*`, qcow2 needs `qemu-img` (one call per image, 30 s timeout) |

Delete selection: base set = named snapshots + `--subtree` + `--upto`, or all snapshots if none given. Then `--match`/`--exclude`/`--older-than` narrow it, `--keep-last N` protects the newest N (counted among `--match`/`--exclude`).

| Example | Deletes |
|---|---|
| `pvesnap delete 100 pre-upgrade` | one snapshot |
| `pvesnap delete 100 --upto weekly-3` | `weekly-3` and every older snapshot in its chain |
| `pvesnap delete 100 --subtree test` | `test` and all its descendants |
| `pvesnap delete 100 --all` | every snapshot of 100 |
| `pvesnap delete all --older-than 30d -n` | dry run, cluster-wide |
| `pvesnap delete all --match '^auto' --keep-last 7 -y` | rotation (cron) |

| Safety | Behaviour |
|---|---|
| no selector | refused |
| single guest + unknown snapshot name | refused |
| multiple guests + snapshot name on none of them | refused |
| guest locked at start | guest skipped |
| task fails or times out (`--timeout 3600`) | rest of that guest skipped, other guests continue |
| after run | snapshot list re-read, leftovers reported as failed |
| `--parallel N` | N guests at once (default 1) |

### repair

A failed delete leaves `lock` and `snapstate=delete`, every retry then dies on the lock, and a parent loop empties the GUI tree including NOW. The API cannot edit these fields, so `repair` edits the config file and hands stuck snapshots back to PVE.

| Example | Action |
|---|---|
| `pvesnap repair 100 -n` | show tree, fixes and resulting tree |
| `pvesnap repair 100` | confirm, then repair |
| `pvesnap repair 100 --drop` | same, a snapshot whose force delete fails is removed from the config |
| `pvesnap repair 100 --drop --chain-ok` | same on `snapshot-as-volume-chain` storage, after verifying the chain by hand |

| Problem | Fix |
|---|---|
| `lock: snapshot` / `snapshot-delete` | removed |
| parent loop | cut at the oldest member, which rejoins the newest older snapshot outside its subtree |
| snapshot with missing parent | rejoins the newest older snapshot outside its subtree, else root |
| live state with missing parent | set to the newest snapshot |
| `snapstate=prepare` / `delete` | `DELETE snapshot` via API, oldest first, with `--force` except on volume chains |

| Safety | Behaviour |
|---|---|
| backend | pvesh only, on the guest's node: PVE's config lock is node-local |
| running task on the guest, or tasks unreadable | refused |
| other lock (`backup`, `rollback`, `migrate`, ...) | refused |
| before writing | config copied to `--backup-dir` (default `/var/backups/pvesnap`, mode 0600) |
| writing | under PVE's config flock, tmp file + rename, refused if the file changed since it was read |
| untouched | every line except `lock` and `parent`, including `[PENDING]` and `[special:*]` |
| force delete fails | rest waits, exit 1; a re-run clears the lock it left |
| `--drop` | only after a failed force delete; storage snapshot and vmstate volume stay |
| storage snapshots missing in the config | listed, never deleted: may belong to zfs-auto-snapshot or sanoid |
| `snapshot-as-volume-chain` storage | no `--force`, `--drop` refused without `--chain-ok`: force hides a chain that only QEMU memory still holds; chain itself is not repaired, check with `qemu-img info -U --backing-chain` |
| task ends with warnings | not reported as repaired, exit 1 |
| storage config unreadable | refused |

## pvedisk

Lists every `images`/`rootdir` volume on every storage (node-local per node, shared once) and matches it against all guest configs: current, `[PENDING]`, every snapshot section, linked-clone bases, replication targets.

| Kind | Severity | Meaning | `delete --kind` |
|---|---|---|---|
| `missing` | ERROR | config references a volume that is not on the storage | - |
| `orphan` | WARN | no config references it (guest gone, or not in its config) | default |
| `stale` | WARN | copy on another node's local storage, no replication there | opt-in |
| `config` | WARN | guest config unreadable, its volumes are not classified | - |
| `scan` | WARN/INFO | storage not listable / not available | - |
| `unused` | INFO | detached `unusedN` disk | opt-in, via config `delete unusedN` |
| `snapshot-only` | INFO | only referenced by snapshots, freed when they are deleted | - |
| `busy` | INFO | unreferenced, but guest locked or task running | - |
| `alias` | INFO | same volume under another storage ID with the same backing (pool, VG, path, export) | - |

| Example | Action |
|---|---|
| `pvedisk check` | report |
| `pvedisk delete -n` | plan for all orphans |
| `pvedisk delete --storage local-zfs -y` | orphans on one storage |
| `pvedisk delete 100-199 --kind orphan --kind unused` | orphans and unused disks owned by 100-199 |

| Safety | Behaviour |
|---|---|
| guest config unreadable | its volumes never classified as orphan |
| running tasks unreadable | refused (exit 2) |
| storage config (`/storage`) unreadable | refused (exit 2), aliases cannot be detected |
| after confirmation | full re-scan, only unchanged findings are deleted |
| `unused` | config re-read, value must still match, `digest` guards concurrent edits |
| after run | storages re-listed, leftovers reported as failed |
| `stale` | opt-in, wrong if a storage is shared but not flagged `shared` |
| path-style volids (`dir`/NFS) via pvesh | `pvesm free` on the local node; remote node-local: refused with hint |

Config source: `--config-source auto` reads `/etc/pve/nodes/*/{qemu-server,lxc}/*.conf` on a node (fast), otherwise the API (config, pending, each snapshot config).

## pvebackup

| Command | Purpose |
|---|---|
| `pvebackup list [GUESTS]` | per guest: in a job, last backup, age, count, size, storages, verify state |
| `pvebackup check [GUESTS]` | findings table |

| Check | Severity | Default |
|---|---|---|
| `none` in a job but no backup found | ERROR | on |
| `verify` latest backup failed PBS verification | ERROR | on |
| `verify` older backups failed | WARN | on |
| `nojob` not in any backup job (`/cluster/backup-info/not-backed-up`) | WARN | on |
| `age` last backup too old | WARN | `--max-age 26h` |
| `count` too few backups | WARN | `--min-count 0` (off) |
| `failed` vzdump task with error status | WARN | `--failed-since 7d` |
| `failed` same, but a newer backup of the guest exists | INFO | same |
| `usage` backup storage fill level | WARN | `--max-usage 85` |
| `scan` backup storage not listable | WARN | on |
| `scan` backup storage not available (node offline), its backups not counted | INFO | on |
| `jobs` disabled job, coverage unknown | INFO | on |
| `deleted-guest` backups of guests that no longer exist | INFO | on, unfiltered runs only |

Templates are skipped unless `--templates`.

## pvenode

| Command | Purpose |
|---|---|
| `pvenode list` | per node: status, pve-manager version, running kernel, reboot pending, uptime, pending updates, time to first certificate expiry |
| `pvenode check` | findings table; `--node` (repeatable), `--skip CHECK` (repeatable) |

| Check | Severity | Default |
|---|---|---|
| `quorum` cluster not quorate | ERROR | on |
| `offline` node offline | WARN | on |
| `fetch` node endpoint unreadable | WARN | on |
| `version` pve-manager differs between online nodes | WARN | on |
| `kernel` newer kernel installed than running: reboot pending | WARN | on |
| `updates` pending apt updates (from the daily apt cache) | INFO | on |
| `cert` certificate expired | ERROR | on |
| `cert` certificate expires soon | WARN | `--cert-warn 30d` |
| `rootfs`, `zfs`, `thin` root fs, ZFS pool, lvm-thin data and metadata usage | WARN / ERROR >= 95% | `--max-usage 85` |
| `zfs` pool health not `ONLINE` | ERROR | on |
| `smart` disk SMART health not `PASSED`/`OK` | ERROR | on |
| `time` clock skew between nodes | WARN | `--max-skew 5` seconds |
| `ceph` `HEALTH_WARN` / `HEALTH_ERR` | WARN / ERROR | on, only with an rbd/cephfs storage without `monhost` |

## pverestore

Restores a VM from old disk images into a new VM: a filer snapshot (`.snapshot` on NetApp/NFS, `.zfs/snapshot`) or any directory with the images. Every disk is copied flat with `qemu-img convert`, so internal qcow2 snapshots and backing chains do not survive. The source is only read, the original VM is not touched unless `--replace` is given. Root on a node, no API mode, QEMU only.

| Example | Action |
|---|---|
| `pverestore sources 100` | filer snapshots on all file storages that hold images of VM 100 |
| `pverestore vm 100 --from daily.0 --as 9100 -n` | plan: disks, sizes, snapshots in the source, dropped config keys |
| `pverestore vm 100 --from daily.0 --as 9100` | new VM 9100 from the config of 100 and the last state of each image |
| `pverestore vm 100 --from daily.0 --as 9100 --snapshot before-upgrade` | state of that PVE snapshot: internal qcow2 snapshot or `snap-<name>-*` chain file |
| `pverestore vm 100 --from /mnt/old/images/100 --as 9100 --storage nfs-prod` | images from a directory, all disks to one target storage |
| `pverestore vm 100 --from /mnt/old/images/100 --as 9100 --storage nfs-prod --no-config` | VM 100 is gone or the id was reused: minimal VM, every image as `scsiN` |
| `pverestore vm 100 --from daily.0 --replace -n` | plan for swapping the disks of VM 100 itself |
| `pverestore vm 100 --from daily.0 --replace --shutdown` | copy while VM 100 runs, shut it down, swap the disks, leave it stopped |

| Topic | Behaviour |
|---|---|
| new VM | name `restore-<name>`, not started, `onboot` and `protection` not copied |
| network | same MAC, `link_down=1` on every NIC: no address conflict with the original |
| not copied | `hostpci*`, `usb*` with `host=`, passthrough disks, `unusedN`, `vmgenid`, `cipassword` (the API returns it masked) |
| cloud-init drive | new drive on the target storage, not the volume of the original |
| image format | from the file extension, never probed: a guest can write a qcow2 header into a raw disk |
| backing chain | every file must be inside the source, else refused: the result would mix two points in time |
| disk without source image | refused, `--skip-missing` restores without it |
| image not in the config | listed, not restored (`--no-config` restores all) |
| target | file storages only (`dir`, `nfs`, `cifs`, `cephfs`, `glusterfs`), default: storage of each disk; move later with `qm disk move` |
| failure | nothing is deleted, the leftover VM and volumes are listed with the `qm destroy` command |
| consistency | a filer snapshot of a running VM is crash-consistent: expect journal replay, databases run recovery |

`--replace` keeps VM id, config, NICs and backup jobs and swaps only the disks:

| Step | Action |
|---|---|
| 1 | copy every disk into a new volume `vm-<vmid>-disk-N` of the VM (N unused in config and on disk); the VM may keep running |
| 2 | VM must be stopped: refused if it runs, `--shutdown` shuts it down here (`--timeout`, default 300 s) |
| 3 | config re-read: refused if a lock appeared or a disk key changed during the copy |
| 4 | old disks detached, they stay as `unusedN`; new volumes attached with the old drive options; `vmgenid` renewed |
| 5 | VM is left stopped: `qm start` after a look at the plan output |

| `--replace` topic | Behaviour |
|---|---|
| refused | VM on another node, template, locked, `--no-config`, running without `--shutdown` |
| way back | stop the VM, `qm set <vmid> --<key> <old volume>`; nothing is deleted |
| PVE snapshots of the VM | keep pointing to the old disks, a rollback brings those back; `unusedN` cannot be deleted before them |
| `--skip-missing` | a disk without source keeps its current content: mixed points in time, shown in the plan |
| failure before the swap | VM unchanged, new volumes are listed |
| failure between detach and attach | the `qm set` commands that attach the restored volumes are printed |

## Development

| Path | Purpose |
|---|---|
| `lib/shared.py` | backends, parsing, output; embedded in every tool between `# ==== shared:` markers |
| `scripts/sync-shared.sh` | re-embed `lib/shared.py` after editing it (a test fails on drift) |
| `scripts/test.sh` | tests in Docker, python 3.9 + 3.13, installs `qemu-utils` for the `pverestore` tests |
| `tests/fake_pvesh`, `tests/fake_pvesm` | API fakes backed by a JSON state file; volume allocation creates real images |

## License

Copyright (C) 2026 Raphael Friedmann. [GNU AGPL-3.0-only](LICENSE). Commercial licensing on request.
