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
| 0 | Machine setup (below) | to do on the owner's machine |
| 1 | `zotero` skill, read-only: config, doctor, collections, status, dry-run, resolve, record, approve | written; 11 tests pass against a fake local API; **not yet run against a real Zotero** |
| 2 | Batch conversion: `index` (estimate, confirm, convert, frontmatter, `meta.json`, manifest), `fetch-pending`, generated collection notes | written; 20 tests pass against fakes; **not yet run for real** |
| 3 | Paper cards (schema, validator), collection overview notes | to do |
| 4 | Zotero writes: add by DOI, file into subcollections, optional markdown attachment | to do |
| 5 | Citation check for LaTeX drafts, reference resolution through OpenAlex, watch list | sample watch prompt is in the README; rest to do |
| 6 | Pull request upstream: `zotero` skill, README/installer changes, arXiv-source option | to do |

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

Owner's values for this machine (Pop!_OS workstation): scope `AFRL Research` (under 50
papers, has subcollections), vault `~/Documents/_Vault101`, key already at
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
