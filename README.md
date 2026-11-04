# proxmox-tools

## pvesnap

Audit and bulk-delete Proxmox VE snapshots (QEMU + LXC), cluster-wide. Single file, Python stdlib only (>= 3.9, PVE 7/8/9).

### Install

```sh
scp pvesnap root@pve1:/usr/local/sbin/pvesnap
```

| Backend | When | Config |
|---|---|---|
| `pvesh` (default) | run on any cluster node as root | none |
| API | remote host | `PVESNAP_API_URL=https://pve1:8006`, `PVESNAP_TOKEN='root@pam!pvesnap=<secret>'`, optional `PVESNAP_INSECURE=1` |

API token needs `VM.Audit` (list/check) and `VM.Snapshot` (delete). `check --orphans` only works with the local `pvesh` backend.

### Commands

| Command | Purpose |
|---|---|
| `pvesnap list [GUESTS]` | snapshot trees with age, RAM state, chain depth; `--flat`, `--json`, `-e` |
| `pvesnap check [GUESTS]` | findings table, exit 1 on WARN/ERROR (`--fail-on`), `--json` for monitoring |
| `pvesnap delete GUESTS [SNAP...]` | plan with `DEL` marks in the tree, confirm, delete oldest first, wait per task, verify |

`GUESTS`: `100`, `100,101`, `200-210`, `all`. Filters: `--node`, `--type qemu|lxc`, `--name REGEX`.

### Checks

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
| `vmstate` snapshot holds RAM | INFO | on, `-q` hides INFO |
| `orphan` zfs/lvm-thin snapshot or state volume not in config | WARN | `--orphans`, local node only, ignores `__replicate_*` |

### Delete selection

Base set = named snapshots + `--subtree` + `--upto`, or all snapshots if none given. Then `--match`/`--exclude`/`--older-than` narrow it, `--keep-last N` protects the newest N (counted among `--match`/`--exclude`).

| Example | Deletes |
|---|---|
| `pvesnap delete 100 pre-upgrade` | one snapshot |
| `pvesnap delete 100 --upto weekly-3` | `weekly-3` and every older snapshot in its chain |
| `pvesnap delete 100 --subtree test` | `test` and all its descendants (whole branch) |
| `pvesnap delete 100 --all` | every snapshot of 100 |
| `pvesnap delete all --older-than 30d -n` | dry run, cluster-wide |
| `pvesnap delete all --match '^auto' --keep-last 7 -y` | rotation without prompt (cron) |

| Safety | Behaviour |
|---|---|
| no selector | refused, exit 2 |
| single guest + unknown snapshot name | refused, exit 2 |
| non-tty without `-y` | refused, exit 2 |
| guest locked at start | guest skipped |
| task fails or times out (`--timeout 3600`) | rest of that guest skipped, other guests continue |
| Ctrl-C | finishes running deletion(s), then stops; 2nd Ctrl-C aborts |
| after run | snapshot list re-read, leftovers reported as failed |
| `--parallel N` | N guests at once (default 1, one task per guest at any time) |

Exit codes: 0 ok, 1 findings/failures/skips, 2 usage or fatal error.

### Tests

```sh
scripts/test.sh            # Docker, python 3.9 + 3.13, fake pvesh in tests/fake_pvesh
```
