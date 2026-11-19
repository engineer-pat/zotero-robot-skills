---
name: zotero
description: Work with the user's Zotero library by collection through the desktop app's local API, with a bundled CLI (zot.py). Use whenever the user mentions Zotero, their reference library or a collection in it, a citation key, or wants a paper from their library found, read, converted, summarized or indexed. It lists collections, classifies each item (ready, done, held, excluded, no PDF), turns a citation key into the PDF on disk, and shows a dry run before any batch. Use it before pdf-convert for any paper that lives in Zotero, so exclusions and holds are respected. Hand DOIs found with the openalex skill to the user for adding.
---

# Zotero, scoped by collection

`zot.py` (next to this file) reads the Zotero desktop app's local API. Reads need no key and
never leave the machine. Zotero must be running. Below, `<skill-dir>` is the directory holding
this SKILL.md.

```bash
python3 <skill-dir>/zot.py <command> ...   # Python 3.11+
uv run <skill-dir>/zot.py <command> ...    # Python 3.10: uv adds the TOML reader
```

On Windows, `python3` is often a Microsoft Store stub that prints "Python was not found".
Use `py -3 <skill-dir>/zot.py` or `uv run` there.

Run `doctor` first in a new session or on a new machine. It checks the config, that Zotero
answers, that the collections in scope resolve, and the web API key, without printing the key.

## The rules

These protect unpublished and restricted documents. They are not optional.

1. **Stay in scope.** Only collections listed under `[scope] collections` in the config, and
   their subcollections, are processed. Do not widen the scope yourself; ask the user.
2. **Excluded means untouched.** An item with an exclude tag (default `no-upload`), or in a
   collection named `exclude`, is never converted and never sent to any service. Do not open or
   read its PDF either, unless the user names that item in this conversation and asks you to.
   Reading a file in an agent session sends its text to the model provider.
3. **Held items wait for the user.** Items with no DOI or arXiv id, and reports, manuscripts,
   theses and documents, are `held` because they look unpublished. Show the user the list and
   let them decide. Run `approve` only for item keys the user approved.
4. **Dry run before any batch.** Show the `dry-run` output and get a yes before converting more
   than one item.
5. **Never ask for the API key in chat.** The user runs `zot.py setup` in their own terminal.

## Commands

| command | does |
|---|---|
| `doctor` | Checks config, Zotero, scope, key, sibling skills. Exit 1 if something is missing. |
| `collections` | The collection tree with keys, item counts, and which parts are in scope or excluded. |
| `status [COLLECTION...]` | Classifies every item in scope (or in the named collections). `--json` for programs. |
| `dry-run [COLLECTION...]` | What a conversion run would send, and what it would skip and why. Sends nothing. |
| `index [COLLECTION...]` | Estimates each `ready`/`stale` item with pdf-convert's dry run, asks, then converts them one by one into the vault, adds frontmatter, writes `meta.json` and a manifest line, and refreshes the collection notes. `--keys`, `--limit`, `--preset`, `--yes`. |
| `fetch-pending` | Collects conversions that outlived pdf-convert's wait. Never re-submits them. |
| `notes` | Rewrites the generated collection notes in the vault. |
| `items COLLECTION` | Same classification for one collection and its subcollections. |
| `resolve CITEKEY` | JSON: item key, title, PDF path on disk, output folder, `zotero://` link. Excluded items return no path. |
| `record CITEKEY` | After converting one paper by hand: frontmatter, `meta.json`, manifest line, so `status` reports `done` (or `stale` if the PDF later changes). |
| `attach CITEKEY PDF` | Uploads a PDF the user obtained to that existing item as a stored attachment (web API, counts against the user's Zotero storage), then waits for Zotero desktop to sync it and checks its SHA-256. Refuses excluded items, non-PDF files, and items that already have a PDF (`--force`). Exit 3: uploaded, not synced to this machine yet. |
| `check [ITEM\|COLLECTION...]` | Pulls citation key, title, creators, venue, DOI, abstract length, PDF, collections and tags for the named items (item keys, citation keys or collections; default the scope) and flags what looks wrong: markup or stray whitespace in titles, missing first names, malformed DOIs, missing citation keys, possible duplicates. `!` lines need fixing, `-` lines are notes. Exit 2 if any `!`. `--json`. |
| `set ITEM FIELD VALUE` | Changes one text field (`title`, `abstractNote`, `date`, `DOI`, …) or one creator (`creator:N "Last, First"`) through the web API, refusing if the item changed since it was read, then waits for Zotero desktop to sync it. `-` as VALUE reads stdin. Refuses excluded items. Exit 3: saved, not synced yet. |
| `approve KEY...` / `unapprove KEY...` | Release or re-hold items by Zotero item key. |
| `config [show\|init\|path]` | Show, create or locate the per-machine config. |
| `setup` | Stores the web API key (hidden input). For the user's own terminal. |

Statuses: `ready`, `done`, `stale`, `pending` (submitted, not collected yet), `held`,
`excluded`, `no-pdf` (no PDF attachment, or the file is not downloaded on this machine),
`no-key` (no citation key).

A collection can be named by key, by name, or by path (`AFRL Research/DLTS`). A name that
matches two collections is refused; use the path or key.

## Reading one paper from the library

```bash
zot.py resolve smith2021                 # -> "pdf": "/…/Smith 2021.pdf", "outdir": "<vault>/_zotero-robot/fulltext/smith2021"
```

1. If `excluded` is not empty, stop (rule 2).
2. If `<outdir>/<citekey>.md` exists and `status` says `done`, read that. List the headings
   first (`grep -n '^#' file.md`) and read the sections you need.
3. Otherwise convert just that paper, after telling the user the cost:

   ```bash
   zot.py index --keys smith2021            # shows the estimate; add --yes once the user agrees
   ```

   Files are named after the citation key (`<outdir>/smith2021.md`), so notes can link to them.

## Converting a collection

1. `zot.py dry-run "Collection Name"`, and show the user the whole output.
2. Resolve held items with the user; `approve` only the ones they clear.
3. `zot.py index "Collection Name"` prints a cost estimate per item and a total, then stops
   (in an agent session there is no terminal to answer its prompt). Show the user the list and
   the total. Only after they agree, run the same command with `--yes`.
4. `index` stops on the first failure and when a pdf-convert guard refuses (page limit or
   30-day budget). Report it and ask; do not raise a guard yourself. A rerun skips everything
   already `done`, so it is safe to repeat. An item over the page limit is skipped at the
   estimate; convert it separately with the user's agreement (`pdfc.py convert ... --max-pages N`,
   then `zot.py record`).
5. Exit code 3 means some conversions are still processing at Datalab. Run
   `zot.py fetch-pending` later; never convert those items again.
6. Finish with `zot.py status` and report what was converted and what it cost.

## Where output goes

Everything generated lives in one folder the pipeline owns, inside the user's notes vault
(`[vault] path` + `pipeline_dir`, default `_zotero-robot/`):

```
_zotero-robot/fulltext/<citekey>/<citekey>.md   converted paper, figures beside it
_zotero-robot/fulltext/<citekey>/meta.json      item key, PDF hash, converter, preset, date
_zotero-robot/cards/<citekey>.md                one structured summary per paper
_zotero-robot/collections/<name>.md             generated list of a collection's papers
_zotero-robot/digests/<date>.md                 scheduled watch-list results
```

Files there may be overwritten by a later run. Write the user's own project notes somewhere
else in the vault, and only when asked. In notes, cite as `[@citekey]` and link back to Zotero
with the `select` link from `resolve`.

## Citation keys

Zotero 8 and later store the key in the item's native Citation Key field (Better BibTeX fills
it). On older setups the key is read from a `Citation Key:` line in Extra. An item with
neither is `no-key`: ask the user to generate keys in Zotero rather than inventing one,
because their citation plugin and `.bib` exports use the same key.

## Config

One TOML file per machine, with no secrets in it; see `config.example.toml` next to this file
for every setting, including Windows paths. `zot.py config init` copies it into place and
`zot.py config path` prints where that is. Paths differ between machines, so never copy
another machine's config without editing `[vault] path`.

## Adding a PDF to an item that has none

For a `no-pdf` item, once the user has a legitimately obtained PDF on disk (their download,
or one saved from their own signed-in browser session):

```bash
zot.py attach smith2021 /path/to/download.pdf
```

Check the file first: a publisher's login or viewer page saved as `.pdf` is refused. Do not
use this to add a second copy, or for papers not yet in Zotero (that creates new items, which
is a separate step the user approves). Never delete the temporary download until `attach`
exits 0 or 3. If it fails after creating the attachment, it names the empty attachment for
the user to delete in Zotero; do not retry blindly.

## After any import or attach: check, then fix

Run `zot.py check` on what was just added (the collection it went into, or the citation keys)
and read the whole output yourself. The flags catch the mechanical faults; some faults only a
reader catches, such as a compound surname split into the wrong fields ("Freire, Teofilo Cobos"
for Teofilo Cobos Freire). Fix with `set`, one field at a time, and run `check` again:

```bash
zot.py check "Research/_inbox"
zot.py set ABCD1234 title "… in the Si/SiO2 Network"          # was "Si/SiO <sub>2</sub>", from Crossref
zot.py set smith2021 creator:1 "Cobos Freire, Teofilo"
oa.py work <DOI> --json | <extract .abstract> | zot.py set smith2021 abstractNote -
```

Take values from a source (the publisher page, OpenAlex, Crossref), never from memory. Changing
an author does not change the citation key; tell the user, who decides whether to refresh it in
Better BibTeX.

## Papers from the user's signed-in browser

For a paywalled paper the user has access to, when a browser automation tool is connected to
the user's own browser. Show the list, the count and a time estimate (about a minute a paper)
first. Read pages as text, not screenshots. Per paper:

1. Open `https://doi.org/<DOI>`.
2. In that page, one script: wait for the page, check it shows institutional access (IEEE Xplore:
   "Access provided by: …"; "Institutional Sign In" alone means signed out), fetch the PDF with
   the page's own session (IEEE: `/stampPDF/getPDF.jsp?tp=&arnumber=<n>&ref=`), check the first
   bytes are `%PDF`, and save it through a blob link, which is the browser's own download.
   Several papers on one site can be fetched from one page.
3. Check the file landed in the downloads folder straight away, move it to a temporary
   folder, and check its header again. Then `attach` (item already in Zotero) or the
   save-papers-to-zotero skill with `--pdf-file` (new item), then `check`.

Stop and hand over at a password, second-factor, CAPTCHA or access-denied page; never retry past
one, never type credentials, never read or export cookies. Clicking "Institutional Sign In" and
following the redirects is fine when the user allows it. If the fetch succeeds but no file lands,
the browser is blocking repeated downloads from the site: ask the user to allow automatic
downloads for it (a browser setting, theirs to change).

## Not here yet

Other write commands (add by DOI, file into subcollections, attach the markdown to the item, delete)
are planned and will use the web API key. Until then, tell the user what to add or move and
let them do it in Zotero.
