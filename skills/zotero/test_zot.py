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
            "C1": [item("A1", "Ready paper", ["C1"], citationKey="smith2021", DOI="10.1/x"),
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


class ZotTest(unittest.TestCase):
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

    def test_file_url_paths(self):
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


if __name__ == "__main__":
    unittest.main()
