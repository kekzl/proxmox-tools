UPID_RE = re.compile(r"UPID:[^\s\"']+")
SEVERITY_RANK = {"ERROR": 0, "WARN": 1, "INFO": 2}
AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


class PveError(Exception):
    """Fatal or per-item error with a user-facing message."""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_age(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdwy]?)\s*", str(text))
    if not m:
        raise argparse.ArgumentTypeError(f"invalid age '{text}' (examples: 30d, 12h, 2w, 90m)")
    return int(float(m.group(1)) * AGE_UNITS[m.group(2) or "d"])


def parse_idspec(text):
    """'all' -> None, '100,105,200-210' -> set of vmids."""
    if text in ("all", "*"):
        return None
    ids = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise argparse.ArgumentTypeError(f"invalid guest spec '{part}' (examples: 100, 100,101, 200-210, all)")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if b < a or b - a > 1000000:
            raise argparse.ArgumentTypeError(f"invalid range '{part}'")
        ids.update(range(a, b + 1))
    if not ids:
        raise argparse.ArgumentTypeError("empty guest spec")
    return ids


def regex(text):
    try:
        return re.compile(text)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"invalid regex '{text}': {e}")


def positive_int(text):
    v = _int(text)
    if v is None or v < 1:
        raise argparse.ArgumentTypeError(f"expected integer >= 1, got '{text}'")
    return v


def fmt_age(sec):
    if sec is None:
        return "?"
    sec = max(0, int(sec))
    if sec >= 172800:
        return f"{sec // 86400}d"
    if sec >= 3600:
        return f"{sec // 3600}h"
    return f"{sec // 60}m"


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def fmt_size(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def short(text, n=50):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= n else line[: n - 3] + "..."


class C:
    on = False

    @classmethod
    def wrap(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on and s else s

    @classmethod
    def red(cls, s):
        return cls.wrap("31", s)

    @classmethod
    def yellow(cls, s):
        return cls.wrap("33", s)

    @classmethod
    def green(cls, s):
        return cls.wrap("32", s)

    @classmethod
    def dim(cls, s):
        return cls.wrap("2", s)

    @classmethod
    def bold(cls, s):
        return cls.wrap("1", s)


SEV_COLOR = {"ERROR": C.red, "WARN": C.yellow, "INFO": C.dim}


def print_table(headers, rows, colorize=None):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    last = len(headers) - 1
    print("  ".join(h if i == last else h.ljust(widths[i]) for i, h in enumerate(headers)))
    for r in rows:
        cells = []
        for i, cell in enumerate(r):
            s = str(cell) if i == last else str(cell).ljust(widths[i])
            cells.append(colorize(i, str(cell), s) if colorize else s)
        print("  ".join(cells).rstrip())


def print_findings(findings, columns):
    """columns: list of (header, getter); first column must be severity."""
    rows = [[get(f) for _, get in columns] for f in findings]
    print_table([h for h, _ in columns], rows, colorize=lambda i, raw, s: SEV_COLOR[raw](s) if i == 0 else s)


def severity_counts(findings):
    return {k: sum(1 for f in findings if f.severity == k) for k in SEVERITY_RANK}


def exit_code(counts, fail_on):
    if fail_on == "never":
        return 0
    return 1 if counts["ERROR"] or (fail_on == "warn" and counts["WARN"]) else 0


class PveshBackend:
    """Local node: pvesh proxies calls to other cluster nodes itself."""

    local = True

    def __init__(self, binary="pvesh"):
        self.binary = binary

    def seg(self, text):
        return text

    def _run(self, method, path, params):
        cmd = [self.binary, method, path, "--output-format", "json"]
        for k, v in params.items():
            cmd += ["--" + k, str(v)]
        try:
            # own session: Ctrl-C must not kill a running pvesh worker mid-task
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, start_new_session=True)
        except OSError as e:
            raise PveError(f"cannot run {self.binary}: {e}") from e
        if p.returncode != 0:
            msg = " | ".join(l.strip() for l in (p.stderr or p.stdout).strip().splitlines() if l.strip())
            raise PveError(f"pvesh {method} {path}: {msg or 'exit code ' + str(p.returncode)}")
        return p.stdout

    def get(self, path, **params):
        out = self._run("get", path, params).strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except ValueError as e:
            raise PveError(f"pvesh get {path}: invalid JSON: {e}") from e

    def delete(self, path, **params):
        m = UPID_RE.search(self._run("delete", path, params))
        return m.group(0) if m else None

    def set(self, path, **params):
        m = UPID_RE.search(self._run("set", path, params))
        return m.group(0) if m else None


class ApiBackend:
    """Remote: https://host:8006 with an API token (USER@REALM!ID=SECRET)."""

    local = False

    def __init__(self, url, token, verify=True):
        if "://" not in url:
            url = "https://" + url
        parsed = urllib.parse.urlparse(url)
        if not parsed.port:
            url = f"{parsed.scheme}://{parsed.hostname}:8006"
        self.base = url.rstrip("/") + "/api2/json"
        token = token.strip()
        if token.startswith("PVEAPIToken="):
            token = token[len("PVEAPIToken="):]
        self.headers = {"Authorization": "PVEAPIToken=" + token, "Accept": "application/json"}
        self.ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()

    def seg(self, text):
        """Encode one path segment that may contain '/' (volume ids)."""
        return urllib.parse.quote(text, safe=":")

    def _req(self, method, path, params):
        url = self.base + urllib.parse.quote(path, safe="/%:")
        data, headers = None, dict(self.headers)
        q = urllib.parse.urlencode(params)
        if method in ("PUT", "POST"):
            data = q.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif q:
            url += "?" + q
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=120) as r:
                return json.load(r).get("data")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace").strip()
            raise PveError(f"{method} {path}: HTTP {e.code} {e.reason} {body}".strip()) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PveError(f"{method} {path}: {e}") from e

    def get(self, path, **params):
        return self._req("GET", path, params)

    def delete(self, path, **params):
        m = UPID_RE.search(str(self._req("DELETE", path, params) or ""))
        return m.group(0) if m else None

    def set(self, path, **params):
        m = UPID_RE.search(str(self._req("PUT", path, params) or ""))
        return m.group(0) if m else None


def make_backend(args):
    if args.api_url:
        if not args.token:
            raise PveError("--api-url needs --token or PVE_TOKEN")
        return ApiBackend(args.api_url, args.token, verify=not args.insecure)
    if shutil.which(args.pvesh) or os.path.isfile(args.pvesh):
        return PveshBackend(args.pvesh)
    raise PveError("pvesh not found: run on a Proxmox VE node or set --api-url/--token (env PVE_API_URL, PVE_TOKEN)")


def wait_task(backend, upid, timeout):
    """Polls a task until it stops; returns '' or ' [WARNINGS: n]', raises on failure."""
    node = upid.split(":")[1]
    path = f"/nodes/{node}/tasks/{upid}"
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        st = backend.get(path + "/status") or {}
        if st.get("status") == "stopped":
            break
        if time.monotonic() > deadline:
            raise PveError(f"task still running after {timeout}s, guest/volume stays locked until it ends: {upid}")
        time.sleep(delay)
        delay = min(delay * 1.5, 5)
    es = str(st.get("exitstatus", ""))
    if es == "OK":
        return ""
    if es.startswith("WARNINGS"):
        return f" [{es}]"
    tail = ""
    try:
        log = backend.get(path + "/log", limit=1000) or []
        lines = [str(e.get("t", "")) for e in log if e.get("t")][-5:]
        tail = (" | " + " | ".join(lines)) if lines else ""
    except PveError:
        pass
    raise PveError(f"task failed: {es}{tail}")


class Stopper:
    """SIGINT: first press stops after the running operation, second aborts."""

    def __init__(self):
        self.stop = False
        self.old = None

    def __call__(self, signum, frame):
        if self.stop:
            raise KeyboardInterrupt
        self.stop = True
        print("\nstopping after the running operation(s), Ctrl-C again aborts", file=sys.stderr, flush=True)

    def __enter__(self):
        self.old = signal.signal(signal.SIGINT, self)
        return self

    def __exit__(self, *exc):
        signal.signal(signal.SIGINT, self.old)
        return False


def confirm(args):
    """True if execution may proceed (--yes or interactive y)."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise PveError("stdin is not a terminal: pass --yes to confirm")
    return input("proceed? [y/N] ").strip().lower() in ("y", "yes")


def common_parser():
    common = argparse.ArgumentParser(add_help=False)
    conn = common.add_argument_group("connection (default: local pvesh)")
    conn.add_argument("--api-url", default=os.environ.get("PVE_API_URL"),
                      help="remote API, e.g. https://pve1:8006 (env PVE_API_URL)")
    conn.add_argument("--token", default=os.environ.get("PVE_TOKEN"),
                      help="API token USER@REALM!ID=SECRET (env PVE_TOKEN)")
    conn.add_argument("--insecure", action="store_true",
                      default=os.environ.get("PVE_INSECURE", "") not in ("", "0"),
                      help="skip TLS verification (env PVE_INSECURE=1)")
    conn.add_argument("--pvesh", default="pvesh", help="pvesh binary (default: pvesh)")
    common.add_argument("-j", "--jobs", type=positive_int, default=8, help="parallel API reads (default 8)")
    common.add_argument("--no-color", action="store_true", help="no ANSI colors (also env NO_COLOR)")
    return common


def filter_parser():
    filt = argparse.ArgumentParser(add_help=False)
    fg = filt.add_argument_group("guest filters")
    fg.add_argument("--node", action="append", help="only this node (repeatable)")
    fg.add_argument("--type", choices=["qemu", "lxc"], help="only VMs or containers")
    fg.add_argument("--name", type=regex, help="regex on guest name")
    return filt


def guest_filtered(args, vmid, node, gtype, name):
    """True if the guest passes GUESTS/--node/--type/--name."""
    if getattr(args, "guests", None) is not None and vmid not in args.guests:
        return False
    if args.node and node not in args.node:
        return False
    if args.type and gtype != args.type:
        return False
    return not (args.name and not args.name.search(name or ""))


def run_main(parser, commands, readonly, prog, argv=None):
    args = parser.parse_args(argv)
    C.on = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    if args.cmd in readonly and hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        return commands[args.cmd](make_backend(args), args)
    except PveError as e:
        print(f"{prog}: error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
