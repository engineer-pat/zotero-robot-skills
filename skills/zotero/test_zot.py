"""Tests for zot.py against a fake Zotero local API. Standard library only.

Run:  python3 -m unittest discover -s skills/zotero -p 'test_*.py'
"""
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zot  # noqa: E402


FAKE_PDFC = r"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["FAKE_PDFC_LOG"])
plan = json.loads(os.environ.get("FAKE_PDFC_PLAN", "{}"))
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def write(out, name, rid):
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.md").write_text(f"# {name}\n\n![](_page_1_Figure_0.jpeg)\nbody\n")
    (out / "_page_1_Figure_0.jpeg").write_bytes(b"img")
    (out / f"{name}.meta.json").write_text(json.dumps({"page_count": 12, "cost_usd": 0.048, "request_id": rid}))
cmd = args[0]
with log.open("a") as f:
    f.write(json.dumps(args) + "\n")
if cmd == "convert":
    name = opt("--name")
    what = plan.get(name, "ok")
    if "--dry-run" in args:
        if what == "toolong":
            print("refused: 400 pages is over the per-call limit", file=sys.stderr); sys.exit(4)
        print(f"{name}.pdf: all 12 pages; mode balanced, images; formats md")
        print("cost: about $0.0480 (list price); 30-day spend $0.0000 of $5.00 budget")
        sys.exit(0)
    if what == "pending":
        print(f"submitted: request R-{name}", file=sys.stderr)
        print("still processing after 600s.", file=sys.stderr); sys.exit(3)
    if what == "refuse":
        print("refused: over the budget", file=sys.stderr); sys.exit(4)
    if what == "fail":
        print("conversion failed", file=sys.stderr); sys.exit(1)
    write(Path(opt("-o")), name, f"R-{name}"); print("pages converted: 12"); sys.exit(0)
if cmd == "fetch":
    rid = args[1]
    if plan.get("fetch") == "pending":
        sys.exit(3)
    write(Path(opt("-o")), rid[2:], rid); sys.exit(0)
"""


def col(key, name, parent=None, n=0):
    return {"key": key, "data": {"name": name, "parentCollection": parent or False}, "meta": {"numItems": n}}


def item(key, title, cols, **data):
    base = {"itemType": "journalArticle", "title": title, "date": "2021-03-01",
            "collections": cols, "tags": [], "extra": ""}
    base.update(data)
    return {"key": key, "data": base, "meta": {"numChildren": 1}}


class Fake:
    """Fixture library: AFRL Research > {DLTS, exclude}, plus Courses outside the scope."""

    def __init__(self, tmp: Path):
        self.pdfs = {}
        for k in ("A1", "A2", "A3", "A4", "A5", "A7", "A8"):
            p = tmp / f"{k} paper.pdf"
            p.write_bytes(b"%PDF-" + k.encode())
            self.pdfs["ATT" + k] = p
        self.collections = [col("C1", "AFRL Research", n=5), col("C2", "DLTS", "C1", 2),
                            col("C3", "exclude", "C1", 1), col("C9", "Courses", n=1)]
        self.items = {
            "C1": [item("A1", "Ready paper", ["C1", "C2"], citationKey="smith2021", DOI="10.1/x"),
                   item("A2", "Tagged draft", ["C1"], citationKey="doe2022", DOI="10.1/y",
                        tags=[{"tag": "No-Upload"}]),
                   item("A3", "Report with no DOI", ["C1"], citationKey="lab2020", itemType="report"),
                   item("A4", "No key", ["C1"], DOI="10.1/z"),
                   item("A6", "No PDF", ["C1"], citationKey="nopdf2019", DOI="10.1/w")],
            "C2": [item("A1", "Ready paper", ["C1", "C2"], citationKey="smith2021", DOI="10.1/x"),
                   item("A5", "Key in Extra, arXiv", ["C2"], extra="Citation Key: lee2023\narXiv: 2301.00001"),
                   item("A8", "In DLTS and in exclude", ["C2", "C3"], citationKey="both2020", DOI="10.1/b")],
            "C3": [item("A7", "Draft in exclude folder", ["C3"], citationKey="draft2024", DOI="10.1/v")],
            "C9": [item("B1", "Textbook", ["C9"], citationKey="book1999", itemType="book")],
        }
        self.hits = []

    def children(self, key):
        att = "ATT" + key
        if att not in self.pdfs:
            return []
        return [{"key": att, "data": {"itemType": "attachment", "contentType": "application/pdf",
                                      "linkMode": "imported_file"}}]


def serve(fake):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            fake.hits.append(path)
            parts = path.split("/")[4:]  # after /api/users/0
            body, ctype = None, "application/json"
            if parts == ["collections"]:
                body = fake.collections
            elif len(parts) == 4 and parts[0] == "collections" and parts[2:] == ["items", "top"]:
                body = fake.items.get(parts[1], [])
            elif parts == ["items", "top"]:
                seen = {}
                for lst in fake.items.values():
                    for it in lst:
                        seen.setdefault(it["key"], it)
                body = list(seen.values())
            elif len(parts) == 2 and parts[0] == "items":
                body = next((it for lst in fake.items.values() for it in lst if it["key"] == parts[1]), None)
            elif len(parts) == 3 and parts[0] == "items" and parts[2] == "children":
                body = fake.children(parts[1])
            elif len(parts) == 5 and parts[2:] == ["file", "view", "url"]:
                p = fake.pdfs.get(parts[1])
                if p:
                    body, ctype = p.as_uri(), "text/plain"
            if body is None:
                self.send_error(404)
                return
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = Fake(self.tmp)
        self.srv = serve(self.fake)
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        cfgdir = self.tmp / "cfg"
        cfgdir.mkdir()
        self.cfgfile = cfgdir / "config.toml"
        self.cfgfile.write_text(
            f'[zotero]\nlocal_api = "http://127.0.0.1:{self.srv.server_address[1]}/api"\n'
            f"api_key_file = '{self.tmp / 'no_api_key'}'\n"                # never the machine's real key
            f'[scope]\ncollections = ["AFRL Research"]\n'
            f"[vault]\npath = '{self.vault}'\n", encoding="utf-8")
        os.environ["ZOTERO_ROBOT_CONFIG"] = str(self.cfgfile)
        os.environ.pop("ZOTERO_API_KEY", None)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def rows(self):
        return {r["key"]: r for r in zot.gather(zot.load_config())}

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            zot.main(list(argv))
        return out.getvalue()


class ZotTest(Base):
    def test_classification(self):
        r = self.rows()
        self.assertEqual(r["A1"]["status"], "ready")
        self.assertEqual(r["A2"]["status"], "excluded")       # tag match is case-insensitive
        self.assertEqual(r["A3"]["status"], "held")           # report, no DOI
        self.assertIn("type report", r["A3"]["reason"])
        self.assertEqual(r["A4"]["status"], "no-key")
        self.assertEqual(r["A6"]["status"], "no-pdf")
        self.assertEqual(r["A5"]["status"], "ready")          # key from Extra, arXiv id counts as public
        self.assertEqual(r["A5"]["citekey"], "lee2023")

    def test_scope_and_exclusion_win(self):
        r = self.rows()
        self.assertNotIn("B1", r)                              # Courses is outside the allowlist
        self.assertNotIn("A7", r)                              # only in the exclude collection: never listed
        self.assertEqual(r["A8"]["status"], "excluded")        # in scope AND in exclude: exclusion wins
        self.assertNotIn("/api/users/0/collections/C3/items/top", self.fake.hits)
        self.assertNotIn("/api/users/0/items/A2/children", self.fake.hits)  # excluded: PDF never looked up
        self.assertEqual(len(r), 7)                            # A1 appears once though it is in two collections

    def test_approve_releases_held_item(self):
        zot.save_approved({"A3"})
        self.assertEqual(self.rows()["A3"]["status"], "ready")
        self.run_cli("unapprove", "A3")
        self.assertEqual(self.rows()["A3"]["status"], "held")

    def test_done_and_stale_follow_pdf_hash(self):
        pdf = self.fake.pdfs["ATTA1"]
        meta = self.vault / "_zotero-robot" / "fulltext" / "smith2021" / "meta.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps({"source_sha256": zot.sha256(pdf)}))
        self.assertEqual(self.rows()["A1"]["status"], "done")
        pdf.write_bytes(b"%PDF-changed")
        self.assertEqual(self.rows()["A1"]["status"], "stale")

    def test_dry_run_lists_only_sendable(self):
        out = self.run_cli("dry-run")
        sent, not_sent = out.split("NOT SENT")
        self.assertIn("smith2021", sent)
        self.assertIn("lee2023", sent)
        for k in ("doe2022", "lab2020", "both2020"):
            self.assertNotIn(k, sent)
            self.assertIn(k, not_sent)
        self.assertIn("Nothing was sent", out)

    def test_long_citekey_keeps_year_apart(self):
        self.fake.items["C1"][0]["data"]["citationKey"] = "alsaadDCCharacteristicsDynamic2024"
        self.assertIn("alsaadDCCharacteristicsDynamic2024  2021", self.run_cli("status"))

    def test_resolve(self):
        d = json.loads(self.run_cli("resolve", "smith2021"))
        self.assertEqual(d["key"], "A1")
        self.assertTrue(d["pdf"].endswith("A1 paper.pdf"))     # space in the file name survives the file:// URL
        blocked = json.loads(self.run_cli("resolve", "doe2022"))
        self.assertEqual(blocked["excluded"], ["no-upload"])
        self.assertIsNone(blocked["pdf"])                      # excluded items give no path
        self.assertIsNone(blocked["outdir"])
        self.assertEqual(json.loads(self.run_cli("resolve", "both2020"))["excluded"], ["exclude collection"])

    def test_record_marks_done_and_refuses_excluded(self):
        outdir = Path(json.loads(self.run_cli("resolve", "smith2021"))["outdir"])
        self.assertEqual(outdir, self.vault / "_zotero-robot" / "fulltext" / "smith2021")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            zot.main(["record", "smith2021"])                  # nothing converted yet
        outdir.mkdir(parents=True)
        (outdir / "smith2021.md").write_text("# paper")
        self.run_cli("record", "smith2021", "--preset", "full")
        self.assertEqual(json.loads((outdir / "meta.json").read_text())["preset"], "full")
        self.assertEqual(self.rows()["A1"]["status"], "done")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            zot.main(["record", "doe2022"])

    def test_collection_tree_and_ambiguity(self):
        out = self.run_cli("collections")
        self.assertIn("AFRL Research  [C1]  5 items  <- in scope", out)
        self.assertIn("exclude  [C3]  1 items  <- excluded", out)
        tree = zot.Tree(self.fake.collections + [col("C8", "DLTS", "C9")])
        self.assertEqual(tree.resolve("AFRL Research/DLTS"), "C2")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            tree.resolve("DLTS")

    def test_unknown_scope_still_lists_tree(self):
        self.cfgfile.write_text(self.cfgfile.read_text().replace('"AFRL Research"', '"AFRL Projects"'))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = self.run_cli("collections")                  # the tree is what you need to fix the name
        self.assertIn("AFRL Research  [C1]", out)
        self.assertNotIn("in scope", out)
        self.assertIn("did you mean: AFRL Research?", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.assertRaises(SystemExit):
            zot.main(["doctor"])
        self.assertIn("[miss] scope: no collection named 'AFRL Projects'", out.getvalue())

    def test_select_link_follows_library(self):
        cfg = zot.load_config()
        self.assertEqual(zot.select_link(cfg, "A1"), "zotero://select/library/items/A1")
        cfg["zotero"]["library"] = "groups/6639135"
        self.assertEqual(zot.select_link(cfg, "A1"), "zotero://select/groups/6639135/items/A1")

    def test_file_url_paths(self):
        if os.name == "nt":
            self.assertEqual(zot.file_url_to_path("file:///C:/Users/p/My%20Paper.pdf"),
                             Path(r"C:\Users\p\My Paper.pdf"))
        else:
            self.assertEqual(zot.file_url_to_path("file:///home/p/My%20Paper.pdf"), Path("/home/p/My Paper.pdf"))
        self.assertIsNone(zot.file_url_to_path("https://example.org/a.pdf"))

    def test_key_is_never_printed(self):
        os.environ["ZOTERO_API_KEY"] = "SECRETKEY123"
        zot.verify_key = lambda k: {"userID": 42, "access": {"user": {"library": True, "write": True}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.assertRaises(SystemExit):
            zot.main(["doctor"])
        self.assertNotIn("SECRETKEY123", out.getvalue())
        self.assertIn("userID 42", out.getvalue())

    def test_zotero_not_running(self):
        self.srv.shutdown()
        self.srv.server_close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            zot.gather(zot.load_config())
        self.assertIn("Is the Zotero desktop app running", err.getvalue())
        self.srv = serve(self.fake)


class IndexTest(Base):
    def setUp(self):
        super().setUp()
        fake = self.tmp / "fake_pdfc.py"
        fake.write_text(FAKE_PDFC)
        self.log = self.tmp / "pdfc.log"
        os.environ["FAKE_PDFC_LOG"] = str(self.log)
        os.environ["FAKE_PDFC_PLAN"] = "{}"
        with open(self.cfgfile, "a", encoding="utf-8") as f:
            # A TOML literal string with quoted paths, as a Windows user would write it
            f.write(f"[convert]\npdfc_command = '\"{sys.executable}\" \"{fake}\"'\n")
        self.root = self.vault / "_zotero-robot"

    def calls(self, kind="convert", dry=False):
        if not self.log.exists():
            return []
        out = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [a for a in out if a[0] == kind and (("--dry-run" in a) == dry)]

    def index(self, *extra, expect=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                zot.main(["index", *extra])
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, expect, out.getvalue())
        return out.getvalue()

    def test_refuses_without_confirmation(self):
        out = self.index(expect=4)                      # stdin is not a terminal under unittest
        self.assertIn("Total: 2 item(s), about $0.10", out)
        self.assertEqual(self.calls(), [])              # estimates only, nothing converted
        self.assertEqual(len(self.calls(dry=True)), 2)

    def test_converts_ready_items_only(self):
        out = self.index("--yes")
        sent = sorted(a[a.index("--name") + 1] for a in self.calls())
        self.assertEqual(sent, ["lee2023", "smith2021"])  # held, excluded, no-key, no-pdf never sent
        md = (self.root / "fulltext" / "smith2021" / "smith2021.md").read_text()
        self.assertTrue(md.startswith('---\ncitekey: "smith2021"\n'))
        self.assertIn('zotero: "zotero://select/library/items/A1"', md)
        self.assertIn("![](_page_1_Figure_0.jpeg)", md)   # figure links untouched
        meta = json.loads((self.root / "fulltext" / "smith2021" / "meta.json").read_text())
        self.assertEqual((meta["pages"], meta["cost_usd"], meta["item_key"]), (12, 0.048, "A1"))
        self.assertEqual(len((self.root / "manifest.jsonl").read_text().splitlines()), 2)
        self.assertIn("Converted 2 of 2", out)
        r = self.rows()
        self.assertEqual((r["A1"]["status"], r["A5"]["status"]), ("done", "done"))
        self.index("--yes")                              # second run: nothing left, nothing sent again
        self.assertEqual(len(self.calls()), 2)

    def test_collection_notes(self):
        self.index("--yes")
        note = (self.root / "collections" / "AFRL Research.md").read_text()
        self.assertIn("[[_zotero-robot/fulltext/smith2021/smith2021\\|smith2021]]", note)
        self.assertIn("| doe2022 | 2021 | Tagged draft | excluded |", note)   # listed, never linked
        sub = (self.root / "collections" / "AFRL Research - DLTS.md").read_text()
        self.assertIn("lee2023", sub)
        self.assertFalse((self.root / "collections" / "AFRL Research - exclude.md").exists())
        self.assertTrue((self.root / "README.md").exists())

    def test_keys_and_limit(self):
        self.index("--yes", "--keys", "lee2023", "lab2020")
        self.assertEqual([a[a.index("--name") + 1] for a in self.calls()], ["lee2023"])
        self.index("--yes", "--limit", "1")
        self.assertEqual(len(self.calls()), 2)

    def test_pending_is_never_converted_twice(self):
        os.environ["FAKE_PDFC_PLAN"] = json.dumps({"smith2021": "pending"})
        self.index("--yes", expect=3)
        self.assertEqual(self.rows()["A1"]["status"], "pending")
        self.assertEqual(zot.load_pending()["smith2021"]["request_id"], "R-smith2021")
        self.index("--yes")                              # rerun skips the pending item
        self.assertEqual(sum(1 for a in self.calls() if "smith2021" in a), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            zot.main(["fetch-pending"])
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(self.calls("fetch")[0][1], "R-smith2021")
        self.assertEqual(self.rows()["A1"]["status"], "done")
        self.assertEqual(zot.load_pending(), {})

    def test_guard_stops_the_run(self):
        os.environ["FAKE_PDFC_PLAN"] = json.dumps({"lee2023": "refuse"})
        out = self.index("--yes", expect=4)
        self.assertIn("guard refused", out)
        self.assertEqual(len(self.calls()), 1)           # lee2023 sorts first; smith2021 never sent

    def test_failure_stops_the_run(self):
        os.environ["FAKE_PDFC_PLAN"] = json.dumps({"lee2023": "fail"})
        self.index("--yes", expect=1)
        self.assertEqual(len(self.calls()), 1)

    def test_over_page_limit_is_skipped_at_estimate(self):
        os.environ["FAKE_PDFC_PLAN"] = json.dumps({"lee2023": "toolong"})
        out = self.index("--yes")
        self.assertIn("over pdf-convert's page limit", out)
        self.assertEqual([a[a.index("--name") + 1] for a in self.calls()], ["smith2021"])

    def test_safe_key_matches_pdfc(self):
        self.assertEqual(zot.safe_key("smith:2021/ab"), "smith_2021_ab")
        self.assertEqual(zot.safe_key("lee2023"), "lee2023")



class AttachTest(Base):
    """`attach` against a fake web API and storage server; the local fake plays the synced desktop."""

    def setUp(self):
        super().setUp()
        self.web, fake, tmp, web = [], self.fake, self.tmp, None
        web = self.web

        class W(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                web.append((self.path, dict(self.headers), body))
                out, code = None, 400
                if self.path == "/users/42/items":
                    out, code = {"successful": {"0": {"key": "NEW1"}}, "failed": {}}, 200
                elif self.path == "/users/42/items/NEW1/file" and body.startswith(b"md5="):
                    port = self.server.server_address[1]
                    out, code = {"url": f"http://127.0.0.1:{port}/upload", "contentType": "multipart/form-data",
                                 "prefix": "PRE", "suffix": "SUF", "uploadKey": "UK"}, 200
                elif self.path == "/upload":
                    synced = tmp / "storage" / "NEW1.pdf"
                    synced.parent.mkdir(exist_ok=True)
                    synced.write_bytes(body[3:-3])
                    fake.pdfs["NEW1"] = synced          # Zotero desktop has synced the file down
                    code = 201
                elif self.path == "/users/42/items/NEW1/file" and body == b"upload=UK":
                    code = 204
                raw = json.dumps(out).encode() if out is not None else b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.websrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), W)
        threading.Thread(target=self.websrv.serve_forever, daemon=True).start()
        self.saved = zot.WEB_API, zot.verify_key
        zot.WEB_API = f"http://127.0.0.1:{self.websrv.server_address[1]}"
        zot.verify_key = lambda k: {"userID": 42}
        os.environ["ZOTERO_API_KEY"] = "SECRETKEY123"
        self.pdf = self.tmp / "download.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nbody\n%%EOF\n")

    def tearDown(self):
        zot.WEB_API, zot.verify_key = self.saved
        os.environ.pop("ZOTERO_API_KEY", None)
        self.websrv.shutdown()
        self.websrv.server_close()
        super().tearDown()

    def attach(self, *argv, expect=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                zot.main(["attach", *argv])
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, expect, out.getvalue())
        self.assertNotIn("SECRETKEY123", out.getvalue())
        return out.getvalue()

    def test_uploads_and_verifies(self):
        out = self.attach("nopdf2019", str(self.pdf), "--wait", "5")
        self.assertIn("verified on this machine", out)
        paths = [w[0] for w in self.web]
        self.assertEqual(paths, ["/users/42/items", "/users/42/items/NEW1/file", "/upload",
                                 "/users/42/items/NEW1/file"])
        created = json.loads(self.web[0][2])[0]
        self.assertEqual((created["parentItem"], created["linkMode"], created["filename"]),
                         ("A6", "imported_file", "nopdf2019.pdf"))
        headers = {k.lower(): v for k, v in self.web[2][1].items()}
        self.assertNotIn("zotero-api-key", headers)      # the key never goes to the storage server
        self.assertEqual(self.web[2][2], b"PRE" + self.pdf.read_bytes() + b"SUF")

    def test_refusals_send_nothing(self):
        html = self.tmp / "login.pdf"
        html.write_text("<html>Sign in</html>")
        self.assertIn("not a PDF", self.attach("nopdf2019", str(html), expect=1))
        self.assertIn("already has a PDF", self.attach("smith2021", str(self.pdf), expect=1))
        self.assertIn("excluded", self.attach("doe2022", str(self.pdf), expect=1))
        self.assertIn("0 items", self.attach("nobody2000", str(self.pdf), expect=1))
        self.assertEqual(self.web, [])


class CheckTest(Base):
    def check(self, *argv, expect=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                zot.main(["check", *argv])
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, expect, out.getvalue())
        return out.getvalue()

    def test_clean_item_passes(self):
        self.fake.items["C1"][0]["data"].update(
            creators=[{"creatorType": "author", "firstName": "Ann", "lastName": "Smith"}],
            abstractNote="An abstract.", publicationTitle="J. Test")
        out = self.check("smith2021")
        self.assertIn("1 item(s), 0 with problems", out)
        self.assertNotIn("abstract: none", out)

    def test_flags_markup_names_and_duplicates(self):
        self.fake.items["C2"].append(item(
            "A9", "Oxide in the Si/SiO\n   <sub>2</sub>   Network", ["C2"], citationKey="ox2021",
            DOI="10.1/x", itemType="conferencePaper",
            creators=[{"creatorType": "author", "firstName": "", "lastName": "Solo"},
                      {"creatorType": "author", "firstName": "Teofilo Reynaldo Cobos", "lastName": "Freire"}]))
        rows = {r["key"]: r for r in json.loads(self.check("ox2021", "--json", expect=2))}
        r = rows["A9"]
        joined = "\n".join(r["problems"])
        self.assertIn("markup", joined)
        self.assertIn("whitespace", joined)
        self.assertIn("creator 1 'Solo': no first name", joined)
        self.assertIn("possible duplicate of A1", joined)    # same DOI as smith2021
        self.assertTrue(any("compound surname" in n for n in r["notes"]))
        self.assertIn("venue: proceedingsTitle / conferenceName empty", r["notes"])
        self.assertIsNone(r["pdf"] if r["excluded"] else None)

    def test_collection_and_excluded(self):
        rows = {r["key"]: r for r in json.loads(self.check("DLTS", "--json", expect=2))}
        self.assertEqual(set(rows), {"A1", "A5", "A8"})
        self.assertEqual(rows["A8"]["excluded"], ["exclude collection"])
        self.assertIsNone(rows["A8"]["pdf"])                 # an excluded item's files are not looked at
        self.assertIn("no such collection", self.check("nothing2000", expect=1))


class SetTest(Base):
    """`set` against a fake web API; a PATCH also updates the local fake, as a desktop sync would."""

    def setUp(self):
        super().setUp()
        self.web, fake = [], self.fake
        web = self.web
        self.version = 7

        def find(key):
            for lst in fake.items.values():
                for it in lst:
                    if it["key"] == key:
                        yield it

        test = self

        class W(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, out=None):
                raw = json.dumps(out).encode() if out is not None else b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                web.append(("GET", self.path, dict(self.headers), b""))
                its = list(find(self.path.rsplit("/", 1)[-1]))
                if not its:
                    return self.reply(404)
                self.reply(200, {"key": its[0]["key"], "version": test.version, "data": its[0]["data"]})

            def do_PATCH(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                web.append(("PATCH", self.path, dict(self.headers), body))
                if self.headers.get("If-Unmodified-Since-Version") != str(test.version):
                    return self.reply(412)
                for it in find(self.path.rsplit("/", 1)[-1]):
                    it["data"].update(json.loads(body))
                self.reply(204)

        self.websrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), W)
        threading.Thread(target=self.websrv.serve_forever, daemon=True).start()
        self.saved = zot.WEB_API, zot.verify_key
        zot.WEB_API = f"http://127.0.0.1:{self.websrv.server_address[1]}"
        zot.verify_key = lambda k: {"userID": 42}
        os.environ["ZOTERO_API_KEY"] = "SECRETKEY123"
        self.fake.items["C1"][0]["data"]["creators"] = [
            {"creatorType": "author", "firstName": "Teofilo Cobos", "lastName": "Freire"}]

    def tearDown(self):
        zot.WEB_API, zot.verify_key = self.saved
        os.environ.pop("ZOTERO_API_KEY", None)
        self.websrv.shutdown()
        self.websrv.server_close()
        super().tearDown()

    def set(self, *argv, expect=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                zot.main(["set", *argv, "--wait", "3"])
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, expect, out.getvalue())
        self.assertNotIn("SECRETKEY123", out.getvalue())
        return out.getvalue()

    def test_sets_title_with_version_check(self):
        out = self.set("smith2021", "title", "Clean title")
        self.assertIn("'Ready paper' -> 'Clean title'", out)
        self.assertIn("synced to Zotero desktop", out)
        method, path, headers, body = self.web[-1]
        self.assertEqual((method, path), ("PATCH", "/users/42/items/A1"))
        self.assertEqual(headers["If-Unmodified-Since-Version"], "7")
        self.assertEqual(json.loads(body), {"title": "Clean title"})

    def test_sets_creator(self):
        out = self.set("A1", "creator:1", "Cobos Freire, Teofilo")
        self.assertIn("'Freire, Teofilo Cobos' -> 'Cobos Freire, Teofilo'", out)
        sent = json.loads(self.web[-1][3])["creators"][0]
        self.assertEqual((sent["lastName"], sent["firstName"], sent["creatorType"]),
                         ("Cobos Freire", "Teofilo", "author"))

    def test_refusals(self):
        self.assertIn("excluded", self.set("doe2022", "title", "x", expect=1))
        self.assertIn("not a text field", self.set("smith2021", "collections", "x", expect=1))
        self.assertIn("creator:3 does not exist", self.set("smith2021", "creator:3", "A, B", expect=1))
        self.assertIn('"Last, First"', self.set("smith2021", "creator:1", "Nocomma", expect=1))
        self.assertIn("no item", self.set("nobody2000", "title", "x", expect=1))
        self.assertFalse([w for w in self.web if w[0] == "PATCH"])

    def test_changed_meanwhile_is_refused(self):
        self.version = 8
        real = zot.Web.call

        def stale(web, method, path, *a, **k):          # read at version 7, then someone else edits
            status, body = real(web, method, path, *a, **k)
            if method == "GET":
                got = json.loads(body)
                got["version"] = 7
                body = json.dumps(got).encode()
            return status, body
        zot.Web.call = stale
        try:
            self.assertIn("changed in Zotero", self.set("smith2021", "title", "x", expect=1))
        finally:
            zot.Web.call = real


if __name__ == "__main__":
    unittest.main()
