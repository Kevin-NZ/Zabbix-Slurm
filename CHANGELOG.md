# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
