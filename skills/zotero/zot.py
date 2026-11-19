#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["tomli>=2; python_version < '3.11'"]
# ///
"""zot.py: read a Zotero library by collection, safely, for agent pipelines.

Reads go to the Zotero desktop app's local API (http://127.0.0.1:23119/api),
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
  pending   submitted for conversion but not collected yet; `fetch-pending`

Commands: config, setup, doctor, collections, items, status, dry-run,
index, fetch-pending, notes, resolve, record, attach, check, set, approve,
unapprove.

Conversion is done by the pdf-convert skill (pdfc.py), called as a separate
program; its page limit, budget, cache and no-retry rules all still apply. Run `zot.py <command> -h` for options.

The Zotero web API key (for write commands such as `attach` and `set`) is read from
$ZOTERO_API_KEY, else the file named in the config. This script never prints it.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import datetime
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
        "local_api": "http://127.0.0.1:23119/api",
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
    "convert": {"preset": "standard", "attach_to_zotero": False, "pdfc_command": ""},
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


def pending_file() -> Path:
    return config_path().parent / "pending.json"


def load_pending() -> dict[str, dict]:
    try:
        return json.loads(pending_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_pending(p: dict[str, dict]) -> None:
    f = pending_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(p, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def safe_key(citekey: str) -> str:
    """Folder and file name for a citation key; the same rule pdfc.py uses for --name."""
    return re.sub(r"[^\w.-]+", "_", citekey, flags=re.ASCII).strip("._") or "document"


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

    def matches(self, spec: str) -> list[str]:
        """Keys matching a collection key, a name, or a path like 'AFRL Research/DLTS'."""
        if spec in self.name:
            return [spec]
        want = spec.strip("/").lower()
        return [k for k in self.name
                if self.name[k].lower() == want or self.path(k).lower() == want]

    def suggest(self, spec: str) -> list[str]:
        """Paths of collections whose name shares a word with spec, for error messages."""
        words = {w for w in re.split(r"[\s/_-]+", spec.lower()) if len(w) > 2}
        return sorted(self.path(k) for k, n in self.name.items()
                      if words & set(re.split(r"[\s/_-]+", n.lower())))

    def unknown(self, spec: str) -> str:
        near = self.suggest(spec)
        return f"no collection named {spec!r}" + (f" (did you mean: {'; '.join(near)}?)" if near else "")

    def resolve(self, spec: str) -> str:
        """A collection key, a name, or a path like 'AFRL Research/DLTS'."""
        hits = self.matches(spec)
        if not hits:
            die(f"{self.unknown(spec)}. See: zot.py collections")
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


def select_link(cfg: dict, key: str) -> str:
    """zotero:// link that selects the item in the desktop app, in its own library."""
    lib = cfg["zotero"]["library"].strip("/")
    where = "library" if lib.startswith("users/") else lib
    return f"zotero://select/{where}/items/{key}"


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
    pending = load_pending()
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
            elif row["citekey"] in pending:
                row.update(status="pending",
                           reason=f"request {pending[row['citekey']].get('request_id')}; run fetch-pending")
            elif held and key not in approved:
                row.update(status="held", reason="; ".join(held))
            else:
                row.update(status="ready", reason="approved" if held else "")
            if pdf:
                row["pdf"], row["bytes"] = str(pdf), pdf.stat().st_size
                if row["status"] == "ready" and root:
                    meta = root / "fulltext" / safe_key(row["citekey"]) / "meta.json"
                    if meta.exists():
                        try:
                            old = json.loads(meta.read_text(encoding="utf-8")).get("source_sha256")
                        except (OSError, ValueError):
                            old = None
                        same = old == sha256(pdf)
                        row.update(status="done" if same else "stale",
                                   reason="" if same else "PDF changed since conversion")
        rows.append(row)
    order = ["ready", "stale", "pending", "held", "no-key", "no-pdf", "excluded", "done"]
    rows.sort(key=lambda r: (order.index(r["status"]), (r["citekey"] or r["title"]).lower()))
    return rows


def show(rows: list[dict], as_json: bool) -> None:
    if as_json:
        json.dump(rows, sys.stdout, indent=1)
        print()
        return
    width = max((len(r["citekey"] or "-") for r in rows), default=0) + 2  # keys run to 40+ chars
    for r in rows:
        title = r["title"] if len(r["title"]) <= 56 else r["title"][:55] + "…"
        note = f"  ({r['reason']})" if r["reason"] else ""
        print(f"{r['status']:<9}{r['key']:<10}{(r['citekey'] or '-'):<{width}}{r['year']:<6}{title}{note}")
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


class Web:
    """Zotero web API, for the few writes zot.py makes. The key is never printed."""

    def __init__(self, cfg: dict):
        self.key = read_key(cfg)
        if not self.key:
            die("no Zotero web API key: run `zot.py setup` in your own terminal")
        lib = cfg["zotero"]["library"].strip("/")
        if lib == "users/0":
            uid = cfg["zotero"].get("user_id") or (verify_key(self.key) or {}).get("userID")
            if not uid:
                die("cannot find your Zotero userID: set user_id under [zotero] (`zot.py setup` prints it)")
            lib = f"users/{uid}"
        self.lib = lib

    def call(self, method: str, path: str, body: bytes = b"", headers: dict | None = None,
             url: str | None = None) -> tuple[int, bytes]:
        """Returns (status, body). With `url` (a storage upload URL) no key is sent."""
        h = {} if url else {"Zotero-API-Version": "3", "Zotero-API-Key": self.key}
        h.update(headers or {})
        req = urllib.request.Request(url or f"{WEB_API}/{self.lib}/{path}", data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            die(f"cannot reach {urllib.parse.urlsplit(req.full_url).netloc} ({getattr(e, 'reason', e)})")


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
                hits = tree.matches(s)
                if len(hits) != 1:
                    line(False, f"scope: {tree.unknown(s)}" if not hits else
                         f"scope: {s!r} matches several collections; use the key or full path")
                    continue
                k = hits[0]
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
    scope, problems = set(), []
    for s in cfg["scope"]["collections"]:
        hits = tree.matches(s)
        if len(hits) == 1:
            scope.update(tree.descendants(hits[0]))
        else:
            problems.append(tree.unknown(s) if not hits else f"{s!r} matches several collections")

    def walk(parent: str | None, depth: int) -> None:
        for k in tree.kids.get(parent, []):
            mark = "excluded" if k in banned else "in scope" if k in scope else ""
            print(f"{'  ' * depth}{tree.name[k]}  [{k}]  {tree.count[k]} items" + (f"  <- {mark}" if mark else ""))
            walk(k, depth + 1)

    walk(None, 0)
    for p in problems:
        print(f"\nscope problem: {p}. Fix [scope] collections in {config_path()}", file=sys.stderr)


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
        print("\nNothing was sent. Held items need `zot.py approve KEY` first; excluded items are never sent.\n"
              "`zot.py index` estimates the cost of the items above and converts them after confirmation.")
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
                    "outdir": str(root / "fulltext" / safe_key(key)) if root and not blocked else None,
                    "select": select_link(cfg, it["key"])})
    return out


def cmd_resolve(args) -> None:
    out = lookup(load_config(required=False), args.citekey)
    if not out:
        die(f"no item with citation key {args.citekey!r}")
    json.dump(out if len(out) > 1 else out[0], sys.stdout, indent=1)
    print()


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def finalize(cfg: dict, item: dict, outdir: Path, preset: str, converter: str = "datalab") -> dict:
    """After a successful conversion: frontmatter on the markdown, meta.json, manifest line."""
    stem = safe_key(item["citekey"])
    md = outdir / f"{stem}.md"
    if not md.exists():
        die(f"no converted markdown at {md}")
    try:
        conv = json.loads((outdir / f"{stem}.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        conv = {}
    meta = {"citekey": item["citekey"], "item_key": item["key"], "title": item.get("title", ""),
            "year": item.get("year", ""), "collections": item.get("collections", []),
            "source_pdf": item["pdf"], "source_sha256": sha256(Path(item["pdf"])),
            "converter": converter, "preset": preset,
            "pages": conv.get("page_count"), "cost_usd": conv.get("cost_usd"),
            "request_id": conv.get("request_id"), "cached": conv.get("cached"),
            "converted_at": now_iso()}
    front = "\n".join([
        "---",
        f"citekey: {json.dumps(item['citekey'])}",
        f"title: {json.dumps(meta['title'])}",
        f"year: {json.dumps(meta['year'])}",
        f"zotero: {json.dumps(select_link(cfg, item['key']))}",
        f"collections: {json.dumps(meta['collections'])}",
        f"converted: {json.dumps(meta['converted_at'][:10])}",
        f"converter: {json.dumps(converter + ' ' + preset)}",
        "tags: [zotero-robot/fulltext]",
        "---", "", ""])
    body = md.read_text(encoding="utf-8")
    if body.startswith("---\ncitekey: "):  # ours from an earlier run on this same file
        body = body.split("\n---\n", 1)[-1].lstrip("\n")
    md.write_text(front + body, encoding="utf-8")
    (outdir / "meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    root = outdir.parent.parent
    with open(root / "manifest.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({k: meta[k] for k in ("citekey", "item_key", "title", "year", "collections",
                                                 "pages", "cost_usd", "preset", "converted_at")}) + "\n")
    return meta


def cmd_attach(args) -> None:
    """Upload a PDF the user obtained as a stored attachment of an item already in Zotero."""
    cfg = load_config()
    pdf = Path(args.pdf)
    try:
        data = pdf.read_bytes()
    except OSError as e:
        die(f"cannot read {pdf}: {e}")
    if not data.startswith(b"%PDF-"):
        die(f"{pdf} is not a PDF (no %PDF- header); a login or viewer page was probably saved instead")
    hits = lookup(cfg, args.citekey)
    if len(hits) != 1:
        die(f"{len(hits)} items have citation key {args.citekey!r}; it must match exactly one")
    item = hits[0]
    if item["excluded"]:
        die(f"{args.citekey} is excluded ({', '.join(item['excluded'])}); attach it in Zotero yourself")
    if item["pdf"] and not args.force:
        die(f"{args.citekey} already has a PDF at {item['pdf']}; pass --force to add another")

    web = Web(cfg)
    name = safe_key(args.citekey) + ".pdf"
    status, body = web.call("POST", "items", json.dumps([{
        "itemType": "attachment", "parentItem": item["key"], "linkMode": "imported_file",
        "title": args.title, "contentType": "application/pdf", "filename": name,
        "tags": [], "relations": {}}]).encode(),
        {"Content-Type": "application/json", "Zotero-Write-Token": os.urandom(16).hex()})
    try:
        att = json.loads(body)["successful"]["0"]["key"] if status == 200 else None
    except (ValueError, KeyError, TypeError):
        att = None
    if not att:
        die(f"Zotero did not create the attachment (HTTP {status}): {body[:300].decode('utf-8', 'replace')}")

    def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
        die(f"{msg}. The attachment {att} on {args.citekey} has no file; delete it in Zotero.")

    form = {"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"}
    status, body = web.call("POST", f"items/{att}/file", urllib.parse.urlencode({
        "md5": hashlib.md5(data).hexdigest(), "filename": name, "filesize": len(data),
        "mtime": int(pdf.stat().st_mtime * 1000)}).encode(), form)
    if status == 413:
        fail("Zotero storage is full (HTTP 413): free space or upgrade the storage plan")
    if status != 200:
        fail(f"upload authorization failed (HTTP {status}: {body[:200].decode('utf-8', 'replace')})")
    auth = json.loads(body)
    if not auth.get("exists"):
        status, _ = web.call("POST", "", auth["prefix"].encode() + data + auth["suffix"].encode(),
                             {"Content-Type": auth["contentType"]}, url=auth["url"])
        if status != 201:
            fail(f"file upload failed (HTTP {status})")
        status, body = web.call("POST", f"items/{att}/file",
                                urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode(), form)
        if status != 204:
            fail(f"registering the upload failed (HTTP {status})")
    print(f"uploaded {name} to {args.citekey} (attachment {att}); "
          f"waiting up to {args.wait}s for Zotero desktop to sync it", flush=True)

    want, api = hashlib.sha256(data).hexdigest(), Local(cfg)
    deadline = time.monotonic() + args.wait
    while True:
        p = api.file_path(att)
        if p and sha256(p) == want:
            print(f"verified on this machine: {p}")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    print(f"not on this machine yet: Zotero downloads it at its next sync (or when opened, if Settings > "
          f"Sync > Download files is 'as needed'). Check later with `zot.py resolve {args.citekey}`.",
          file=sys.stderr)
    sys.exit(3)


# --------------------------------------------------- metadata check and fix

MARKUP = re.compile(r"</?[a-zA-Z][^>]*>|&(?:amp|lt|gt|quot|#\d+);")
VENUE_FIELDS = {"journalArticle": ("publicationTitle",),
                "conferencePaper": ("proceedingsTitle", "conferenceName")}


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", MARKUP.sub("", t).lower()).strip()


def person(c: dict) -> str:
    if c.get("name"):
        return c["name"]
    return ", ".join(x for x in (c.get("lastName", ""), c.get("firstName", "")) if x)


def library_items(cfg: dict) -> tuple[Local, Tree, list[dict]]:
    api = Local(cfg)
    items = [it for it in api.get("items/top")
             if it.get("data", {}).get("itemType") not in ("attachment", "note", "annotation")]
    return api, Tree(api.get("collections")), items


def pick_items(cfg: dict, tree: Tree, items: list[dict], specs: list[str],
               collections: bool = True) -> list[dict]:
    """Items named by item key or citation key, or (with `collections`) every item in a collection."""
    by_key = {it["key"]: it for it in items}
    out: dict[str, dict] = {}
    for s in specs:
        hits = [by_key[s]] if s in by_key else [it for it in items if citekey(it["data"]) == s]
        if not hits and collections and tree.matches(s):
            keys = set(tree.descendants(tree.resolve(s)))
            hits = [it for it in items if keys & set(it["data"].get("collections", []))]
        if not hits:
            die(f"no item with key or citation key {s!r}" + (" and no such collection" if collections else ""))
        for it in hits:
            out.setdefault(it["key"], it)
    return list(out.values())


def blocked_by(cfg: dict, tree: Tree, d: dict) -> list[str]:
    ex_tags = {t.lower() for t in cfg["scope"]["exclude_tags"]}
    banned = tree.excluded(cfg["scope"]["exclude_collections"])
    out = sorted({t.get("tag", "").lower() for t in d.get("tags", [])} & ex_tags)
    if any(c in banned for c in d.get("collections", [])):
        out.append("exclude collection")
    return out


def review(d: dict, has_pdf: bool | None, dups: list[str]) -> tuple[list[str], list[str]]:
    """(problems to fix, notes) for one item's metadata. has_pdf None: not looked at."""
    fix, note = [], []
    t = d.get("title") or ""
    if not t.strip():
        fix.append("title: empty")
    else:
        if MARKUP.search(t):
            fix.append("title: contains markup (HTML/JATS tags or entities)")
        if t != t.strip() or re.search(r"\s{2,}|\n", t):
            fix.append("title: stray whitespace or line breaks")
        if len(t) > 20 and t.isupper():
            fix.append("title: all capitals")
    creators = d.get("creators") or []
    if not creators:
        fix.append("creators: none")
    for i, c in enumerate(creators, 1):
        if c.get("name"):
            continue
        first = (c.get("firstName") or "").strip()
        if MARKUP.search(first + (c.get("lastName") or "")):
            fix.append(f"creator {i} {person(c)!r}: contains markup")
        if not first:
            fix.append(f"creator {i} {person(c)!r}: no first name")
        elif len(first.split()) >= 3:
            note.append(f"creator {i} {person(c)!r}: three or more given names; a compound surname split wrong?")
    if not year(d):
        fix.append("date: no year")
    doi = (d.get("DOI") or "").strip()
    if doi and not re.fullmatch(r"10\.\d+/\S+", doi):
        fix.append(f"DOI: {doi!r} is not a bare DOI (10.xxxx/...)")
    if not citekey(d):
        fix.append("citation key: none (generate it in Zotero)")
    fix.extend(f"possible duplicate of {k}" for k in dups)
    venues = VENUE_FIELDS.get(d.get("itemType", ""), ())
    if venues and not any((d.get(f) or "").strip() for f in venues):
        note.append(f"venue: {' / '.join(venues)} empty")
    if not public_id(d):
        note.append("no DOI or arXiv id")
    if not (d.get("abstractNote") or "").strip():
        note.append("abstract: none")
    if has_pdf is False:
        note.append("PDF: none on this machine")
    return fix, note


def cmd_check(args) -> None:
    """Pull the metadata of the named items so an agent can review it, and flag what looks wrong."""
    cfg = load_config(required=False)
    api, tree, items = library_items(cfg)
    specs = args.item or cfg["scope"]["collections"]
    if not specs:
        die("name item keys, citation keys or a collection, or set [scope] collections in the config")
    by_doi: dict[str, list[str]] = {}
    by_title: dict[str, list[str]] = {}
    for it in items:
        d = it["data"]
        if (d.get("DOI") or "").strip():
            by_doi.setdefault(d["DOI"].strip().lower(), []).append(it["key"])
        if norm_title(d.get("title") or ""):
            by_title.setdefault(norm_title(d["title"]), []).append(it["key"])
    rows = []
    for it in pick_items(cfg, tree, items, specs):
        d, key = it["data"], it["key"]
        blocked = blocked_by(cfg, tree, d)
        has_pdf = None
        if not blocked:
            has_pdf = any(ch.get("data", {}).get("contentType") == "application/pdf"
                          and ch["data"].get("linkMode") != "linked_url" and api.file_path(ch["key"])
                          for ch in api.get(f"items/{key}/children"))
        dups = set(by_doi.get((d.get("DOI") or "").strip().lower(), []) if d.get("DOI") else [])
        dups |= set(by_title.get(norm_title(d.get("title") or ""), []))
        fix, note = review(d, has_pdf, sorted(dups - {key}))
        venue = next((d[f] for f in VENUE_FIELDS.get(d.get("itemType", ""), ()) if d.get(f)), "") \
            or d.get("publicationTitle") or d.get("proceedingsTitle") or d.get("bookTitle") or ""
        rows.append({"key": key, "citekey": citekey(d), "type": d.get("itemType", ""), "year": year(d),
                     "title": d.get("title", ""), "creators": [person(c) for c in d.get("creators", [])],
                     "venue": venue, "id": public_id(d), "abstract_chars": len((d.get("abstractNote") or "").strip()),
                     "pdf": has_pdf, "excluded": blocked,
                     "collections": sorted(tree.path(c) for c in d.get("collections", []) if c in tree.name),
                     "tags": sorted(t.get("tag", "") for t in d.get("tags", [])),
                     "problems": fix, "notes": note})
    rows.sort(key=lambda r: (not r["problems"], (r["citekey"] or r["title"]).lower()))
    if args.json:
        json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        for r in rows:
            print(f"{r['key']}  {r['citekey'] or '(no citation key)'}  {r['type']}  {r['year']}")
            creators = "; ".join(r["creators"][:4]) + (f"; … ({len(r['creators'])})" if len(r["creators"]) > 4 else "")
            for label, value in (("title", repr(r["title"])), ("creators", creators), ("venue", r["venue"]),
                                 ("id", r["id"] or ""),
                                 ("abstract", f"{r['abstract_chars']} chars" if r["abstract_chars"] else "none"),
                                 ("PDF", "excluded, not looked at" if r["excluded"] else "yes" if r["pdf"] else "none"),
                                 ("in", "; ".join(r["collections"])), ("tags", ", ".join(r["tags"]))):
                if value:
                    print(f"  {label:<9}{value}")
            for p in r["problems"]:
                print(f"  ! {p}")
            for n in r["notes"]:
                print(f"  - {n}")
            print()
        bad = sum(bool(r["problems"]) for r in rows)
        print(f"{len(rows)} item(s), {bad} with problems (!), notes (-) are for information.")
    if any(r["problems"] for r in rows):
        sys.exit(2)


def cmd_set(args) -> None:
    """Change one field of one item through the web API, then wait for the desktop copy to match."""
    cfg = load_config()
    api, tree, items = library_items(cfg)
    hits = pick_items(cfg, tree, items, [args.item], collections=False)
    if len(hits) != 1:
        die(f"{len(hits)} items have citation key {args.item!r}; name it by item key")
    key = hits[0]["key"]
    blocked = blocked_by(cfg, tree, hits[0]["data"])
    if blocked:
        die(f"{args.item} is excluded ({', '.join(blocked)}); edit it in Zotero yourself")
    value = sys.stdin.read() if args.value == "-" else args.value
    value = value.strip()

    web = Web(cfg)
    status, body = web.call("GET", f"items/{key}")
    if status != 200:
        die(f"cannot read {key} from the web API (HTTP {status}); has Zotero synced it?")
    got = json.loads(body)
    data, version = got["data"], got["version"]
    m = re.fullmatch(r"creator:(\d+)", args.field)
    if m:
        creators = [dict(c) for c in data.get("creators", [])]
        i = int(m.group(1)) - 1
        if not 0 <= i < len(creators):
            die(f"{key} has {len(creators)} creator(s); creator:{i + 1} does not exist")
        last, sep, first = value.partition(",")
        if not sep or not last.strip() or not first.strip():
            die('a creator is given as "Last, First"')
        old = person(creators[i])
        new_c = {"creatorType": creators[i].get("creatorType", "author"),
                 "lastName": last.strip(), "firstName": first.strip()}
        if new_c == creators[i]:
            print(f"{key} creator:{i + 1} is already {old!r}; nothing to do")
            return
        creators[i] = new_c
        patch, shown, want = {"creators": creators}, (old, person(new_c)), creators
        field = "creators"
    else:
        field = args.field
        if field in ("key", "version", "itemType", "dateAdded", "dateModified", "parentItem") \
                or not isinstance(data.get(field), str):
            die(f"{field!r} is not a text field of this {data.get('itemType')} item")
        if data[field] == value:
            print(f"{key} {field} is already that value; nothing to do")
            return
        patch, shown, want = {field: value}, (data[field], value), value
    status, body = web.call("PATCH", f"items/{key}", json.dumps(patch).encode(),
                            {"Content-Type": "application/json", "If-Unmodified-Since-Version": str(version)})
    if status == 412:
        die(f"{key} changed in Zotero since it was read; run the command again")
    if status != 204:
        die(f"Zotero refused the change (HTTP {status}): {body[:300].decode('utf-8', 'replace')}")
    print(f"{key} {args.field}: {shown[0]!r} -> {shown[1]!r}", flush=True)

    deadline = time.monotonic() + args.wait
    while True:
        local = api.get(f"items/{key}").get("data", {})
        if local.get(field) == want:
            print("synced to Zotero desktop")
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    print("saved online; Zotero desktop has not synced it yet (it will at its next sync)", file=sys.stderr)
    sys.exit(3)


def cmd_record(args) -> None:
    """Finalize a conversion done by hand, so status can report done/stale."""
    cfg = load_config()
    hits = lookup(cfg, args.citekey)
    if len(hits) != 1:
        die(f"{len(hits)} items have citation key {args.citekey!r}; expected exactly one")
    h = hits[0]
    if h["excluded"]:
        die(f"{args.citekey} is excluded ({', '.join(h['excluded'])}); nothing recorded")
    if not h["pdf"] or not h["outdir"]:
        die("no PDF on this machine, or [vault] path is not set")
    outdir = Path(h["outdir"])
    if not (outdir / f"{safe_key(args.citekey)}.md").exists():
        die(f"no {safe_key(args.citekey)}.md in {outdir}; convert first "
            f"(pdfc.py convert PDF -o {outdir} --name {safe_key(args.citekey)})")
    finalize(cfg, h, outdir, args.preset, args.converter)
    print(f"recorded {outdir / 'meta.json'}")


# ------------------------------------------------------- batch conversion

def pdfc_command(cfg: dict) -> list[str]:
    custom = cfg["convert"].get("pdfc_command") or ""
    if custom:
        if not isinstance(custom, str):
            return list(custom)
        if os.name == "nt":  # POSIX splitting would eat the backslashes in Windows paths
            return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t
                    for t in shlex.split(custom, posix=False)]
        return shlex.split(custom)
    script = Path(__file__).resolve().parent.parent / "pdf-convert" / "pdfc.py"
    if not script.exists():
        die(f"pdf-convert skill not found at {script}; set [convert] pdfc_command")
    uv = shutil.which("uv")
    return [uv, "run", "--quiet", str(script)] if uv else [sys.executable, str(script)]


def run_pdfc(cfg: dict, args: list[str], echo: bool = True) -> tuple[int, str]:
    """Run pdfc.py, echo its output indented, return (exit code, combined output)."""
    try:
        proc = subprocess.Popen(pdfc_command(cfg) + args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    except OSError as e:
        die(f"cannot run pdf-convert: {e}")
    lines = []
    assert proc.stdout is not None
    with proc.stdout:
        for line in proc.stdout:
            lines.append(line)
            if echo:
                print("    " + line.rstrip("\n"), flush=True)
    return proc.wait(), "".join(lines)


def estimate(cfg: dict, rows: list[dict], preset: str, root: Path) -> tuple[float, list[dict]]:
    """pdfc.py --dry-run per item: cost estimates, nothing sent. Drops items over the page limit."""
    total, ok = 0.0, []
    for r in rows:
        rc, out = run_pdfc(cfg, ["convert", r["pdf"], "--dry-run", "--preset", preset,
                                 "--name", safe_key(r["citekey"]),
                                 "-o", str(root / "fulltext" / safe_key(r["citekey"]))], echo=False)
        m = re.search(r"cost: about \$([\d.]+)", out)
        pages = re.search(r"all (\d+) pages", out)
        if rc == 4:
            r["estimate"], r["note"] = None, "over pdf-convert's page limit; convert it on its own"
            print(f"  skip  {r['citekey']}: {r['note']}")
            continue
        if rc != 0:
            die(f"pdf-convert dry run failed for {r['citekey']}:\n{out}")
        r["estimate"] = 0.0 if "cached result" in out else float(m.group(1)) if m else None
        r["pages"] = int(pages.group(1)) if pages else None
        total += r["estimate"] or 0.0
        ok.append(r)
    return total, ok


def cmd_index(args) -> None:
    cfg = load_config()
    root = pipeline_root(cfg)
    if root is None:
        die("[vault] path is not set in the config")
    if not root.parent.is_dir():
        die(f"vault folder does not exist: {root.parent}")
    preset = args.preset or cfg["convert"]["preset"]
    rows = gather(cfg, args.collection or None)
    todo = [r for r in rows if r["status"] in ("ready", "stale")]
    if args.keys:
        wanted = set(args.keys)
        known = {r["key"] for r in rows} | {r["citekey"] for r in rows if r["citekey"]}
        for k in sorted(wanted - known):
            print(f"  note: {k} is not in scope")
        for r in rows:
            if (r["key"] in wanted or r["citekey"] in wanted) and r not in todo:
                print(f"  note: {r['citekey'] or r['key']} is {r['status']}, not converted")
        todo = [r for r in todo if r["key"] in wanted or r["citekey"] in wanted]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("Nothing to convert.")
        write_notes(cfg, root)
        return
    print(f"Estimating {len(todo)} item(s) with pdf-convert --dry-run ({preset} preset); nothing is sent.")
    total, todo = estimate(cfg, todo, preset, root)
    for r in todo:
        est = "?" if r["estimate"] is None else f"${r['estimate']:.4f}"
        print(f"  {r['status']:<6} {r['citekey']:<28} {str(r.get('pages') or '?'):>4} pp  {est:>8}  {r['title'][:50]}")
    print(f"Total: {len(todo)} item(s), about ${total:.2f} at list price.")
    if not todo:
        return
    if not args.yes:
        no_terminal = "Not converting: pass --yes once the user has seen and agreed to this list."
        if not sys.stdin.isatty():
            print(no_terminal, file=sys.stderr)
            sys.exit(4)
        try:
            answer = input(f"Convert {len(todo)} item(s) for about ${total:.2f}? [y/N] ")
        except EOFError:  # Windows reports NUL as a terminal
            print("\n" + no_terminal, file=sys.stderr)
            sys.exit(4)
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled; nothing was sent.")
            return
    pending = load_pending()
    done, spent, code = 0, 0.0, 0
    for i, r in enumerate(todo, 1):
        outdir = root / "fulltext" / safe_key(r["citekey"])
        print(f"[{i}/{len(todo)}] {r['citekey']}", flush=True)
        rc, out = run_pdfc(cfg, ["convert", r["pdf"], "-o", str(outdir), "--preset", preset,
                                 "--name", safe_key(r["citekey"])])
        if rc == 0:
            meta = finalize(cfg, r, outdir, preset)
            done += 1
            spent += meta.get("cost_usd") or 0.0
        elif rc == 3:
            m = re.search(r"submitted: request (\S+)", out)
            pending[r["citekey"]] = {"request_id": m.group(1) if m else None, "item_key": r["key"],
                                     "outdir": str(outdir), "preset": preset, "since": now_iso()}
            save_pending(pending)
            print(f"    pending: collect it later with `zot.py fetch-pending` (it is not converted again)")
            code = 3
        elif rc == 4:
            print("Stopped: a pdf-convert guard refused (page limit or budget). Ask the user before changing it.")
            code = 4
            break
        else:
            print(f"Stopped: pdf-convert failed on {r['citekey']} (exit {rc}).")
            code = 1
            break
    write_notes(cfg, root)
    print(f"\nConverted {done} of {len(todo)}; reported cost ${spent:.4f}."
          + (f" Pending: {sum(1 for _ in pending)}." if pending else ""))
    if code:
        sys.exit(code)


def cmd_fetch_pending(args) -> None:
    cfg = load_config()
    root = pipeline_root(cfg)
    pending = load_pending()
    if not pending:
        print("Nothing pending.")
        return
    code = 0
    for ck, p in sorted(pending.items()):
        if not p.get("request_id"):
            print(f"{ck}: no request id was recorded; check `pdfc.py usage` and convert it by hand")
            continue
        print(f"{ck}: fetching request {p['request_id']}", flush=True)
        rc, _ = run_pdfc(cfg, ["fetch", p["request_id"], "-o", p["outdir"]])
        if rc == 0:
            hits = [h for h in lookup(cfg, ck) if h["key"] == p["item_key"]]
            if hits and hits[0]["pdf"]:
                finalize(cfg, hits[0], Path(p["outdir"]), p.get("preset", "standard"))
            del pending[ck]
            save_pending(pending)
        elif rc == 3:
            print("    still processing; try again later")
            code = 3
        else:
            print(f"    fetch failed (exit {rc}); left in the pending list")
            code = 1
    if root:
        write_notes(cfg, root)
    sys.exit(code)


# --------------------------------------------------------- vault notes

def note_name(path: str) -> str:
    return re.sub(r'[\\:*?"<>|]+', "_", path.replace("/", " - ")).strip() or "collection"


def write_notes(cfg: dict, root: Path) -> None:
    """One generated note per collection in scope, plus a README for the folder."""
    tree = Tree(Local(cfg).get("collections"))
    scoped: list[str] = []
    for spec in cfg["scope"]["collections"]:
        for k in tree.descendants(tree.resolve(spec)):
            if k not in scoped:
                scoped.append(k)
    banned = tree.excluded(cfg["scope"]["exclude_collections"])
    rows = gather(cfg)
    by_path: dict[str, list[dict]] = {tree.path(k): [] for k in scoped if k not in banned}
    for r in rows:
        for c in r["collections"]:
            if c in by_path:
                by_path[c].append(r)
    outdir = root / "collections"
    outdir.mkdir(parents=True, exist_ok=True)
    rel = root.name
    for path, items in by_path.items():
        lines = ["---", f"zotero_collection: {json.dumps(path)}", f"generated: {json.dumps(now_iso()[:10])}",
                 "tags: [zotero-robot/collection]", "---", "",
                 f"# {path.split('/')[-1]}", "",
                 "Generated by zotero-robot from Zotero; edits here are overwritten.", ""]
        if not items:
            lines.append("No items directly in this collection.")
        else:
            lines += ["| Paper | Year | Title | Status | Card | Zotero |", "|---|---|---|---|---|---|"]
            for r in sorted(items, key=lambda r: (r["citekey"] or r["title"]).lower()):
                ck = r["citekey"]
                sk = safe_key(ck) if ck else ""
                paper = f"[[{rel}/fulltext/{sk}/{sk}\\|{ck}]]" if ck and r["status"] == "done" else (ck or "-")
                card = f"[[{rel}/cards/{sk}\\|card]]" if ck and (root / "cards" / f"{sk}.md").exists() else ""
                title = r["title"].replace("|", "/")
                link = f"[open]({select_link(cfg, r['key'])})"
                lines.append(f"| {paper} | {r['year']} | {title} | {r['status']} | {card} | {link} |")
        (outdir / f"{note_name(path)}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# _zotero-robot\n\nGenerated by the zotero-robot skills. Files here are rewritten by later\n"
            "runs, so do not edit them; write your own notes elsewhere and link here.\n\n"
            "- `fulltext/<citekey>/`: converted papers with figures\n"
            "- `cards/<citekey>.md`: one structured summary per paper\n"
            "- `collections/`: one generated note per Zotero collection in scope\n"
            "- `digests/`: results of scheduled checks\n", encoding="utf-8")


def cmd_notes(args) -> None:
    cfg = load_config()
    root = pipeline_root(cfg)
    if root is None:
        die("[vault] path is not set in the config")
    write_notes(cfg, root)
    print(f"wrote collection notes in {root / 'collections'}")


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

    s = sub.add_parser("index", help="estimate, confirm, then convert ready/stale items into the vault")
    s.add_argument("collection", nargs="*", help="default: the collections in the config")
    s.add_argument("--keys", nargs="+", metavar="KEY", help="only these item keys or citation keys")
    s.add_argument("--limit", type=int, help="convert at most this many")
    s.add_argument("--preset", choices=["text", "standard", "full"], help="default: [convert] preset")
    s.add_argument("--yes", action="store_true", help="skip the prompt (only after the user agreed to the list)")
    s.set_defaults(fn=cmd_index)

    sub.add_parser("fetch-pending", help="collect conversions that outlived pdf-convert's wait"
                   ).set_defaults(fn=cmd_fetch_pending)
    sub.add_parser("notes", help="rewrite the generated collection notes in the vault").set_defaults(fn=cmd_notes)

    s = sub.add_parser("resolve", help="citation key -> item key, PDF path, Zotero link")
    s.add_argument("citekey")
    s.set_defaults(fn=cmd_resolve)

    s = sub.add_parser("record", help="after converting: write meta.json so status shows done/stale")
    s.add_argument("citekey")
    s.add_argument("--converter", default="datalab")
    s.add_argument("--preset", default="standard")
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("attach", help="upload a PDF you obtained to an existing item (web API), then verify it synced")
    s.add_argument("citekey")
    s.add_argument("pdf")
    s.add_argument("--title", default="Full Text PDF", help="attachment title in Zotero")
    s.add_argument("--wait", type=int, default=120, help="seconds to wait for Zotero desktop to sync it (default 120)")
    s.add_argument("--force", action="store_true", help="attach even if the item already has a PDF")
    s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("check", help="show items' metadata and flag what looks wrong (exit 2 if anything to fix)")
    s.add_argument("item", nargs="*", help="item keys, citation keys or collections (default: the scope)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("set", help="change one field of one item (web API), then verify it synced")
    s.add_argument("item", help="item key or citation key")
    s.add_argument("field", help='a text field such as title, abstractNote, date, DOI; or creator:N for "Last, First"')
    s.add_argument("value", help="the new value; - reads it from stdin (for abstracts)")
    s.add_argument("--wait", type=int, default=60, help="seconds to wait for Zotero desktop to sync it (default 60)")
    s.set_defaults(fn=cmd_set)

    for name in ("approve", "unapprove"):
        s = sub.add_parser(name, help=f"{name} held items by item key")
        s.add_argument("key", nargs="+")
        s.set_defaults(fn=cmd_approve)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    if os.name == "nt":  # piped output is cp1252 there, which cannot hold titles and names
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()
