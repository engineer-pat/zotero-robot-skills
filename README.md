# assistant_skills

Research skills for AI coding agents, in the open
[Agent Skills](https://agentskills.io) format: one folder per skill, each with a `SKILL.md`
(YAML frontmatter `name` + `description`, then instructions) plus any scripts it uses.
Nothing here is tied to one vendor. Any harness that loads skill folders can use them
directly, and the rest can be pointed at them (see below).

| skill | what it does | needs |
|---|---|---|
| [openalex](skills/openalex/SKILL.md) | Literature search, citation snowballing, author disambiguation and citation-graph mapping through the [OpenAlex](https://openalex.org) API, with a bundled CLI (`oa.py`) and DuckDB views | Python ≥ 3.10; [uv](https://docs.astral.sh/uv/) for the `sql` command; optional free API key |
| [papis](skills/papis/SKILL.md) | Drive a local [papis](https://github.com/papis/papis) bibliography: search, add by DOI/arXiv/ISBN, export and audit BibTeX, while avoiding papis's silent non-TTY failure modes | papis (written against 0.15) with at least one library configured |
| [pdf-convert](skills/pdf-convert/SKILL.md) | Convert PDFs (and Word, PowerPoint, images) to markdown, LaTeX, HTML or JSON with figures through the [Datalab](https://www.datalab.to) marker API, at a chosen level of detail, with a bundled CLI (`pdfc.py`) that guards spend: page limit, 30-day budget, cache, no double-billed retries | Python ≥ 3.10; a Datalab API key; [uv](https://docs.astral.sh/uv/) recommended; pandoc for LaTeX |
| [zotero](skills/zotero/SKILL.md) | Work with a [Zotero](https://www.zotero.org) library by collection through the desktop app's local API, with a bundled CLI (`zot.py`): collection tree, per-item status, citation key to PDF path, and a dry run before any batch, with exclude tags and holds so unpublished documents are never sent anywhere | Python ≥ 3.11 (or [uv](https://docs.astral.sh/uv/) on 3.10); Zotero 7+ running with the local API enabled; citation keys (Zotero 8+, or Better BibTeX) |
| [figure-craft](skills/figure-craft/SKILL.md) | Design and critique research plots and schematics, from quick feedback to whole figure sets; align figures with text, compare visual alternatives, and retain decisions across sessions | none for guidance; production tools depend on the project |

openalex finds papers; papis keeps the ones you want and produces the `.bib`; pdf-convert
turns their PDFs into text an agent can read; figure-craft helps communicate research
through figures. zotero does papis's job for people whose library lives in Zotero: it decides
which papers may be processed and hands their PDFs to pdf-convert. Each skill also works
independently.

## Install

```bash
git clone <this repo> ~/repos/assistant_skills
cd ~/repos/assistant_skills
./install.sh                  # symlinks each skill into ~/.agents/skills, then checks prerequisites
```

`~/.agents/skills` is read by several harnesses. If yours reads a different directory, pass
that directory instead, or pass several:

```bash
./install.sh ~/.claude/skills                 # e.g. Claude Code
./install.sh ~/.agents/skills ~/.claude/skills
./install.sh --skill openalex DIR             # only some skills
./install.sh --copy DIR                       # copies, for harnesses that don't follow symlinks
./install.sh --check                          # just re-run the prerequisite check
./install.sh --uninstall DIR                  # remove the links this script made
```

The script never overwrites anything it didn't create. If a skill of the same name is already
in the target, it is skipped. `--force` moves it to `~/.local/state/assistant_skills/backups/`
first.

**Harness without skill support?** Run `./install.sh --index` and paste the output into the
agent's instruction file (`AGENTS.md`, a system prompt, or similar). It lists each skill's
trigger description and the path of its `SKILL.md`, which is all an agent needs to load a skill
on demand.

### Prerequisites

- **openalex:** `python3` ≥ 3.10. The CLI uses only the standard library, except `oa.py sql`,
  which needs duckdb. `uv run` installs duckdb automatically from the script's inline metadata.
  An API key raises the daily budget from 1,000 to 10,000 credits. Create a free account at
  openalex.org, then store the key where `oa.py` looks for it:

  ```bash
  mkdir -p ~/.config/openalex && (umask 077; printf %s 'YOUR_KEY' > ~/.config/openalex/api_key)
  # or: export OPENALEX_API_KEY=...
  ```

- **papis:** `uv tool install papis` (or `pipx install papis`), then configure a library in
  `~/.config/papis/config`. See the [papis docs](https://papis.readthedocs.io). Check it with
  `papis list --libraries`.

- **pdf-convert:** `python3` ≥ 3.10 and a Datalab API key. Running through `uv run` adds
  pypdf (page counts before upload) and keyring (stores the key in the OS credential store);
  without them the CLI still works. Create a key just for agents at
  [datalab.to/app/keys](https://www.datalab.to/app/keys) and **give it a 30-day spend cap**:
  that cap is the only hard limit on what an agent can spend. Then store it, in your own
  terminal so the key never passes through an agent conversation:

  ```bash
  uv run skills/pdf-convert/pdfc.py setup     # asks for the key (hidden), verifies, stores it
  uv run skills/pdf-convert/pdfc.py doctor    # checks key, pandoc, budget
  ```

  [pandoc](https://pandoc.org/installing.html) is needed only for LaTeX output.

- **zotero:** the Zotero desktop app, running, with Settings → Advanced → "Allow other
  applications on this computer to communicate with Zotero" turned on. Then create the
  per-machine config and edit it (which collections are in scope, where your notes vault is):

  ```bash
  python3 skills/zotero/zot.py config init    # writes ~/.config/zotero-robot/config.toml
  python3 skills/zotero/zot.py doctor
  ```

  A web API key is only needed for write commands. Store it in your own terminal with
  `python3 skills/zotero/zot.py setup`.

## Updating

With the default symlinks, `git pull` updates every harness at once. Copies (`--copy`) need
`./install.sh --copy --force DIR` again.

## Per-machine config

Skills stay general; anything that differs between machines goes in a config file on that
machine. For the zotero skill that is one TOML file
([config.example.toml](skills/zotero/config.example.toml) documents every setting):

| | location |
|---|---|
| Linux, macOS | `~/.config/zotero-robot/config.toml` |
| Windows | `%APPDATA%\zotero-robot\config.toml` |
| anywhere | the path in `$ZOTERO_ROBOT_CONFIG` |

To adapt this to your own setup, change three things: `[scope] collections` (which Zotero
collections an agent may touch), `[vault] path` (your notes folder; the same vault usually has
a different path on each machine) and, if you want, the exclude tag. Secrets are never in this
file and are never synced: each machine stores its own keys with the skills' `setup` commands.
An agent starting on a machine reads the config through `zot.py config show` and `zot.py doctor`.

## Creating scheduled tasks leveraging this skillset

The skills are command-line tools, so anything that can run an agent on a timer can use them.
Two things decide where a scheduled task can run:

- **It must run on the machine that has Zotero and the keys.** The local Zotero API and the
  stored keys exist only there. A cloud-hosted schedule cannot reach them.
- **It should be read-only unless you are there to approve.** Searching, listing and writing a
  digest are safe unattended. Converting PDFs costs money and uploads files, so leave that for
  a session you are watching.

Ways to schedule it: your agent app's own scheduled tasks, if they can be set to run on your
computer; or a system timer (systemd, cron, Windows Task Scheduler) that starts your agent's
non-interactive mode with the prompt below. Check your harness's documentation for how to grant
an unattended run permission to execute these scripts and write to one folder.

A sample prompt for a weekly literature watch. Put the works you want watched, one OpenAlex id
or DOI per line, in `_zotero-robot/watch.txt` in your vault first.

```text
Weekly literature watch. Work unattended; do not ask questions.

1. Run `zot.py doctor`. If it fails, write the failure to today's digest and stop.
2. Read the watch list at <vault>/_zotero-robot/watch.txt (one OpenAlex id or DOI per line).
3. For each entry, use the openalex skill to list the works citing it, newest first
   (`oa.py cites ID --sort publication_date:desc --limit 25`). Keep only works published
   since the date of the newest file in <vault>/_zotero-robot/digests/ (or the last 7 days
   if there is none).
4. Drop anything already in my library: check DOIs against `zot.py status --json`.
5. For what remains, read each abstract with `oa.py work ID` and write
   <vault>/_zotero-robot/digests/<today>.md: for each paper its title, first author, venue,
   date, DOI, which watched work it cites, and one sentence on why it may matter to the
   collections in scope. Group by watched work. Say plainly if there was nothing new.
6. Do not add anything to Zotero, do not convert or download any PDF, and do not write
   anywhere except that digest file. End by reporting the OpenAlex credits used.
```

Other tasks that fit the same pattern: a weekly `zot.py status` report of new items waiting to
be converted, or a monthly check of the library's DOIs against OpenAlex for retractions.

## Layout and conventions

```
skills/<name>/SKILL.md      required: frontmatter (name, description) + instructions
skills/<name>/*.py, *.sql   scripts the skill runs, addressed relative to the skill folder
skills/<name>/references/   longer material the skill loads only when needed
install.sh                  installer, prerequisite check, index generator
AGENTS.md                   instructions for an agent working in this repo
docs/PLAN.md                this fork's implementation plan and handoff notes
```

Skills stay general. Facts about one machine or project, such as which papis library a
manuscript uses or where its `.bib` lives, belong in that project's own agent notes, not in
the skill.
