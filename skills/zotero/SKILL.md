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
| `items COLLECTION` | Same classification for one collection and its subcollections. |
| `resolve CITEKEY` | JSON: item key, title, PDF path on disk, output folder, `zotero://` link. Excluded items return no path. |
| `record CITEKEY` | After converting: writes `meta.json` so `status` reports `done`, or `stale` if the PDF later changes. |
| `approve KEY...` / `unapprove KEY...` | Release or re-hold items by Zotero item key. |
| `config [show\|init\|path]` | Show, create or locate the per-machine config. |
| `setup` | Stores the web API key (hidden input). For the user's own terminal. |

Statuses: `ready`, `done`, `stale`, `held`, `excluded`, `no-pdf` (no PDF attachment, or the
file is not downloaded on this machine), `no-key` (no citation key).

A collection can be named by key, by name, or by path (`AFRL Research/DLTS`). A name that
matches two collections is refused; use the path or key.

## Reading one paper from the library

```bash
zot.py resolve smith2021                 # -> "pdf": "/…/Smith 2021.pdf", "outdir": "<vault>/_zotero-robot/fulltext/smith2021"
```

1. If `excluded` is not empty, stop (rule 2).
2. If `<outdir>/<citekey>.md` exists and `status` says `done`, read that. List the headings
   first (`grep -n '^#' file.md`) and read the sections you need.
3. Otherwise convert with the pdf-convert skill, into the output folder, then record it:

   ```bash
   pdfc.py convert "<pdf>" -o "<outdir>" --dry-run     # cost first
   pdfc.py convert "<pdf>" -o "<outdir>"
   zot.py record smith2021 --preset standard
   ```

   pdf-convert names its output after the PDF file. Rename the markdown to `<citekey>.md` so
   notes can link to it by citation key.

## Converting a collection

1. `zot.py dry-run "Collection Name"` and show the user the whole output.
2. Resolve held items with the user; `approve` the ones they clear.
3. After the user says yes, convert the `ready` and `stale` items one at a time as above.
   pdf-convert's page limit and 30-day budget still apply; if one fires, stop and ask.
4. Finish with `zot.py status` and report counts and what was spent (`pdfc.py usage`).

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

## Not here yet

Write commands (add by DOI, file into subcollections, attach the markdown to the item) are
planned and will use the web API key. Until then, tell the user what to add or move and let
them do it in Zotero.
