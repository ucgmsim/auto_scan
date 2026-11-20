# auto_scan: design and contracts

auto_scan reviews every automated realtime earthquake simulation
([realtime_simulation_workflow](https://github.com/ucgmsim/realtime_simulation_workflow))
for scientific and performance problems, files a GitHub issue in the repository
at fault, and checks on later events that the fix worked. It never touches the
cluster: every input is already published off it.

## The loop

```
scheduled GitHub Action (scan.yml)
  auto-scan pull      scan.db from the state store (rclone)
  auto-scan collect   new or changed runs -> cache -> extractors -> rows
  auto-scan check     checks -> evaluations -> findings (open)
  auto-scan verify    filed findings: issue closed? later events pass? -> verified / regressed
  auto-scan push      scan.db -> state store, and -> Datasette (dashboard)
triage job (same workflow, only when findings are open)
  auto-scan brief     the open findings, their evidence, the suspect commit ranges
  Claude              confirms each in the source, files an issue in the owning repo
                      (labels ai-suggested, ai-ok, scan), then `auto-scan link ID URL`
ucgmsim/meta-ci-action's ai-issue-solver, in that repo, attempts the fix (ai-ok)
next events          the check passes on a run built after the fix -> `verified`,
                      a comment on the issue; still failing -> issue reopened, `regressed`
```

## Inputs (all verified readable off-cluster)

| Source | How | What |
|---|---|---|
| Results | `rclone` at `Config.results_url`: `<cycle>-<event>/<res>/` | `manifest.json`, `sw4_run.json`, `sw4_estimate.json`, `logs.tar.gz`, `realisation.json`, `shake_movie.json`, `simdb/<event>.duckdb`, `simdb/<event>_waveforms.nc`; event level: `realisation.json`, `realisation.toml`, `realisation.geojson` |
| Cylc monitor | `GET {monitor_url}/{monitor_database}.json?sql=...&_shape=array`, `Authorization: Bearer <reader token>`; it answers 302, follow it | the `jobs` view (queue, run, cores, state, `job_err` tail) |
| SimDB | `GET {simdb_api}/api/events/<id>/station-ims?dataset=realtime` (public; the default `simdb_api` already ends in `/api`, so the URL has `/api/api/`) | simulated IMs at real stations; only the finest resolution ingested |
| SeismicNow | `GET {seismicnow_api}/download/flatfiles?event_id=<id>` (public) | a zip of NZGMDB-style flatfiles: observed IMs per component, `gmc_predictions.csv`, `snr_metadata.csv`, `fmax.csv`, `phase_arrival_table.csv`, `site_table.csv`, `earthquake_source_table.csv` |
| SeismicNow CMT | `GET {seismicnow_api}/earthquakes?page=<n>&has_cmt=true&event_id=&min_magnitude=&max_magnitude=&min_depth=&max_depth=` (pages from 0, every key required) and `GET {seismicnow_api}/download/cmt_results?event_id=<id>&scope=all` (public) | the earthquakes with a posted CMT and `cmt_applied_at`; a zip of every run's result tables, figures, reports, logs and `timings.csv` (CMT review only) |

Both quakecoresoft APIs need `Config.user_agent` (Cloudflare blocks urllib's).
A real event's files are in the session scratchpad for reference (paths in each
agent's brief); tests use small synthetic fixtures and never the network.

## Cache layout (what the collector writes, what extractors read)

```
<cache_dir>/<cycle>-<event_id>/            event_dir
    realisation.json  realisation.toml  realisation.geojson
    observed/                              the SeismicNow flatfile zip, unpacked (absent if none)
        ground_motion_im_table_rotd50_flat.csv  gmc_predictions.csv  snr_metadata.csv ...
    simdb_station_ims.json                 SimDB's station-ims response (absent if not ingested)
    <resolution>/                          run_dir
        manifest.json  sw4_run.json  sw4_estimate.json  realisation.json  shake_movie.json
        logs/log/job/<cycle>/<task>/<NN>/{job.out,job.err,job,sw4.log,sw4_timers.csv,ranks/}
        simdb/<event_id>.duckdb            every site's IMs (imdb schema: ~/src/imdb)
        simdb/<event_id>_waveforms.nc      group <event_id>: acc(station, component, time) in g
```

Files a run didn't upload are simply absent. Large files (`*.duckdb`, hundreds
of MB) are deleted from the cache once the run's extractors have finished, unless
`AUTO_SCAN_KEEP_CACHE=1`.

## Code layout and ownership

```
src/auto_scan/
  config.py     settings, one env var each                 (contract: fixed)
  db.py         schema + set_metric/get_metric/history     (contract: fixed)
  registry.py   @extractor, @event_extractor, @check, Evaluation   (contract: fixed)
  cli.py        argument parsing only                      (contract: fixed)
  sources/      monitor.py, results.py (rclone), simdb.py, seismicnow.py     [core]
  collect.py    collect(config, db, event=None, force=False)                 [core]
  engine.py     run_checks(db, event=None); findings lifecycle               [core]
  extract/science.py   residuals, cross-resolution, waveforms, source        [science]
  checks/science.py                                                          [science]
  extract/perf.py      timers, sw4_run/estimate, job logs                    [perf]
  checks/perf.py, checks/ops.py                                              [perf]
  publish.py    pull(config, db_path), push(config, db_path, datasette=True) [deploy]
  issues.py     brief(db, max_findings), link(db, id, url), dismiss(db, id, reason), verify(db)  [deploy]
  cmt/          the CMT review: source.py, collect.py, extract.py, checks.py (see below)  [cmt]
.github/workflows/, prompts/, README.md                                      [deploy]
tests/test_<module>.py                                                       each owner
```

Don't edit a `contract: fixed` file or another owner's file. If the contract is
wrong or missing something, say so in your final report with the exact change
you need, and work around it meanwhile (e.g. a helper in your own module).
Don't run `uv add`: the dependencies are numpy, pandas, scipy, duckdb, xarray,
h5netcdf, h5py and the standard library (HTTP with `urllib`, rclone and `gh`
with `subprocess`). If you truly need another, name it in your report.

## Rules for extractors

- Registered with `@extractor` (per run) or `@event_extractor` (per event).
- Write to `metrics` with `db.set_metric`, and to `station_residuals` /
  `sw4_timers` directly. Delete your own earlier rows for the run first
  (idempotent). Never commit: the collector commits once per event.
- A missing or malformed file means no rows, a one-line warning on stderr and no
  exception. One event's bad upload must not stop the scan.
- Metric names are dotted and prefixed by owner: `science.*`, `perf.*`, `ops.*`.
  Document every metric you write in a table in the module docstring.

## Rules for checks

- Registered with `@check`; read only the database; yield `Evaluation`s, passing
  or failing, and nothing when an input is missing.
- Thresholds are module-level constants with a comment saying where the number
  comes from. Prefer a comparison with the metric's own history
  (`db.metric_history`) once there are at least 5 earlier events, and an absolute
  bound before that. Few events exist so far (about 12): a check must not fire on
  noise. Aim for checks that would each point at a specific kind of bug.
- `title` has no event-specific numbers (it names the finding across events).
  `suspect_sources` uses `run_sources.source` names, or `realtime`.
- `evidence` should let the triage reproduce the number: the metric names, the
  values, the history, the files.

## Findings lifecycle (engine.py, issues.py)

`(check_id, subject)` names a finding. States:

- `open`: a check failed and no live finding existed. Further failures update
  `last_event`, `n_failing`, `summary`, `evidence`.
- `filed`: `auto-scan link` recorded its issue. Not reported in the brief again.
- `fix_pending`: its issue was closed (`verify` reads it with `gh issue view`).
- `verified`: after the issue closed, the check passed on a run whose manifest
  was written after the close. `verify` comments on the issue naming the event.
- `regressed`: after the issue closed, the check failed on such a run. `verify`
  reopens the issue with a comment and the new evidence; the finding goes back
  to `filed`.
- `dismissed`: a person or the triage judged it not a problem; the same
  `(check_id, subject)` is not reopened for 90 days.
- An `open` finding that passes on 3 consecutive later events closes itself as
  `verified` with no issue (it was transient).

A new finding is opened only when the check fails on the latest event simulated,
or on at least 2 of the last 5: old one-off failures in the backfill don't open
findings nobody can act on.

## Order of events, backlog events and re-runs

Events are ordered by **when they were simulated** (the earliest `written` of
their manifests, `db.SIMULATED_AT`), never by the earthquake's origin time,
wherever the question is "which code ran": `metric_history` (per run, by that
run's `written`), the findings lifecycle ("the latest event", "2 of the last 5",
"3 later passes"), the suspect commit range and `verify`. Origin time is for
display, and for the time from the earthquake to its results.

A *backlog event* is an old earthquake put through the workflow later, by hand
or while catching up after an outage. By the rule above it is simply the latest
run: its history is the runs made before it, a failure on it can open a finding,
and a pass on it counts towards a fix. `db.is_backlog` (an earthquake with a
later origin was simulated first) marks it for the one check that measures
reaction time, `ops.pickup_delay`, which skips it. It is retried for late SimDB
and SeismicNow data for two weeks from its simulation, not from its origin.

An event simulated more than once has one directory per cycle. The database
holds one simulation per event, the one whose manifest was written last (cycle
numbers restart when the workflow is reinstalled, so they can't decide). When a
newer one appears, every row derived from the old one is deleted and the event
is ingested afresh, so it moves to the end of every history; its findings stay,
and a re-run that passes after a fix verifies it on the same earthquake. The
directories left unscanned are listed in the meta row `superseded:<event_id>`.

## Suspect commit range

For a finding, per suspect source: the commit in `run_sources` on the last event
simulated where the check passed, and on the first where it failed. The brief gives both as
a GitHub compare URL (`<url>/compare/<good>...<bad>`). No passing event means no
range ("failing since the first event scanned").

## The CMT review

A second review over the same machinery: SeismicNow's automatic CMT inversions
(`src/auto_scan/cmt/`, its package docstring has the table mapping). Its own
database, `cmt.db`, has `scan.db`'s schema, so `engine`, `issues.verify`, `link`,
`dismiss` and `publish` work unchanged; `--review cmt` on the CLI picks the
database, the collector (`cmt.collect`), the checks (`registry.CMT_CHECKS`, added
with `@cmt_check`; extractors with `@cmt_extractor`) and the brief's wording. A CMT
run is a `runs` row (`resolution` = `all_stations`, `azimuth` or
`drop_outliers_dev`); `runs.written` is when `cmt_solution.csv` was written (the
download's zip time, else SeismicNow's `cmt_applied_at`), so the ordering rules above
hold: a CMT computed later for an older earthquake is a backlog event. Check ids and
metrics start `cmt.`. `run_sources` stays empty (no code versions are published), so
there is no suspect commit range. `push` uploads it to `/-/scan/upload/cmt`, the
Datasette's `cmt` database (views in cylc-monitor's `server/cmt_views.sql`, dashboard
`/-/dashboards/cmt`).

The schedule only takes CMTs applied after the watermark (meta `cmt_since`, set on
the first run) or re-applied since ingested (meta `cmt_applied:<event_id>`); any
other event is reviewed by naming it. Nothing in the code knows about SeismicNow's
older CMT layout: an event without `cmt_selection.csv` simply has no runs.

## Hosting

`scan.db` lives at `Config.state_url` (Dropbox), so the Datasette is a view that
can move: set `AUTO_SCAN_DATASETTE_URL` (GitHub variable `DATASETTE_URL`) and
deploy the server side there. That side (the upload plugin, the views and the
dashboards) is part of ucgmsim/cylc-monitor's `server/`, deployed with the Cylc
monitor. `push` POSTs the database to `{datasette_url}/-/scan/upload` (cmt.db to
`/-/scan/upload/cmt`) with the `scan-writer` token (`AUTO_SCAN_WRITER_TOKEN`); the
plugin replaces the database's tables in one transaction. A change to the tables
(`SCHEMA_VERSION`) needs the plugin and views in cylc-monitor changed first.

## Style

Python 3.13, `ruff` at line length 120, numpydoc docstrings on every public
function (CI lints them with npdlint), type hints that pass `ty check`. Comments
say why, not what. Tests with pytest, fast and offline.
