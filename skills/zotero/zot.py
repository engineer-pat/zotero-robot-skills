#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["tomli>=2; python_version < '3.11'"]
# ///
"""zot.py: read a Zotero library by collection, safely, for agent pipelines.

Reads go to the Zotero desktop app's local API (http://localhost:23119/api),
which needs no key and never leaves the machine. Zotero must be running with
Settings > Advanced > "Allow other applications on this computer to
communicate with Zotero" turned on.

Everything is scoped by an allowlist of collections in a per-machine config
file, and every item is classified before anything may be sent anywhere:

  ready     has a PDF on disk and a citation key; may be processed
  done      already converted, and the PDF has not changed since
  stale     converted, but the PDF changed afterwards
  excluded  carries an exclude tag or sits in an exclude collection
  held      looks unpublished (no DOI/arXiv id, or a held item type); needs
            `approve` before it may be processed
  no-pdf    no PDF attachment, or the file is not downloaded on this machine
  no-key    no citation key (Zotero 8+ field, or "Citation Key:" in Extra)

Commands: config, setup, doctor, collections, items, status, dry-run,
resolve, record, approve, unapprove. Run `zot.py <command> -h` for options.

The Zotero web API key (for later write commands) is read from
$ZOTERO_API_KEY, else the file named in the config. This script never prints it.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

APP = "zotero-robot"
WEB_API = "https://api.zotero.org"

DEFAULTS: dict = {
    "zotero": {
        "local_api": "http://localhost:23119/api",
        "library": "users/0",
        "user_id": 0,
        "api_key_file": "",  # empty: <config home>/zotero/api_key
    },
    "scope": {
        "collections": [],
        "exclude_collections": ["exclude"],
        "exclude_tags": ["no-upload"],
        "hold_item_types": ["manuscript", "report", "document", "thesis"],
        "hold_without_public_id": True,
    },
    "vault": {"path": "", "pipeline_dir": "_zotero-robot"},
    "convert": {"preset": "standard", "attach_to_zotero": False},
}

EXAMPLE_NAME = "config.example.toml"


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"zot: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- config

def config_home() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def config_path() -> Path:
    env = os.environ.get("ZOTERO_ROBOT_CONFIG")
    return Path(env).expanduser() if env else config_home() / APP / "config.toml"


def expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def load_config(required: bool = True) -> dict:
    path = config_path()
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if not path.exists():
        if required:
            die(f"no config at {path}. Create one with: zot.py config init")
        return cfg
    if tomllib is None:
        die("reading the config needs Python 3.11+, or run through `uv run` (adds tomli)")
    try:
        with open(path, "rb") as f:
            user = tomllib.load(f)
    except Exception as e:  # tomllib.TOMLDecodeError and OSError
        die(f"cannot read {path}: {e}")
    for section, values in user.items():
        if isinstance(values, dict) and isinstance(cfg.get(section), dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    return cfg


def key_file(cfg: dict) -> Path:
    custom = cfg["zotero"].get("api_key_file") or ""
    return expand(custom) if custom else config_home() / "zotero" / "api_key"


def read_key(cfg: dict) -> str | None:
    env = os.environ.get("ZOTERO_API_KEY")
    if env:
        return env.strip()
    f = key_file(cfg)
    try:
        return f.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def state_file() -> Path:
    return config_path().parent / "approved.json"


def load_approved() -> set[str]:
    try:
        return set(json.loads(state_file().read_text(encoding="utf-8")).get("items", []))
    except (OSError, ValueError):
        return set()


def save_approved(keys: set[str]) -> None:
    f = state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"items": sorted(keys)}, indent=1) + "\n", encoding="utf-8")


# ------------------------------------------------------------- local API

class Local:
    """Minimal client for Zotero's local API (read-only use)."""

    def __init__(self, cfg: dict):
        self.base = cfg["zotero"]["local_api"].rstrip("/")
        self.lib = cfg["zotero"]["library"].strip("/")

    def _open(self, url: str):
        req = urllib.request.Request(url, headers={"Zotero-API-Version": "3"})
        try:
            return urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                die('Zotero refused the request (403). Turn on Settings > Advanced > "Allow other '
                    'applications on this computer to communicate with Zotero".')
            raise
        except (urllib.error.URLError, OSError) as e:
            die(f"cannot reach Zotero at {self.base} ({getattr(e, 'reason', e)}). "
                "Is the Zotero desktop app running on this machine?")

    def get(self, path: str, **params) -> list | dict:
        """GET a JSON endpoint, following Link rel=next if the server pages."""
        params.setdefault("format", "json")
        url = f"{self.base}/{self.lib}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        out: list = []
        while url:
            with self._open(url) as r:
                data = json.load(r)
                link = r.headers.get("Link", "")
            if not isinstance(data, list):
                return data
            out.extend(data)
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else ""
        return out

    def file_path(self, attachment_key: str) -> Path | None:
        """Local path of an attachment's file, or None if it is not on disk."""
        url = f"{self.base}/{self.lib}/items/{attachment_key}/file/view/url"
        req = urllib.request.Request(url, headers={"Zotero-API-Version": "3"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8").strip()
        except urllib.error.HTTPError:
            return None
        except (urllib.error.URLError, OSError) as e:
            die(f"cannot reach Zotero at {self.base} ({getattr(e, 'reason', e)})")
        p = file_url_to_path(text)
        return p if p and p.exists() else None


def file_url_to_path(url: str) -> Path | None:
    if not url.startswith("file:"):
        return None
    parsed = urllib.parse.urlparse(url)
    path = urllib.request.url2pathname(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":  # UNC share on Windows
        path = f"//{parsed.netloc}{path}"
    return Path(path)


# ----------------------------------------------------------- collections

class Tree:
    def __init__(self, raw: list):
        self.name: dict[str, str] = {}
        self.parent: dict[str, str | None] = {}
        self.count: dict[str, int] = {}
        self.kids: dict[str | None, list[str]] = {}
        for c in raw:
            k = c["key"]
            d = c.get("data", c)
            self.name[k] = d.get("name", "")
            self.parent[k] = d.get("parentCollection") or None
            self.count[k] = (c.get("meta") or {}).get("numItems", 0)
        for k, p in self.parent.items():
            self.kids.setdefault(p if p in self.name else None, []).append(k)
        for v in self.kids.values():
            v.sort(key=lambda k: self.name[k].lower())

    def path(self, key: str) -> str:
        parts, seen = [], set()
        k: str | None = key
        while k and k in self.name and k not in seen:
            seen.add(k)
            parts.append(self.name[k])
            k = self.parent[k]
        return "/".join(reversed(parts))

    def descendants(self, key: str) -> list[str]:
        out, stack = [], [key]
        while stack:
            k = stack.pop()
            out.append(k)
            stack.extend(self.kids.get(k, []))
        return out

    def resolve(self, spec: str) -> str:
        """A collection key, a name, or a path like 'AFRL Research/DLTS'."""
        if spec in self.name:
            return spec
        want = spec.strip("/").lower()
        hits = [k for k in self.name
                if self.name[k].lower() == want or self.path(k).lower() == want]
        if not hits:
            die(f"no collection named {spec!r}. See: zot.py collections")
        if len(hits) > 1:
            listing = "; ".join(f"{self.path(k)} [{k}]" for k in hits)
            die(f"{spec!r} matches several collections: {listing}. Use the key or full path.")
        return hits[0]

    def excluded(self, names: list[str]) -> set[str]:
        want = {n.lower() for n in names}
        out: set[str] = set()
        for k, n in self.name.items():
            if n.lower() in want:
                out.update(self.descendants(k))
        return out


# ----------------------------------------------------------------- items

def citekey(data: dict) -> str | None:
    k = (data.get("citationKey") or "").strip()
    if k:
        return k
    m = re.search(r"^\s*Citation Key:\s*(\S+)", data.get("extra") or "", re.I | re.M)
    return m.group(1) if m else None


def public_id(data: dict) -> str | None:
    doi = (data.get("DOI") or "").strip()
    if doi:
        return doi
    extra = data.get("extra") or ""
    m = re.search(r"^\s*DOI:\s*(\S+)", extra, re.I | re.M)
    if m:
        return m.group(1)
    m = re.search(r"arxiv(?:\.org/abs/|:\s*)(\S+)", f"{extra}\n{data.get('url') or ''}", re.I)
    return f"arXiv:{m.group(1)}" if m else None


def year(data: dict) -> str:
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", data.get("date") or "")
    return m.group(1) if m else ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pipeline_root(cfg: dict) -> Path | None:
    v = cfg["vault"].get("path") or ""
    return expand(v) / cfg["vault"]["pipeline_dir"] if v else None


def gather(cfg: dict, specs: list[str] | None = None) -> list[dict]:
    """Classify every top-level item in the scoped collections."""
    api = Local(cfg)
    tree = Tree(api.get("collections"))
    specs = specs or cfg["scope"]["collections"]
    if not specs:
        die("no collections in scope. Add names to [scope] collections in the config, "
            "or name one on the command line.")
    scope: list[str] = []
    for s in specs:
        for k in tree.descendants(tree.resolve(s)):
            if k not in scope:
                scope.append(k)
    banned = tree.excluded(cfg["scope"]["exclude_collections"])
    ex_tags = {t.lower() for t in cfg["scope"]["exclude_tags"]}
    hold_types = {t.lower() for t in cfg["scope"]["hold_item_types"]}
    approved = load_approved()
    root = pipeline_root(cfg)

    seen: dict[str, dict] = {}
    for ck in scope:
        if ck in banned:
            continue
        for it in api.get(f"collections/{ck}/items/top"):
            seen.setdefault(it["key"], it)

    rows = []
    for key, it in seen.items():
        d = it.get("data", {})
        row = {
            "key": key, "citekey": citekey(d), "year": year(d),
            "type": d.get("itemType", ""), "title": d.get("title", ""),
            "id": public_id(d), "pdf": None, "bytes": 0,
            "collections": sorted(tree.path(c) for c in d.get("collections", []) if c in tree.name),
        }
        tags = {t.get("tag", "").lower() for t in d.get("tags", [])}
        in_banned = [tree.path(c) for c in d.get("collections", []) if c in banned]
        if tags & ex_tags:
            row.update(status="excluded", reason="tag " + ", ".join(sorted(tags & ex_tags)))
        elif in_banned:
            row.update(status="excluded", reason="in " + in_banned[0])
        else:
            pdf = None
            if (it.get("meta") or {}).get("numChildren", 1):
                for ch in api.get(f"items/{key}/children"):
                    cd = ch.get("data", {})
                    if cd.get("itemType") == "attachment" and cd.get("contentType") == "application/pdf" \
                            and cd.get("linkMode") != "linked_url":
                        pdf = api.file_path(ch["key"])
                        if pdf:
                            row["attachment"] = ch["key"]
                            break
            held = []
            if row["type"].lower() in hold_types:
                held.append(f"type {row['type']}")
            if cfg["scope"]["hold_without_public_id"] and not row["id"]:
                held.append("no DOI or arXiv id")
            if not pdf:
                row.update(status="no-pdf", reason="no PDF on this machine")
            elif not row["citekey"]:
                row.update(status="no-key", reason="no citation key")
            elif held and key not in approved:
                row.update(status="held", reason="; ".join(held))
            else:
                row.update(status="ready", reason="approved" if held else "")
            if pdf:
                row["pdf"], row["bytes"] = str(pdf), pdf.stat().st_size
                if row["status"] == "ready" and root:
                    meta = root / "fulltext" / row["citekey"] / "meta.json"
                    if meta.exists():
                        try:
                            old = json.loads(meta.read_text(encoding="utf-8")).get("source_sha256")
                        except (OSError, ValueError):
                            old = None
                        same = old == sha256(pdf)
                        row.update(status="done" if same else "stale",
                                   reason="" if same else "PDF changed since conversion")
        rows.append(row)
    order = ["ready", "stale", "held", "no-key", "no-pdf", "excluded", "done"]
    rows.sort(key=lambda r: (order.index(r["status"]), (r["citekey"] or r["title"]).lower()))
    return rows


def show(rows: list[dict], as_json: bool) -> None:
    if as_json:
        json.dump(rows, sys.stdout, indent=1)
        print()
        return
    for r in rows:
        title = r["title"] if len(r["title"]) <= 56 else r["title"][:55] + "…"
        note = f"  ({r['reason']})" if r["reason"] else ""
        print(f"{r['status']:<9}{r['key']:<10}{(r['citekey'] or '-'):<28}{r['year']:<6}{title}{note}")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + ", ".join(f"{n} {s}" for s, n in counts.items()) + f"  |  {len(rows)} items")


# -------------------------------------------------------------- commands

def cmd_config(args) -> None:
    path = config_path()
    if args.action == "path":
        print(path)
    elif args.action == "init":
        if path.exists() and not args.force:
            die(f"{path} already exists (use --force to replace it)")
        example = Path(__file__).resolve().parent / EXAMPLE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"wrote {path}\nEdit it: set [scope] collections and [vault] path for this machine.")
    else:
        cfg = load_config()
        print(f"# {path}")
        json.dump(cfg, sys.stdout, indent=1)
        print()


def cmd_setup(args) -> None:
    cfg = load_config(required=False)
    f = key_file(cfg)
    if args.remove:
        f.unlink(missing_ok=True)
        print(f"removed {f}")
        return
    print("Create a key at https://www.zotero.org/settings/keys (personal library, read/write).\n"
          "Paste it here; it is not shown and is stored only on this machine.")
    key = getpass.getpass("Zotero API key: ").strip()
    if not key:
        die("no key entered")
    f.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(f.parent, 0o700)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(key + "\n")
    print(f"stored in {f}")
    info = verify_key(key)
    if info:
        print(f"verified: userID {info['userID']}. Put `user_id = {info['userID']}` under [zotero] in the config.")
    else:
        print("could not verify the key online just now; `zot.py doctor` will retry.")


def verify_key(key: str) -> dict | None:
    req = urllib.request.Request(f"{WEB_API}/keys/current",
                                 headers={"Zotero-API-Key": key, "Zotero-API-Version": "3"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        return {"userID": d.get("userID"), "access": d.get("access", {})}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def cmd_doctor(args) -> None:
    bad = 0

    def line(ok: bool | None, text: str) -> None:
        nonlocal bad
        bad += ok is False
        print(f"  [{'ok' if ok else 'warn' if ok is None else 'miss'}] {text}")

    path = config_path()
    line(path.exists(), f"config: {path}" + ("" if path.exists() else "  (run: zot.py config init)"))
    cfg = load_config(required=False)
    scope = cfg["scope"]["collections"]
    line(bool(scope) or None, f"collections in scope: {', '.join(scope) if scope else 'none set'}")
    root = pipeline_root(cfg)
    if root is None:
        line(None, "vault path not set ([vault] path)")
    else:
        line(root.parent.is_dir(), f"vault: {root.parent}")
    api = Local(cfg)
    try:
        cols = api.get("collections")
        line(True, f"Zotero local API: {api.base} ({len(cols)} collections)")
        if scope:
            tree = Tree(cols)
            for s in scope:
                k = tree.resolve(s)
                line(True, f"scope resolves: {tree.path(k)} [{k}], {len(tree.descendants(k)) - 1} subcollections")
    except SystemExit:
        bad += 1
    key = read_key(cfg)
    if not key:
        line(None, f"no web API key (only needed for write commands): {key_file(cfg)}")
    else:
        f = key_file(cfg)
        if os.name != "nt" and f.exists() and (f.stat().st_mode & 0o077) and not os.environ.get("ZOTERO_API_KEY"):
            line(False, f"key file is readable by others: chmod 600 {f}")
        info = verify_key(key)
        if info is None:
            line(None, "web API key present, but could not verify it online")
        else:
            lib = (info["access"].get("user") or {})
            line(True, f"web API key: userID {info['userID']}, library read={bool(lib.get('library'))} "
                       f"write={bool(lib.get('write'))}")
            if cfg["zotero"]["user_id"] not in (0, info["userID"]):
                line(False, f"config user_id {cfg['zotero']['user_id']} does not match the key's {info['userID']}")
    here = Path(__file__).resolve().parent.parent
    for sib in ("pdf-convert/pdfc.py", "openalex/oa.py"):
        line((here / sib).exists() or None, f"sibling skill: {sib}")
    sys.exit(1 if bad else 0)


def cmd_collections(args) -> None:
    cfg = load_config(required=False)
    tree = Tree(Local(cfg).get("collections"))
    banned = tree.excluded(cfg["scope"]["exclude_collections"])
    scope = set()
    for s in cfg["scope"]["collections"]:
        scope.update(tree.descendants(tree.resolve(s)))

    def walk(parent: str | None, depth: int) -> None:
        for k in tree.kids.get(parent, []):
            mark = "excluded" if k in banned else "in scope" if k in scope else ""
            print(f"{'  ' * depth}{tree.name[k]}  [{k}]  {tree.count[k]} items" + (f"  <- {mark}" if mark else ""))
            walk(k, depth + 1)

    walk(None, 0)


def cmd_status(args) -> None:
    cfg = load_config()
    rows = gather(cfg, args.collection or None)
    if args.command == "dry-run":
        send = [r for r in rows if r["status"] in ("ready", "stale")]
        skip = [r for r in rows if r["status"] not in ("ready", "stale", "done")]
        if args.json:
            show(send, True)
            return
        print(f"WOULD BE SENT for conversion ({cfg['convert']['preset']} preset): {len(send)} items, "
              f"{sum(r['bytes'] for r in send) / 1e6:.1f} MB")
        show(send, False) if send else print("  nothing")
        print(f"\nNOT SENT: {len(skip)} items")
        show(skip, False) if skip else print("  nothing")
        print("\nNothing was sent. Held items need `zot.py approve KEY` first; excluded items are never sent.")
    else:
        show(rows, args.json)


def cmd_items(args) -> None:
    cfg = load_config(required=False)
    show(gather(cfg, [args.collection]), args.json)


def lookup(cfg: dict, key: str) -> list[dict]:
    """Items with this exact citation key: item key, PDF path, output folder."""
    api = Local(cfg)
    root = pipeline_root(cfg)
    ex_tags = {t.lower() for t in cfg["scope"]["exclude_tags"]}
    banned = Tree(api.get("collections")).excluded(cfg["scope"]["exclude_collections"])
    # The local API returns the whole library unpaged, so match the key exactly here
    # instead of relying on which fields its quick search covers.
    out = []
    for it in api.get("items/top"):
        d = it.get("data", {})
        if citekey(d) != key:
            continue
        blocked = sorted({t.get("tag", "").lower() for t in d.get("tags", [])} & ex_tags)
        if any(c in banned for c in d.get("collections", [])):
            blocked.append("exclude collection")
        pdf = None
        if not blocked:
            for ch in api.get(f"items/{it['key']}/children"):
                cd = ch.get("data", {})
                if cd.get("contentType") == "application/pdf" and cd.get("linkMode") != "linked_url":
                    pdf = api.file_path(ch["key"])
                    if pdf:
                        break
        out.append({"key": it["key"], "citekey": key, "title": d.get("title", ""),
                    "year": year(d), "id": public_id(d), "excluded": blocked,
                    "pdf": str(pdf) if pdf else None,
                    "outdir": str(root / "fulltext" / key) if root and not blocked else None,
                    "select": f"zotero://select/library/items/{it['key']}"})
    return out


def cmd_resolve(args) -> None:
    out = lookup(load_config(required=False), args.citekey)
    if not out:
        die(f"no item with citation key {args.citekey!r}")
    json.dump(out if len(out) > 1 else out[0], sys.stdout, indent=1)
    print()


def cmd_record(args) -> None:
    """Write meta.json after a conversion, so status can report done/stale."""
    hits = lookup(load_config(), args.citekey)
    if len(hits) != 1:
        die(f"{len(hits)} items have citation key {args.citekey!r}; expected exactly one")
    h = hits[0]
    if h["excluded"]:
        die(f"{args.citekey} is excluded ({', '.join(h['excluded'])}); nothing recorded")
    if not h["pdf"] or not h["outdir"]:
        die("no PDF on this machine, or [vault] path is not set")
    outdir = Path(h["outdir"])
    if not any(outdir.glob("*.md")):
        die(f"no converted markdown in {outdir}; convert first")
    import datetime
    meta = {"citekey": args.citekey, "item_key": h["key"], "source_pdf": h["pdf"],
            "source_sha256": sha256(Path(h["pdf"])), "converter": args.converter,
            "preset": args.preset,
            "converted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    (outdir / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(f"recorded {outdir / 'meta.json'}")


def cmd_approve(args) -> None:
    keys = load_approved()
    keys = keys - set(args.key) if args.command == "unapprove" else keys | set(args.key)
    save_approved(keys)
    print(f"{len(keys)} approved item(s): {', '.join(sorted(keys)) or 'none'}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="zot.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("config", help="show, create, or locate the per-machine config")
    s.add_argument("action", nargs="?", choices=["show", "init", "path"], default="show")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("setup", help="store the Zotero web API key (run in your own terminal)")
    s.add_argument("--remove", action="store_true")
    s.set_defaults(fn=cmd_setup)

    sub.add_parser("doctor", help="check config, Zotero, key and sibling skills").set_defaults(fn=cmd_doctor)
    sub.add_parser("collections", help="collection tree with keys and scope marks").set_defaults(fn=cmd_collections)

    s = sub.add_parser("items", help="classify the items of one collection (and its subcollections)")
    s.add_argument("collection")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_items)

    for name, text in (("status", "classify every item in scope"),
                       ("dry-run", "show exactly what a conversion run would send; sends nothing")):
        s = sub.add_parser(name, help=text)
        s.add_argument("collection", nargs="*", help="default: the collections in the config")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=cmd_status)

    s = sub.add_parser("resolve", help="citation key -> item key, PDF path, Zotero link")
    s.add_argument("citekey")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("record", help="after converting: write meta.json so status shows done/stale")
    s.add_argument("citekey")
    s.add_argument("--converter", default="datalab")
    s.add_argument("--preset", default="standard")
    s.set_defaults(fn=cmd_record)

    for name in ("approve", "unapprove"):
        s = sub.add_parser(name, help=f"{name} held items by item key")
        s.add_argument("key", nargs="+")
        s.set_defaults(fn=cmd_approve)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
