# auto_scan

auto_scan reviews every automated realtime earthquake simulation run by
[realtime_simulation_workflow](https://github.com/ucgmsim/realtime_simulation_workflow)
for scientific and performance problems. When a check fails, it files a GitHub issue
in the repository at fault. On later events it checks that the fix worked. It never
touches the cluster: everything it reads has already been published off it (the
results on Dropbox, the Cylc monitor's Datasette, SimDB and SeismicNow).

DESIGN.md has the contracts between the modules: the database schema, the cache
layout, the rules for extractors and checks, and the findings lifecycle.

## The loop

A scheduled GitHub Action (`.github/workflows/scan.yml`, every 30 minutes) runs:

1. `auto-scan pull`: fetch `scan.db`, the store of record, from the state store (rclone).
2. `auto-scan collect`: download each new or changed run, run the extractors, and
   record the metrics, station residuals and SW4 timers.
3. `auto-scan check`: run every check. Each failure opens or updates a finding.
4. `auto-scan verify`: follow the issues of filed findings. A fix that holds on later
   events is verified, and a failure after the fix reopens the issue.
5. `auto-scan push`: store `scan.db` again, and upload a copy to the Datasette for the
   dashboard.

When findings are open, a second job writes a brief (`auto-scan brief`) and runs
Claude with the standing instructions in `prompts/triage.md`. Claude confirms each
finding in the source and files one issue in the repository at fault, labelled
`ai-suggested`, `ai-ok` and `scan`. Then it records the link with `auto-scan link`.
Each repository's `ai-issue-solver` from
[meta-ci-action](https://github.com/ucgmsim/meta-ci-action) then attempts the fix.
Claude dismisses a finding that is not a problem, and files one whose cause it could
not establish in realtime_simulation_workflow without `ai-ok`, for a person.

## Running it locally

```sh
uv sync
uv run auto-scan run --no-push          # pull, collect, check, verify; nothing uploaded
uv run auto-scan collect 2026p866048 --force
uv run auto-scan check 2026p866048
uv run auto-scan brief --out brief.md   # what the triage would read
uv run auto-scan push --no-datasette    # store scan.db but don't upload it
```

You need rclone configured with the remotes in `AUTO_SCAN_RESULTS_URL` and
`AUTO_SCAN_STATE_URL`, and the monitor's reader token. `verify` needs `gh`
authenticated (`GH_TOKEN`). `pull` refuses to start from an empty database if rclone
fails. `push` refuses to upload a database with fewer events than it pulled; set
`AUTO_SCAN_ALLOW_SHRINK=1` if you really mean to. Each day's first upload is also
kept as `history/scan-<YYYYMMDD>.db` in the state store, so a bad push can be undone
by copying one back.

Tests are offline: `uv run pytest`.

## Settings, secrets and variables

Every setting is one environment variable, read in `src/auto_scan/config.py`.

| Name | Kind | What it is for |
|---|---|---|
| `RCLONE_CONFIG` | org secret (exists) | An `rclone.conf` with the `dropbox` remote, base64-encoded (`base64 -w0 rclone.conf`). The workflow decodes it to `~/.config/rclone/rclone.conf`. |
| `CLAUDE_CODE_OAUTH_TOKEN` | org secret (exists) | Claude for the triage job, as in meta-ci-action. |
| `SCAN_GH_TOKEN` | repository secret (create) | The `gh` token for `verify` and the triage: it reads, files, comments on and reopens issues in the other ucgmsim repositories and clones them. `github.token` can't, because it only covers this repository. |
| `CYLC_MONITOR_READER_TOKEN` | repository secret (create) | The Cylc monitor's `cylc-reader` token. The scan reads the monitor's `jobs` view with it. |
| `AUTO_SCAN_WRITER_TOKEN` | repository secret (create) | The Datasette's `scan-writer` token, for `push`'s upload (see [Deploying the server side](#deploying-the-server-side)). Without it, `push` stores `scan.db` and skips the upload with a warning. |
| `DATASETTE_URL` | repository variable (create) | The Datasette's base URL, e.g. `https://<your-datasette>`. The workflow passes it as `AUTO_SCAN_DATASETTE_URL`. |
| `AUTO_SCAN_DATASETTE_URL` | env | Where the scan database and dashboard are hosted. It also sets the monitor URL unless `AUTO_SCAN_MONITOR_URL` is set. |
| `AUTO_SCAN_MONITOR_URL`, `AUTO_SCAN_MONITOR_DATABASE` | env | The Cylc monitor's Datasette and database name (`cylc`). |
| `AUTO_SCAN_DATABASE` | env | The scan's database name on the Datasette (`scan`). |
| `AUTO_SCAN_WORKFLOW` | env | The Cylc workflow scanned (`realtime/run1`). |
| `AUTO_SCAN_RESULTS_URL` | env | The rclone path the realtime workflow uploads results to. |
| `AUTO_SCAN_STATE_URL` | env | The rclone directory holding `scan.db` and `history/`. |
| `AUTO_SCAN_SIMDB_API`, `AUTO_SCAN_SEISMICNOW_API` | env | The two public APIs. |
| `AUTO_SCAN_CACHE`, `AUTO_SCAN_KEEP_CACHE` | env | The download cache (`~/.cache/auto-scan`), and `1` to keep its large files. |
| `AUTO_SCAN_DB` | env | The local `scan.db`. |
| `AUTO_SCAN_ALLOW_SHRINK` | env | `1` lets `push` upload a database with fewer events than it pulled. |

`CYLC_MONITOR_READER_TOKEN` and `AUTO_SCAN_WRITER_TOKEN` can also come from files
(`~/.config/cylc-monitor/reader-token`, `~/.config/auto-scan/writer-token`, or the
paths in `<NAME>_FILE`).

### `SCAN_GH_TOKEN`

Use a fine-grained personal access token with resource owner `ucgmsim`, limited to
this repository and every repository a run's manifest names in `sources` (the
workflow itself, `sw4`, `workflow`, `source_modelling`, `IM_calculation`, `qcore`, and
so on). Give it these repository permissions:

- **Issues: read and write.** It views, files, comments on, reopens and labels issues,
  and creates the `scan`, `ai-suggested`, `ai-ok`, `ai-serious` and
  `needs-investigation` labels.
- **Contents: read.** It clones private repositories to read the suspect commits.
- **Pull requests: read.** It searches for duplicates.
- **Metadata: read.** GitHub requires this one.

Don't give it contents write: the triage only reads code.

Each repository's `ai-issue-solver` acts on an `ai-ok` label only if a maintainer
applied it, or if a trusted bot applied it to an issue a bot opened. So the token
must belong to a maintainer listed in that workflow's `maintainers` input, or be the `ucgmsim[bot]` app's token. Otherwise the
solver ignores the issues the triage files.

### Setting them

```sh
gh secret set SCAN_GH_TOKEN -R ucgmsim/auto_scan
gh secret set CYLC_MONITOR_READER_TOKEN -R ucgmsim/auto_scan < ~/.config/cylc-monitor/reader-token
gh secret set AUTO_SCAN_WRITER_TOKEN -R ucgmsim/auto_scan < scan-writer.token
gh variable set DATASETTE_URL -R ucgmsim/auto_scan --body 'https://<your-datasette>'
```

## The workflow

`.github/workflows/scan.yml` has two jobs, and never runs twice at once
(`concurrency: scan`), because both read and write the one `scan.db`.

- `scan` installs a pinned rclone (the official .deb, checked against its sha256),
  then runs `auto-scan run`. It writes a job summary from the database: runs scanned,
  and findings opened, verified, regressed and dismissed. It outputs the number of
  open, unfiled findings. When no manifest changed, it only pulls, reads the monitor,
  lists the results and pushes: a couple of minutes.
- `triage` runs when that number is above zero. It pulls, writes `.ai/brief.md` (at
  most `max-findings`, 5 by default) and runs `anthropics/claude-code-action@v1` with
  `prompts/triage.md`. Then it pushes, so the links Claude recorded are stored. Claude
  works with `SCAN_GH_TOKEN` (the action's `github_token` input, which the action also
  exports as `GH_TOKEN` to Claude's tools). Its tools are limited to reading files,
  `uv run`, read-only `git`, `rclone copy`/`lsf`/`cat`, `tar`, and `gh` issue and label
  commands. The job summary has Claude's final message and any tool calls the
  allowlist refused.

Run it by hand from the Actions tab. The inputs are `event` (rescan one event with
`--force`), `triage` (off to skip Claude), `max-findings` and `model`.

## The CMT review

`.github/workflows/cmt.yml` reviews the automatic centroid moment tensors SeismicNow
posts in the same way: `cmt_solutions`' `auto_cmt` runs BayesISOLA three times an event
(`all_stations`, `azimuth`, `drop_outliers_dev`), and each posting is checked for
scientific problems (the solution's internal consistency, the runs' agreement, the fit,
the grid, the posterior, the magnitude against GeoNet's) and performance problems
(`timings.csv`'s stage timings against their history). The checks are listed in
`src/auto_scan/cmt/checks.py`'s docstring.

- It keeps its own database, `cmt.db` (`AUTO_SCAN_CMT_DB`), in the same state store
  as `scan.db`, with the same schema and findings lifecycle (`--review cmt` on any
  subcommand). Each CMT run is a `runs` row whose `resolution` is the run's name.
  `push` uploads it to `/-/scan/upload/cmt`, which makes it the Datasette's `cmt`
  database, with its dashboard at `/-/dashboards/cmt` (cylc-monitor's
  `server/cmt_views.sql` and `server/datasette.yaml`).
- Every 30 minutes it reads SeismicNow's listing (`/earthquakes?has_cmt=true`) and
  reviews each CMT applied since the last run, or re-applied after a re-run. The first
  run takes nothing and only marks where to start from.
- Older events are reviewed only when named: the workflow's `events` input
  (space-separated ids, `force` to redo one), a `repository_dispatch` of type
  `cmt-posted` with `client_payload.events`, or locally
  `uv run auto-scan --review cmt collect <id> ... && uv run auto-scan --review cmt check`.
  An event whose download has no `cmt_selection.csv` is skipped with a message.
- The triage (`prompts/cmt_triage.md`) files every issue in `ucgmsim/cmt_solutions`,
  assigned to Joel Ridden (`joelridden`) and mentioning him. A cause in BayesISOLA's
  code is labelled `BayesISOLA`, since we can't file in AaronJR474/BayesISOLA.
  `SCAN_GH_TOKEN` must therefore cover `ucgmsim/cmt_solutions` (Issues: read and write;
  Contents: read).
- SeismicNow's results record no code versions, so a CMT finding has no commit range:
  the triage reads the default branches.

It looks at each posting in three ways, because a check only finds what someone thought
to test:

- **Checks** (`src/auto_scan/cmt/checks.py`): one finding per problem, across events.
  Pipeline checks aim at a kind of bug or cost. Product criteria ask whether the main
  solution can be trusted: stations, fit, signal, magnitude, grid edge, jackknife,
  station misfit, agreement between runs, isotropic share. They compare with fixed
  references, not with the history, so a run of poor solutions can't become the norm.
- **Verdicts** (`src/auto_scan/cmt/verdict.py`): each posted solution is graded
  `good`, `doubtful` or `reject` from the product criteria. A rejected one is a finding
  of its own, `cmt.verdict` with the event as its subject, so one event's dismissal
  never speaks for another. Three `cmt.population.*` checks then look at the last 20
  verdicts together: the share rejected, the share of largely isotropic full tensors,
  and whether Mw is inflated where the fit is poor.
- **The open-ended review** (`src/auto_scan/cmt/observe.py`, the workflow's `explore`
  job): one Claude per event not yet read, with the whole download (figures included)
  and a sheet of numbers (`auto-scan --review cmt sheet <event>`), and with no word of
  what the checks test (`prompts/cmt_explore.md`). What it sees goes in the
  `observations` table, each under a key naming the phenomenon. An event is read once,
  and again if its CMT is re-run.

The triage then takes **every** open finding and observation in one run (`max-findings`
is 0, all, by default). It has a fourth outcome besides a code cause, an unexplained
problem and a dismissal: the published result is wrong though no code is broken, which
it files once per policy gap with the label `cmt-quality` and links every event's
verdict to. For each observation it decides: already covered (`resolve <key> known`),
new (an issue for the problem, and one in this repository proposing the check that would
catch it from now on; `resolve <key> filed`), or not a problem (`resolve <key> dismissed`).

`ci.yml` and `claude-review.yml` call meta-ci-action's shared workflows (`@v2`), as
every ucgmsim repository does. `lefthook.yml` runs the same checks as git hooks
(`uvx lefthook install`).

## Deploying the server side

The scan's database and dashboard live on the Cylc monitor's Datasette, as a second
database (`scan`) beside `cylc`, and the CMT review's as a third (`cmt`). That
server side is part of [cylc-monitor](https://github.com/ucgmsim/cylc-monitor), in its
`server/`: the upload plugin (`plugins/scan_upload.py`), the views (`scan_views.sql`,
`cmt_views.sql`) and the two dashboards (in `datasette.yaml`). Its README, sections
"auto_scan's databases" and "Deploying the server", says how to deploy it and mint the
token.

What auto_scan relies on:

- `POST /-/scan/upload` (scan.db) and `POST /-/scan/upload/cmt` (cmt.db). Each takes a
  gzipped snapshot, from the `scan-writer` actor only, up to 200 MB. The plugin checks
  that the snapshot is an SQLite database with a known `meta.schema_version`. Then, in
  one transaction, it replaces every table of the target database with the snapshot's
  and remakes its views. It also registers the SQL aggregate `stdev()`, which the views
  use.
- The dashboards, `/-/dashboards/scan` and `/-/dashboards/cmt`. Until a database's
  first upload, its dashboard errors, because the views need the tables.
- The `scan-writer` token, made on the server with
  `python /app/make_token.py scan-writer` and stored as the repository secret
  `AUTO_SCAN_WRITER_TOKEN` (above). Rotating the server's `DATASETTE_SECRET` revokes it
  with all the others.
- The server's URL, in the repository variable `DATASETTE_URL`.

To check a deployment, run the workflow once by hand. `push` logs
`uploaded to .../-/scan/upload`, and `https://<your-datasette>/-/dashboards/scan` shows
the scan. Run the CMT review workflow too: `/-/dashboards/cmt`.

A change to `db.py`'s tables, to a metric a view reads or to a dashboard is a change in
cylc-monitor too. A new `SCHEMA_VERSION` needs the plugin's `SCHEMA_VERSIONS` and the
views updated there first, or the server refuses the upload.

### Moving the Datasette

The Datasette's address is one setting: the repository variable `DATASETTE_URL`.
Deploy cylc-monitor's server on the new host, mint a new `scan-writer` token there,
then:

```sh
gh variable set DATASETTE_URL -R ucgmsim/auto_scan --body 'https://<new-datasette>'
gh secret set AUTO_SCAN_WRITER_TOKEN -R ucgmsim/auto_scan < scan-writer.token
```

Nothing else names the host. `scan.db` lives in the state store, not on the server,
so the next push fills the new server. If the monitor stays behind while the scan
moves, also set `AUTO_SCAN_MONITOR_URL` in the workflow's env.

## Adding a check

A check is a function in `src/auto_scan/checks/<area>.py`, registered with `@check`.
It reads only the database and yields an `Evaluation` per subject, passing or
failing (see `registry.py`). Most checks need a metric first: write it in an
extractor under `src/auto_scan/extract/` with `db.set_metric`, and document it in the
module docstring's table. DESIGN.md has the rules. Briefly:

- Yield an evaluation whether it passes or fails, and nothing when an input is
  missing. Passes are how a fix gets verified.
- Thresholds are module constants, each with a comment saying where the number comes
  from. Compare with the metric's own history (`db.metric_history`) once there are 5
  earlier events.
- The `title` names the problem without one event's numbers. The `summary` has them.
- `suspect_sources` names the `run_sources.source` entries likely at fault (or
  `realtime`). The triage reads the commit range of those.
- `evidence` holds what is needed to reproduce the number.

Add a test with a small synthetic database. Then `uv run auto-scan check --force`
locally shows what it would have found on the events so far. If the dashboard should
show the new metric, add a column to a view in cylc-monitor's `server/scan_views.sql`
and a chart to the `scan` dashboard in its `server/datasette.yaml`. `metric_values`
already shows any metric's history.

## A finding's life

`(check_id, subject)` names a finding.

- **open**: a check failed on the latest event (or on 2 of the last 5), and no live
  finding existed. Later failures update its summary, evidence and `n_failing`.
- **filed**: the triage, or a person, filed or found its issue and ran `auto-scan link
  ID URL`. The brief stops listing it.
- **fix_pending**: its issue was closed as completed. `verify` then looks at the
  check on runs whose manifest was written after the close:
  - any failure: the issue is reopened with a comment and the new numbers, and the
    finding goes back to **filed**, with the regression recorded in its evidence;
  - passes on 1 later event (2 for a science check), and no failure: **verified**,
    with a comment on the issue naming the events.

  An issue reopened by hand sends the finding back to **filed**.
- **dismissed**: the triage or a person judged it not a problem
  (`auto-scan dismiss ID "reason"`), or its issue was closed as not planned or as a
  duplicate. The same check and subject don't open a new finding for 90 days.
- An **open** finding that passes on 3 consecutive later events closes itself as
  **verified**, without an issue.

To take a finding out of the triage's hands, file or find the issue yourself and run
`auto-scan link`. To stop it, `auto-scan dismiss`. Locally, run `auto-scan pull` first
and `auto-scan push` after, and not while the workflow is running: the workflow's next
push would overwrite a change made in between.
