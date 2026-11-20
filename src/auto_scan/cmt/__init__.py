"""The CMT review: every automatic CMT inversion SeismicNow posts, checked like a simulation.

SeismicNow (the NZGMDB pipeline) runs ``cmt_solutions``' ``auto_cmt``, which runs
BayesISOLA's 1-D centroid moment tensor inversion three times an event
(``all_stations``, ``azimuth``, ``drop_outliers_dev``), picks a main solution and
posts it. This package reviews each one for scientific and performance problems,
with the same findings lifecycle and triage as the simulations, in its own
database, ``cmt.db`` (the same schema as ``scan.db``):

==================== ================================================================
table                holds, for the CMT review
==================== ================================================================
events               one row an event with a CMT: origin time, the main solution's Mw
                     and centroid, ``event_dir`` = the event id (its cache directory)
runs                 one row a CMT run: ``resolution`` is the run's name
                     (``all_stations``, ...), ``status`` its status (``ok`` or the
                     failure), ``written`` when SeismicNow applied the solution
                     (``cmt_applied_at``): what orders events as runs of the code
metrics              ``cmt.*``; ``resolution`` is the run, or ``''`` for the event
evaluations          as for the simulations; check ids start ``cmt.``
findings             as for the simulations
run_sources          empty: SeismicNow's results record no code versions
==================== ================================================================

Modules: ``source`` (SeismicNow's listing and download), ``collect`` (which
events to ingest), ``extract`` (the metrics), ``checks`` (the checks).
"""
