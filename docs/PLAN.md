# zotero-robot-skills: implementation plan and handoff

For an agent session picking this work up on the owner's machine (Claude Code or similar).
Read this, then `AGENTS.md`, then `skills/zotero/SKILL.md`. Update the status table when a
phase finishes. This file is specific to this fork and stays out of any pull request upstream.

## Goal

Let agent sessions use the owner's Zotero library as full-text, citable material and write
results into their Obsidian vault, on top of the four upstream skills (openalex, papis,
pdf-convert, figure-craft), which stay unchanged.

## Fixed decisions

- **Repo:** fork of `Vesalon/assistant_skills`, MIT. Follow `AGENTS.md`: vendor-neutral,
  machine-neutral, no secrets, a README row and an `install.sh` check per new skill.
- **Where it runs:** the owner's own machine. Zotero's local API, the keys and the vault exist
  only there. Cloud sandboxes cannot reach OpenAlex, Datalab or Zotero.
- **Zotero is home.** Reads: local API. Writes: web API with the key stored by `zot.py setup`
  (Zotero 10+ also allows local writes after a user prompt; not used yet).
- **Keyed by citation key** (Zotero 8+ native field, filled by Better BibTeX), because the
  owner cites in Obsidian with `[@citekey]` through the Zotero Integration plugin.
- **Scope:** an allowlist of collections in the per-machine config. Never the whole library.
- **Exclusion:** tag `no-upload` or a collection named `exclude`. Exclusion always wins and
  means no conversion and no reading by an agent unless the owner names the item.
- **Holds:** no DOI/arXiv id, or type manuscript/report/document/thesis, waits for `approve`.
- **Conversion:** Datalab through the unchanged `pdfc.py`, `standard` preset. Planned: try the
  arXiv LaTeX source first when a paper has one. Output layout is backend-neutral so a local
  converter can be added later. Local GPU conversion is deferred: the whole library costs
  about $40 through the API.
- **Output:** one pipeline-owned folder in the vault, `_zotero-robot/` (layout in the skill).
  The vault copy is the only copy by default; attaching markdown to Zotero items is an option
  (`[convert] attach_to_zotero`).
- **ZotFlow** (Obsidian plugin) is optional. Nothing depends on it. No fork of it.
- **Licensing:** only MIT-licensed material may be adapted, with attribution in the file that
  uses it. Planned adaptation: the citation-verification procedure from
  `orchestra-research/AI-research-SKILLs` (MIT). `lingzhi227/agent-research-skills` has no
  license: take nothing from it; write the citekey validator and arXiv source fetch fresh.

## Status

| Phase | Work | State |
|---|---|---|
| 0 | Machine setup (below) | done 2026-11-18: skills linked into `~/.claude/skills`, config written, `doctor` and `pdfc.py doctor` pass |
| 1 | `zotero` skill, read-only: config, doctor, collections, status, dry-run, resolve, record, approve | verified against Zotero 10.0.7 on 2026-11-18 (results below); 22 tests pass |
| 2 | Batch conversion: `index` (estimate, confirm, convert, frontmatter, `meta.json`, manifest), `fetch-pending`, generated collection notes | written; 20 tests pass against fakes; **not yet run for real** |
| 3 | Paper cards (schema, validator), collection overview notes | to do |
| 4 | Zotero writes: add by DOI, file into subcollections, optional markdown attachment | `attach CITEKEY PDF` done 2026-11-19 (2 tests; first real run on Morath, verified); rest to do |
| 5 | Citation check for LaTeX drafts, reference resolution through OpenAlex, watch list | sample watch prompt is in the README; rest to do |
| 6 | Pull request upstream: `zotero` skill, README/installer changes, arXiv-source option | to do |
| 7 | Research to Zotero: shortlist from OpenAlex, open-access PDFs through the web API, paywalled papers through the owner's browser and the Zotero Connector | designed (below); browser loop trialled on 3 IEEE papers 2026-11-19 |
| 8 | Backup: restic snapshots of Zotero, the vault and the zotero-robot config; this machine first, then external SSD, later a NAS | designed (below) |
| 9 | Windows: Phase 0 on the owner's Windows machine, PowerShell installer, Claude in Chrome trial | steps 1-6 done 2026-11-19 (`install.ps1`, four Windows fixes, 23 tests pass, keys, proxies); step 7 Chrome trial done 2026-11-19, 3/3 papers saved with verified PDFs (results below) |

## Phase 0: machine setup

Run these with the owner present. Never take a key in chat; the owner runs every `setup`.

```bash
./install.sh ~/.claude/skills                 # or the harness's skills directory
python3 skills/zotero/zot.py config init      # then edit: [scope] collections, [vault] path
python3 skills/zotero/zot.py doctor
python3 skills/zotero/zot.py collections
python3 skills/zotero/zot.py dry-run
uv run skills/pdf-convert/pdfc.py setup       # owner's terminal; 30-day spend cap on the key
uv run skills/pdf-convert/pdfc.py doctor
```

In Zotero: enable Settings → Advanced → "Allow other applications on this computer to
communicate with Zotero"; create the tag `no-upload` and give it a colour; tag anything that
must not leave the machine. An OpenAlex key is optional (`~/.config/openalex/api_key`).

Owner's values for this machine (Pop!_OS workstation): scope `PhD/AFRL` (46 top-level
items, 8 subcollections; there is no collection named `AFRL Research`), vault `~/Documents/_Vault101`, key already at
`~/.config/zotero/api_key`.

## Verify against the real Zotero before building further

Phases 1 and 2 were written from the documentation and tested only against fake servers. Check, and
fix `zot.py` plus its tests where reality differs:

1. `collections/<key>/items/top` returns the top-level items of a collection, each with
   `data.collections`, `data.tags`, `data.citationKey` and `meta.numChildren`.
2. `items/<attachmentKey>/file/view/url` returns a plain-text `file://` URL, and a 404 (not an
   error page with status 200) when the file is not on disk.
3. `items/top` returns the whole library unpaged (the docs say local results are not paged).
4. Citation keys are present in `data.citationKey` for this library. If many items are
   `no-key`, the Better BibTeX migration to the native field has not completed.
5. Linked-file attachments (`linkMode` `linked_file`) resolve to a path.
6. Group libraries: untested. `library = "groups/<id>"` should work for reads.

Results, 2026-11-18, Zotero 10.0.7, scope `PhD/AFRL` (46 items: 38 ready, 5 held, 3 no-pdf):

1. Matches. Every item carries all four fields.
2. Matches: `text/plain` `file://` URL (percent-encoded, non-ASCII survives), 404 for an
   unknown item. A real attachment with its file missing did not occur in scope; `file_path`
   treats any HTTP error and a URL to a missing file alike, as `no-pdf`.
3. Matches: 385 top items in one response with no `limit`. With `limit`, the server pages
   with `rel="next"`, which `Local.get` follows.
4. Matches: all 46 have native `citationKey`, none in Extra. An item with no creators and no
   date gets a fallback key like `zotero-item-1769`; fix the item in Zotero.
5. Not testable here: no `linked_file` attachments anywhere in the library.
6. Reads work for both group libraries. Fixed: `zotero://select` links now use
   `groups/<id>` instead of `library` for group items.

Also fixed: `collections` died before printing the tree when a scope name did not resolve,
and `doctor` reported such a scope as ok. Both now report it with close matches.

## Phase 2 as built: `zot.py index [COLLECTION...]`

- Estimates every `ready`/`stale` item with `pdfc.py convert --dry-run` (sends nothing), prints a
  table and a total, then asks on a terminal. Without a terminal it exits 4 unless `--yes` is
  given. Agents show the list to the user and rerun with `--yes` only after a yes.
- Converts one item at a time with `pdfc.py convert PDF -o <vault>/_zotero-robot/fulltext/<key>
  --name <key> --preset P`, run as a separate program (`uv run` if available). pdfc's exit codes:
  0 finalize; 3 record in `pending.json` and continue (status `pending`, never resubmitted,
  `fetch-pending` collects it); 4 stop the run; other stop the run.
- Finalize: YAML frontmatter on `<key>.md` (citekey, title, year, `zotero://` link, collections,
  tag `zotero-robot/fulltext`), `meta.json` (item key, PDF sha256, preset, pages, cost),
  one line in `manifest.jsonl`.
- Rewrites `collections/<path>.md`: one table per collection in scope with links to the full
  text and cards. Excluded items are listed by citekey and title but never linked.

First real run, with the owner: `zot.py index --keys <one citekey>` on a single open-access
paper, check the markdown and equations in Obsidian, then `--limit 3`, then the rest.

## Phase 7 design: research to Zotero

Findings, 2026-11-18:

- The web API can create items and upload files we already have. It cannot fetch a PDF.
- Connector endpoint `saveAttachmentFromResolver` fetches open-access copies only (methods
  `oa`, `custom`), never the publisher page.
- `Zotero.Attachments.addAvailableFile` (Find Full Text) does try the DOI page, but the desktop
  app does not share the browser's logins. IEEE and AIP use institutional sign-in (SAML) in the
  browser; the UNM EZproxy (`https://%h.libproxy.unm.edu/%p`, login
  `https://login.libproxy.unm.edu/login?url=%u`) did not work for IEEE. So a plugin calling
  Find Full Text would fail for the publishers that matter. Not built.
- OpenAlex finds a legal open-access copy for 24 of the 39 DOIs in scope (62%), mostly AIP
  author-choice open access, arXiv and OSTI. 10 of the 15 closed ones are IEEE.
- Not used, ever: Sci-Hub, LibGen and plugins built on them (zotero-scipdf, zotero-nexus).
  Also not used: generic "run any JavaScript in Zotero" bridges (cli-anything-zotero).

Writes: use `alexxpzhu/save-papers-to-zotero` (MIT, https://github.com/alexxpzhu/save-papers-to-zotero,
reviewed candidate commit `5fe2e34`, 2026-08-29) rather than building our own write path. It
writes through the local Connector server, so no web API key and no plugin: `saveItems`
(item from translator-style metadata), `updateSession` (files it into a named collection;
the collection must already exist), `saveAttachment` (streams a local or public PDF, then
checks size and SHA-256). Its `zotero_ingest.py` turns DOIs/arXiv ids/.bib into a manifest via
Crossref and arXiv; batches resume from a ledger. Its rules match ours: browser PDFs only
from the user's signed-in session via Claude in Chrome, downloaded to a temp file and passed
with `--pdf-file`; no cookies, no paywall or CAPTCHA bypass, no mirrors. Windows-aware.

Before use:
1. Read all of it (~3,700 lines; it writes to the library). Pin to the reviewed commit.
2. Install as its own skill, not vendored. The zotero skill orchestrates around it.
3. Always pass `reading_status: none` (default adds `#status/to-read` for a plugin not in use).
4. It creates an item even when a duplicate exists (reports only), so drop DOIs already in
   the library first (step 2 below).
5. Trial: one arXiv paper into a throwaway collection the owner creates (e.g.
   `PhD/AFRL/_inbox`). Watch for the Connector hang seen once on `/connector/proxies`.

Flow (nothing is added to Zotero before the owner approves the shortlist):

1. Search with the openalex skill, read abstracts, give a shortlist. The owner picks.
2. Drop DOIs already in the library (match on DOI, all libraries in config).
3. Open access: build a save-papers-to-zotero manifest (its ingest step) for the inbox
   collection the owner names (e.g. `PhD/AFRL/_inbox`), with the OpenAlex
   `best_oa_location.pdf_url` as the PDF source, and import it. Only repository copies (arXiv, OSTI, NASA NTRS,
   PubMed Central) download from a script; Wiley and AIP answer a script with a Cloudflare
   challenge even for open-access PDFs (checked 2026-11-18), so publisher-hosted open access
   goes through step 4 with the paywalled papers.
4. Paywalled: before starting, print the list, the count, and a time estimate (about one
   paper a minute). Then, in the owner's own Chrome (Claude in Chrome), open each DOI page in
   the signed-in session, download the visible PDF to a temp file and import it with save-papers-to-zotero
   (`--pdf-file`) into the inbox, and print progress
   (`[3/12] saved smith2021, 9 min left`). Stop and hand over on a login page, CAPTCHA, or
   access-denied page; never retry past one. Daily cap in config. Without the browser
   extension, print the links for the owner to save.
5. `zot.py status` reports what arrived and what is still missing; then `index` as usual.

Sign-in, for step 4:

- Credentials never go to the agent: no password manager access, no typing passwords. UNM
  sign-in needs the owner's second factor anyway.
- The pre-run list groups papers by publisher and flags which need an institutional sign-in
  in that browser, so the owner signs in to each once before the loop starts. Known so far:
  IEEE Xplore (Institutional Sign In > University of New Mexico) always needs it. Add
  publishers to this list as runs find them (AIP, APS, Springer, Elsevier: to confirm).
- Sessions last hours to days. If one expires mid-run, stop at the sign-in page with
  progress saved, ask the owner to sign in, then resume from the next unsaved paper.

Keep the browser step cheap in tokens (Pro plan): read pages as text, not screenshots; save
with the the browser's own PDF download rather than clicking through; confirm each save
with `zot.py` against the local API rather than by looking. Aim for 2-3 short tool calls per
paper. The mechanical loop may run on a smaller model as a subagent. Measure with a
3-paper trial (usage before and after) before promising a few dozen per session.

A focused search means a few dozen papers per session, paced. No crawling, no scheduled
bulk pulls through the institution's access.

## Phase 8 design: backup

- Sizes on the workstation: `~/Zotero/storage` 1.4 GB, `zotero.sqlite` 8 MB, vault 2.9 GB.
  Zotero file sync uses Zotero storage; the free plan is 300 MB, so check the plan: PDFs past
  the quota live only on this machine.
- restic (encrypted, deduplicated). Repository password in a mode-600 file the owner creates
  in their own terminal. Destinations: a local repository first, then the external SSD, later
  a NAS, all as repositories of the same backup set.
- Contents: `~/Zotero` (database, storage, Better BibTeX), the vault, `~/.config/zotero-robot`,
  the zotero-robot state directory. Zotero locks its database while running: back up
  `zotero.sqlite.bak` or snapshot while Zotero is closed, and say which.
- `backup` command: snapshot, `restic check` on a sample, retention policy, a one-line report.
  Scheduled daily once it has run cleanly by hand.

## Phase 9: Windows (next session, on the owner's Windows machine)

The Linux session's conversation does not carry over; this file is the handoff. Start a
new agent session in the repo on Windows and say "read docs/PLAN.md, do Phase 9".

1. `git clone` the fork, `git checkout zotero-skill`. Python 3.11+ (`py -3 --version`) and uv.
2. Install the skills. `install.sh` is bash: run it from Git Bash, or write `install.ps1`
   (same checks, `New-Item -ItemType SymbolicLink`, which needs Developer Mode, else copy).
   Writing `install.ps1` is part of this phase.
3. Zotero desktop: sign in to the same account and let it sync. Settings > Advanced: "Allow
   other applications on this computer to communicate with Zotero". Install Better BibTeX.
4. `zot.py config init`, then set `[scope] collections = ["PhD/AFRL"]` and the Windows vault
   path. Keys, each in the owner's own terminal, never in chat: `zot.py setup`,
   `uv run skills/pdf-convert/pdfc.py setup`. Then `zot.py doctor`, `pdfc.py doctor`.
5. Run the test suite on Windows: `py -3 -m unittest discover -s skills/zotero -p "test_*.py"`.
   Fix what breaks (paths, file URLs, permissions checks).
6. Zotero Connector in Chrome, Proxies tab. Proxy settings do not sync between machines;
   enter by hand (one minute):
   - Login URL Scheme: `https://login.libproxy.unm.edu/login?url=%u`
   - Proxied URL Scheme: `https://%h.libproxy.unm.edu/%p`
   - Hostnames: as needed. IEEE uses institutional sign-in in the browser instead; sign in
     there once ("Institutional Sign In" > University of New Mexico).
7. Claude in Chrome: install the extension, connect it to Claude Code, then the Phase 7
   3-paper trial: Morath (`10.1109/TNS.2016.2634500`) plus two other closed IEEE papers from
   the list. Record tool calls and usage per paper here.

Results, 2026-11-19, Windows 11, Windows PowerShell 5.1, Python 3.13 (`py -3`), Zotero 10.0.8:

- Steps 1-3 were already done on this machine (repo, Python, uv, Zotero signed in with local
  API on, Better BibTeX with the same citekey format).
- `install.ps1` written: same options as `install.sh`, same checks with Windows key locations.
  `New-Item -ItemType SymbolicLink` in 5.1 needs admin even with Developer Mode, so it uses
  `mklink /D`, and falls back to a junction (no privilege needed). Developer Mode is off here,
  so the five skills are junctions in `~\.claude\skills`. Links are removed with a
  non-recursive `Directory.Delete`, never `Remove-Item`, which can follow a directory link.
- Config at `%APPDATA%\zotero-robot\config.toml`: scope `PhD/AFRL`, vault `~/Documents/_Vault101`.
  Keys stored by the owner (`zot.py setup`; Datalab key in the Windows keyring). `doctor` and
  `pdfc.py doctor` pass. Connector proxies entered and Claude in Chrome installed by the owner. Dry run: 44 items,
  36 ready, 5 held, 3 no-pdf (Linux had 46 and 38 ready; the two missing items were not found).
- Fixed for Windows in `zot.py`: (1) `local_api` default is now `127.0.0.1`: Zotero listens on
  IPv4 only and Windows `localhost` waits 2 s for IPv6 on each request (a dry run took more
  than 2 min, now 3 s). (2) A string `pdfc_command` was split with POSIX `shlex`, which drops
  backslashes from Windows paths. (3) `index` crashed on EOF where stdin is `NUL`, which
  Windows reports as a terminal; now exits 4 like any other non-terminal. (4) Piped output was
  cp1252 and died on non-ASCII titles; stdout and stderr are UTF-8 on Windows.
- `python3` here is the Microsoft Store stub. SKILL.md and README now say to use `py -3` or `uv run`.
- Tests: 23 pass on Windows (the failures were all the `pdfc_command` quoting).

save-papers-to-zotero review, 2026-11-19, commit `5fe2e34` (no later commits upstream): all
four scripts read in full (connector import, batch import, ingest, summary note) plus SKILL.md
and the manifest reference; README, other references and tests skimmed or not read. Network:
only `127.0.0.1:23119`, the given PDF URL, Crossref and arXiv. No subprocess, eval or cookie
access. Writes: `saveItems`, `updateSession`, `saveAttachment` only; never edits or deletes.
Collection is matched by name in `users/0` only and refused if not unique. Minor bugs, not
safety issues: ingest's retry on a dropped connection never fires (checks `connection_error`,
gets `zotero_connection_error`); verification unquotes the file URL twice, so a stored name
with a literal `%` would report `verification_failed`.

It cannot attach a PDF to an item already in the library, and Morath (`ZSRN5L64`) is already
there with only an HTML snapshot. The two "IEEE Xplore Full-Text PDF:" items hold IEEE's
`stamp.html` viewer frame, not a PDF. So, decided with the owner: new `zot.py attach CITEKEY
PDF` (web API upload to the existing item, then waits for desktop sync and checks SHA-256;
refuses excluded items, non-PDFs, items with a PDF; leaves and names an empty attachment if a
later step fails). save-papers-to-zotero stays for papers not yet in the library.

Trial, revised: Morath through `attach`; two new closed IEEE papers, chosen by the owner from
an OpenAlex shortlist, through save-papers-to-zotero into `PhD/AFRL/_inbox`.

State at handoff (2026-11-19): save-papers-to-zotero installed as its own skill, clone at
`~/Desktop/save-papers-to-zotero` pinned to `5fe2e34`, junction in `~\.claude\skills`.
`PhD/AFRL/_inbox` exists (`SKQTGZQB`, empty). Owner's topic for the two new papers: DFT and
ML-potential modelling of silicon oxide defects (bulk, Si/SiO2 interface, oxide); seed paper
Erhard et al. 2024, "Modelling atomic and nanoscale structure in the silicon–oxygen system
through active machine learning" (OpenAlex `W4392348184`). No OpenAlex key on this machine
(1,000 credits/day).

Step 7 trial, 2026-11-19 (Chrome 1 connected, IEEE Xplore through UNM institutional sign-in):

| paper | route | result | browser calls |
|---|---|---|---|
| `morathMoreAccurateQuantum2017` (`ZSRN5L64`) | `zot.py attach` | PDF `4GQT55VZ`, synced, SHA-256 verified (1.14 MB) | 5 (3 lost to a signed-out session, see below; 2 once signed in) |
| `wilhelmerStatisticalInitioAnalysis2021` (`AK3U88VX`), `10.1109/essderc53440.2021.9631833` | save-papers-to-zotero into `_inbox` | `saved_with_pdf`, 0.68 MB, no possible duplicates | 2, then 2 for the retry of both papers |
| `freireAtomisticSimulationDefect2024` (`3D967B9F`), `10.1109/iirw62856.2024.10947139` | save-papers-to-zotero into `_inbox` | `saved_with_pdf`, 11.0 MB, no possible duplicates | 2 (download blocked, retried with the paper above) |

11 browser calls in all; the clean path is 2 per paper. Shell calls: one move-and-check of the
downloads, then `attach` (Morath) or `zotero_ingest.py` plus one `zotero_batch_import.py` for
both new papers (`--reading-status none`, balanced safety, 2.5 s and 3.8 s per paper).
Usage: the whole session (reading the plan, the OpenAlex shortlist, three downloads, imports,
this write-up) left the owner's plan at 20% of the day's allowance. The browser loop is a small
part of that, so a few dozen papers per session is realistic. Shortlist: 62 OpenAlex credits.
Afterwards Wilhelmer's title was fixed through the web API (PATCH with
`If-Unmodified-Since-Version`; synced back to desktop within seconds).

Vault folder on this machine: `pipeline_dir = "_Zotero_Robot"` (owner's choice, 2026-11-19;
nothing had been written under the default yet).

How the browser step works (no screenshots, no clicking through viewers): open
`https://doi.org/<DOI>` (IEEE redirects to `/document/<arnumber>`), then one `javascript_tool`
call in that page that waits for the header, checks for "Access provided by: UNIVERSITY OF NEW
MEXICO", fetches `/stampPDF/getPDF.jsp?tp=&arnumber=<n>&ref=` with the page's own session,
checks the first bytes are `%PDF`, and saves it as `zr_<arnumber>.pdf` through a blob link (the
browser's own download, into `~/Downloads`). The script then moves it to a temp folder and checks
header and `%%EOF` before any Zotero write. Several arnumbers on the same site can be fetched
in one call from one page.

Learned:

- Zotero desktop must be running, not just the web library: `doctor`, the DOI check and both
  write paths use the local server.
- The session was signed out at first although the owner had signed in earlier; the page showed
  "Institutional Sign In" and no "Access provided by". Detect that from page text before
  fetching. Owner's rule from now on: the agent may click Institutional Sign In > University of
  New Mexico and follow the redirects, and hands over only at a password or second-factor page.
- Chrome blocks automatic downloads after the first one from a site ("trying to download
  multiple files"); the fetch succeeds but no file lands. The owner allowed
  `ieeexplore.ieee.org` under `chrome://settings/content/automaticDownloads` (a browser setting,
  so the owner's to change). Check that each file landed on disk straight after the download
  call, not at the end of the loop.
- Crossref metadata for IEEE conference papers has no abstract, keeps JATS markup in titles
  (Wilhelmer's title is stored as `Si/SiO <sub>2</sub> Network` with runs of spaces), and splits
  compound surnames (Cobos Freire became `Freire`, hence the citekey). Clean titles in
  `zotero_ingest.py` output before import, or fix by hand afterwards; OpenAlex has the abstracts.
- `oa.py` dies on non-ASCII output in a Windows console unless `PYTHONUTF8=1` (the same fix
  `zot.py` got). Without a key, OpenAlex refuses cursor paging past the first 100 results, so
  `cites --all` fails; use `--limit 100`. Semantic search does not accept a publisher filter;
  the keyword filter is `primary_location.source.host_organization_lineage:P4310319808` (IEEE).
- IEEE's closed-access papers on this topic are DFT work; the ML-potential literature
  (seed's snowball) is almost all non-IEEE. Owner's fallback topic if a shortlist is thin:
  defects in IR detectors.

Open items from the Linux session: first single-paper conversion not yet run
(`dengDeepLevelTransient2024` suggested); duplicates to clean up in Zotero (`ochoa...2022a`,
`dsouza...2016a`, the two "IEEE Xplore Full-Text PDF:" items); owner to check the Zotero
storage plan against 1.4 GB of PDFs; restic not yet installed on Linux.

## Phase 3 design: paper cards

`_zotero-robot/cards/<citekey>.md`, YAML properties then a short body. Properties: `citekey`,
`zotero_key`, `title`, `year`, `collections`, `material_system`, `method`, `quantities` (list
of name, value, unit, page). Every number or claim carries a page reference into the
converted markdown (its page separators are `{N}------`, 0-based). Cards are written by an
agent session one collection at a time; a validator script checks the properties and that
each page reference exists. Numbers read from charts by the converter are not data: check the
figure image before putting one on a card.

## Conventions for whoever continues

- Standard library only where possible; declare extras inline (PEP 723) as upstream does.
- Tests live next to the script: `python3 -m unittest discover -s skills/zotero -p 'test_*.py'`.
- The pipeline writes only under `_zotero-robot/`. Project notes are written only on request.
- Commit on a branch; the owner decides what goes upstream.
