# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

* **License monitoring.** A discovery rule over `scontrol show licenses` with
  total, used, free, reserved and usage per pool, plus a trigger that fires when
  a pool is exhausted *and* jobs are queueing for a license — the combination
  that means the pool is actually too small.
* **Reservation discovery.** Per reservation state, node and core counts, time
  until it starts and time remaining. Reservations explain why nodes are out of
  service, so they carry no triggers of their own. Lost reservations are cleaned
  up after a day rather than a week.
* **Node outage duration.** Slurm records who drained or downed a node and when;
  that timestamp is now parsed into "unavailable for", per node and as a cluster
  wide longest outage. Two triggers escalate capacity that was taken out of
  service and never brought back, which is easy to lose track of on a large
  cluster. The metric resets to 0 as soon as a node is usable again, so a node
  returning to service does not keep alerting.
* **Job accounting from `sacct`** behind a third master item, `slurm.accounting`:
  completion and failure counts by ending, success and failure rate, mean and
  longest queue wait, mean and longest runtime, and CPU hours delivered over a
  rolling window. It ships **disabled** and runs on its own 15 minute schedule
  with its own cache, because it is the only collection that queries the
  accounting database. `install.sh --accounting` sets up the agent side.
* **A lock around collection.** When both master items found the cache expired in
  the same second they each ran a full sweep of `scontrol`/`squeue` for identical
  data. Collection now takes a lock: the second caller waits and reads the fresh
  cache instead of doubling the load on `slurmctld`. Failing to take the lock is
  never fatal — collection still happens, just unserialised.
* **Continuous integration.** GitHub Actions runs the test suite on Python 3.9,
  3.11 and 3.13, validates the template, fails if the committed XML no longer
  matches the builder, and runs ShellCheck over the shell scripts.

### Changed

* *Slurm: Jobs are blocked while the cluster has free CPUs* counted every
  pending job, so it fired whenever anything was waiting on a dependency — which
  no amount of free capacity can resolve. It now counts only jobs held back by a
  QOS, association or partition limit, or by a reason the collector does not
  recognise, exposed as the new item *Pending jobs - blocked by a limit*. Jobs
  waiting for a dependency, a licence, a reservation or a hold are excluded, and
  the trigger is renamed to say what it means.
* The template dashboard no longer starts its slideshow automatically. Zabbix
  does that by default on a multi-page dashboard, rotating the page away while
  somebody is reading it.
* *Slurm: Backfill scheduler has not run for N* now also requires pending jobs.
  The backfill scheduler only runs when there is something to backfill, so on an
  idle cluster the age of its last cycle grows by itself and the trigger fired
  on a perfectly healthy system. The queue has to have been continuously
  non-empty for 15 minutes for the alert to mean anything.
* `--refresh` no longer prints the document when a mode is given explicitly. It
  exists to warm the cache from the systemd timer, where the output only filled
  the journal; use `--no-cache` to force a collection and see the result.

* `{$SLURM.NODE.MEMORY.FREE.MIN}` now defaults to `0`, which disables the
  *Node free memory is below N%* trigger. Slurm's `FreeMem` does not count
  reclaimable page cache as free, so the value falls towards zero on any node
  that has been running jobs and the trigger fired constantly on healthy
  clusters. Real memory pressure is better monitored with an operating system
  template, which reports available memory. The macro can be raised globally,
  per host, or per node where `FreeMem` is known to be meaningful.

### Fixed

* **The template could not be imported.** Zabbix accepts version 4 UUIDs only
  and rejected the export with "UUIDv4 is expected", because the UUIDs were
  derived with `uuid5`. They are still derived from each object's path, so they
  stay stable across builds, but the version and variant bits are now set to
  make them well formed v4 values. Every UUID in the template changed as a
  result; since no earlier version could be imported, there is nothing to
  migrate.
* **Trigger expressions using `length()` would have failed validation.**
  `length()` is a string function and takes a value, so it has to be written
  `length(last(/host/key))` rather than `length(/host/key)`. Affects the two
  node drain triggers and the Slurm version trigger.
* **Dashboard widgets referenced their objects with the pre-7.0 field names.**
  Zabbix 7.0 indexes them (`itemid.0`, `graphid.0`) and expects a `reference`
  on graph and graph prototype widgets; the previous spelling imported without
  complaint and rendered empty widgets. Item value widgets now also set the
  `show` options explicitly.
* The template group now carries the UUID Zabbix ships for
  `Templates/Applications`, so the import maps onto the existing group.
* Plain agent items no longer spell out `<type>ZABBIX_PASSIVE</type>`, matching
  the official templates, which rely on it being the default.

All of the above are now covered by `tools/validate_template.py` and the test
suite, checked against the widget and expression conventions used by the
official Zabbix 7.0 templates.

* `install.sh` now writes the UserParameters to
  `/etc/zabbix/zabbix_agent2.d/plugins.d/slurm.conf` on hosts running Zabbix
  agent 2. The packaged agent 2 includes `plugins.d/*.conf` only, so the
  previous target (`zabbix_agent2.d/slurm.conf`) was never read and every item
  came back as "Unsupported item key". The installer creates the directory when
  missing and warns when the agent configuration has no `Include` covering it.

## [1.0.0] - 2026-08-13

First release.

### Added

* **Collector** (`bin/slurm_zabbix.py`): queries `scontrol`, `squeue`, `sdiag`
  and `sacctmgr`, and emits the whole cluster state as one JSON document.
  Parses the stable text output rather than `--json`, so it works across Slurm
  releases from 20.02 onwards. Caches on disk with atomic writes, reports
  collection failures in-band instead of failing, and omits values Slurm does
  not provide so dependent items never go unsupported.
* **Zabbix 7.0 template** (`templates/slurm_cluster_7.0.xml`): two master items
  and 107 dependent cluster metrics covering node states, CPU, memory, GPU, job
  states, pending reasons, queue latency, scheduler and backfill statistics,
  reservations and collector health.
* **Discovery** of partitions (26 items each), QOS (7 items each) and nodes
  (21 items each), each filterable by macro.
* **Triggers**: 21 cluster triggers and 15 trigger prototypes, chained by
  dependency so a controller outage produces one alert rather than dozens.
* **Graphs and dashboards**: 13 graphs, 5 graph prototypes, and a four page
  template dashboard for the cluster, the scheduler, partitions and nodes.
* **Deployment**: agent UserParameters, a systemd timer for cache refresh on
  large clusters, and `install.sh`.
* **Tooling**: `tools/build_template.py` generates the template from a
  declarative definition with deterministic UUIDs;
  `tools/validate_template.py` checks references, macros, widget geometry and
  resolves every JSONPath against real collector output.
* **Tests**: 71 unit and end-to-end tests running against recorded Slurm output,
  including fault injection that proves the template validator detects broken
  references, dead JSONPaths and overlapping widgets.

[1.0.0]: https://github.com/Kevin-NZ/Zabbix-Slurm/releases/tag/v1.0.0
