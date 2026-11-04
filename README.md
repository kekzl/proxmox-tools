# proxmox-tools

Cluster-wide CLI tools for Proxmox VE. Each tool is one file, Python stdlib only (>= 3.9, PVE 7/8/9).

| Tool | Purpose |
|---|---|
| [`pvesnap`](#pvesnap) | snapshot trees, snapshot audit, delete chains/branches/by age |
| [`pvedisk`](#pvedisk) | find and delete orphaned, stale and unused guest volumes on all storages |
| [`pvebackup`](#pvebackup) | backup audit: job coverage, age, PBS verification, failed jobs, storage usage |

## Install

```sh
scp pvesnap pvedisk pvebackup root@pve1:/usr/local/sbin/
```

| Backend | When | Config |
|---|---|---|
| `pvesh` (default) | on any cluster node as root | none |
| API | remote host | `PVE_API_URL=https://pve1:8006`, `PVE_TOKEN='root@pam!tools=<secret>'`, optional `PVE_INSECURE=1` |

| Tool | API token privileges |
|---|---|
| `pvesnap` | `VM.Audit`; delete: `VM.Snapshot` |
| `pvedisk` | `VM.Audit`, `Datastore.Audit`, `Sys.Audit`; delete: `Datastore.Allocate`, `VM.Config.Disk` |
| `pvebackup` | `VM.Audit`, `Datastore.Audit`, `Sys.Audit` |

Common: `GUESTS` = `100`, `100,101`, `200-210`, `all`. `check` exits 1 on WARN/ERROR (`--fail-on warn|error|never`), `-q` hides INFO, `--json` for monitoring. Delete commands: `-n` dry run, `-y` no prompt, refused without `-y` on non-tty stdin, Ctrl-C finishes the running operation then stops. Exit codes: 0 ok, 1 findings/failures, 2 usage or fatal error.

## pvesnap

| Command | Purpose |
|---|---|
| `pvesnap list [GUESTS]` | snapshot trees with age, RAM state, chain depth; `--flat`, `--json`, `-e` |
| `pvesnap check [GUESTS]` | findings table |
| `pvesnap delete GUESTS [SNAP...]` | plan with `DEL` marks in the tree, confirm, delete oldest first, wait per task, verify |

| Check | Severity | Default |
|---|---|---|
| `fetch` snapshot list unreadable (node offline) | ERROR | on |
| `parent` missing parent reference | ERROR | on |
| `cycle` parent loop | ERROR | on |
| `snapstate` half-finished create/delete | ERROR | on, fix: `delete VMID SNAP --force` |
| `lock` guest lock without running task | WARN | on, hint `qm/pct unlock` |
| `age` | WARN | `--max-age 30d` |
| `depth` snapshots between live state and root | WARN | `--max-depth 5` |
| `count` per guest | WARN | `--max-count 0` (off) |
| `vmstate` snapshot holds RAM | INFO | on |
| `orphan` zfs/lvm-thin snapshot or state volume not in config | WARN | `--orphans`, local node only, ignores `__replicate_*` |

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
| guest locked at start | guest skipped |
| task fails or times out (`--timeout 3600`) | rest of that guest skipped, other guests continue |
| after run | snapshot list re-read, leftovers reported as failed |
| `--parallel N` | N guests at once (default 1) |

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
| `usage` backup storage fill level | WARN | `--max-usage 85` |
| `scan` backup storage not listable | WARN | on |
| `jobs` disabled job, coverage unknown | INFO | on |
| `deleted-guest` backups of guests that no longer exist | INFO | on, unfiltered runs only |

Templates are skipped unless `--templates`.

## Development

| Path | Purpose |
|---|---|
| `lib/shared.py` | backends, parsing, output; embedded in every tool between `# ==== shared:` markers |
| `scripts/sync-shared.sh` | re-embed `lib/shared.py` after editing it (a test fails on drift) |
| `scripts/test.sh` | tests in Docker, python 3.9 + 3.13 |
| `tests/fake_pvesh`, `tests/fake_pvesm` | API fakes backed by a JSON state file |
