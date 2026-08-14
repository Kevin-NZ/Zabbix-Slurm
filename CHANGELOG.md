# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
