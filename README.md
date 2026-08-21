# Zabbix template for Slurm clusters

Monitoring for a [Slurm](https://slurm.schedmd.com/) workload manager cluster
with Zabbix 7.0: a collector script, a template with discovery, triggers,
graphs and dashboards, and the agent glue to tie them together.

The whole template costs **two Zabbix agent checks per interval**, no matter how
large the cluster is. Both checks return one JSON document, and every one of the
109 cluster metrics plus the per partition, per QOS, per license, per
reservation and per node metrics is a dependent item derived from those
documents. Adding a metric costs nothing extra on `slurmctld`.

```
                           ┌──────────────────────────────┐
  scontrol ─┐              │ slurm.cluster      (1m)      │─▶ 109 cluster metrics
  squeue   ─┼─▶ collector  │ slurm.nodes        (2m)      │─▶ 5 discovery rules
  sdiag    ─┤   (cached,   │ slurm.accounting  (15m, off) │     partitions   → 26 items
  sacctmgr ─┤    locked)   └──────────────────────────────┘     QOS          →  7 items
  sacct    ─┘                     Zabbix agent                  nodes        → 23 items
                                                                licenses     →  6 items
                                                                reservations →  8 items
```

A third master item collects job throughput from `sacct`. It ships **disabled**,
because it is the only collection that queries the accounting database.

## Contents

| Path | Description |
| --- | --- |
| `bin/slurm_zabbix.py` | Collector; queries Slurm and prints JSON |
| `templates/slurm_cluster_7.0.xml` | The Zabbix 7.0 template, ready to import |
| `agent/slurm.conf` | Zabbix agent UserParameters |
| `systemd/` | Timer that refreshes the collector cache |
| `install.sh` | Installs the collector, the cache directory and the agent config |
| `tools/build_template.py` | Generates the template XML from its declarative definition |
| `tools/validate_template.py` | Checks the template for broken references and dead JSONPaths |
| `tests/` | Unit and end-to-end tests, with recorded Slurm output as fixtures |
| `.github/workflows/ci.yml` | Runs the tests, the validator and ShellCheck on every push |

## Requirements

* Slurm 20.02 or newer (`scontrol`, `squeue`, `sdiag`, optionally `sacctmgr`)
* Python 3.6 or newer, standard library only
* Zabbix 7.0 server and agent (agent 1 or agent 2)
* A host running the Zabbix agent with working Slurm client commands, usually the
  controller or a login node. **No root privileges are needed** — the collector
  only reads cluster state, as any user could with `sinfo`.

## Installation

### 1. Collector and agent

```sh
git clone https://github.com/Kevin-NZ/Zabbix-Slurm.git
cd Zabbix-Slurm
sudo ./install.sh              # or: sudo ./install.sh --timer
```

The installer copies the collector to `/usr/local/bin/slurm_zabbix.py`, creates
the cache directory `/var/lib/zabbix-slurm/`, writes the UserParameters into the
agent include directory, and verifies that collection works as the `zabbix` user.

The include directory differs between the two agents:

| Agent | UserParameter file |
| --- | --- |
| Zabbix agent 2 | `/etc/zabbix/zabbix_agent2.d/plugins.d/slurm.conf` |
| Zabbix agent | `/etc/zabbix/zabbix_agentd.d/slurm.conf` |

Agent 2 includes `zabbix_agent2.d/plugins.d/*.conf`, **not**
`zabbix_agent2.d/*.conf`, so a file dropped one directory too high is never
read. The installer picks the right path and warns if your agent configuration
has no `Include` covering it; with `--agent-dir` you can override it.

Without `--timer` the agent runs the collector itself, so the agent timeout has
to be raised in `zabbix_agentd.conf` / `zabbix_agent2.conf`:

```
Timeout=30
```

Then restart the agent and check from the Zabbix server:

```sh
zabbix_get -s slurm-head01 -k slurm.cluster | head -c 400
```

### 2. Template

Import `templates/slurm_cluster_7.0.xml` through *Data collection → Templates →
Import*, then link **Slurm cluster by Zabbix agent** to the host running the
agent.

The host represents the cluster, not a compute node — name it after the cluster
(`hpc-prod`) rather than after the machine the agent happens to run on.

### 3. Check

Within a few minutes *Latest data* should show the cluster metrics, and
*Data collection → Hosts → Discovery* should list the discovered partitions,
QOS entries and nodes.

## Collection modes

The collector caches its results on disk, so both master items share a single
query of Slurm. Collection is serialised with a lock file, so when both items
poll in the same second and find the cache expired, one collects and the other
waits for the result instead of running a second sweep against `slurmctld`.

| Mode | UserParameter | When to use |
| --- | --- | --- |
| Direct | `--cache-ttl 55` | Default. The agent collects when the cache expires. Needs `Timeout=30`. |
| Timer | `--cache-only` + systemd timer | Above a few hundred nodes, or when `squeue` is slow. Agent checks return instantly. |

In timer mode the agent never runs a Slurm command. If the timer stops, the
data age keeps growing and the template raises *Slurm: Collected data is stale*
instead of quietly reporting old numbers.

### Job accounting (optional)

A third mode, `--mode accounting`, reads finished-job throughput from `sacct`:
completion and failure counts by ending, success and failure rate, mean and
longest queue wait, mean and longest runtime, and CPU hours delivered over a
rolling window (one hour by default).

It is opt-in on both sides, because it is the only collection that queries the
accounting database and it is the expensive one on a busy cluster:

```sh
sudo ./install.sh --accounting        # adds the slurm.accounting UserParameter
```

then enable the **Slurm: Get accounting data** item on the host. Every
accounting metric depends on that one item, so enabling it switches the whole
feature on. It runs on its own 15 minute schedule with its own cache, so it
never slows down the one minute cluster poll.

## What is collected

### Cluster level (master item `slurm.cluster`, 1 minute)

| Group | Metrics |
| --- | --- |
| Health | slurmctld and backup controller availability, slurmdbd availability, collector errors, data age, collection duration, cluster name, Slurm version, scheduler and select type |
| Nodes | total, idle, allocated, mixed, down, failed, drained, draining, maintenance, reserved, completing, planned, powered down, pending reboot, future, unknown, not responding, available, availability % |
| CPU | total, allocated, idle, unusable, allocation % |
| Memory | total, allocated, free, allocation % |
| GPU | total, allocated, idle, allocation % |
| Jobs | running, pending, suspended, completing, configuring, other, CPUs requested by running and pending jobs, active users and accounts, oldest and mean queue wait, longest running job, MaxJobCount and job table usage |
| Pending reasons | resources, priority, dependency, QOS limit, association limit, licenses, reservation, partition, nodes unavailable, held, other, plus the five most frequent raw reasons as text |
| Queue shape | jobs ready to run (excluding dependency, hold and reservation waits) and jobs blocked by a limit |
| Scheduler (`sdiag`) | controller thread count, agent queue, DBD agent queue, submission/start/completion/cancellation/failure rates, main cycle last/mean/max, cycles per minute, queue length, mean depth |
| Backfill | cycle last/mean/max, mean and last depth, queue length, backfilled job rate, time since the last cycle |
| Reservations | total, active, nodes reserved |
| Licenses | number of configured license pools |
| Outages | how long the node that has been unusable the longest has been out of service |

### Discovery

| Rule | Source | Items per entity | Filter macros |
| --- | --- | --- | --- |
| Partitions | `scontrol show partition`, joined with node and job data | 26 | `{$SLURM.PARTITION.DISCOVERY.MATCHES}` / `.NOT_MATCHES` |
| QOS | `sacctmgr show qos`, joined with job data | 7 | `{$SLURM.QOS.DISCOVERY.MATCHES}` / `.NOT_MATCHES` |
| Licenses | `scontrol show licenses` | 6 | `{$SLURM.LICENSE.DISCOVERY.MATCHES}` / `.NOT_MATCHES` |
| Reservations | `scontrol show reservation` | 8 | `{$SLURM.RESERVATION.DISCOVERY.MATCHES}` / `.NOT_MATCHES` |
| Nodes | `scontrol show node` | 23 | `{$SLURM.NODE.DISCOVERY.MATCHES}` / `.NOT_MATCHES`, `{$SLURM.NODE.PARTITION.MATCHES}` |

Per partition: state, node counts by state, availability, CPUs (total, allocated,
idle, unusable, %), memory, GPUs, jobs running/pending/total, CPUs requested by
pending jobs, oldest pending job.

Queue wait — the oldest and mean pending age, at cluster and partition level —
counts only jobs the scheduler could actually start. Jobs waiting on a
dependency, a hold or begin time, or a reservation window are excluded, since
they would report their whole wait even on a completely idle cluster. They are
still counted in the pending totals and in the pending reason breakdown.

Per node: state and state code, availability, not responding, drain reason and
who set it, how long the node has been out of service, CPUs, CPU allocation,
load average, load per core, memory (total, allocated, free, %), temporary disk,
GPUs, uptime.

Per license: total, used, free, reserved, usage %, whether the pool is remote.

Per reservation: state, active, maintenance flag, nodes, cores, time until it
starts, time remaining, duration.

Per QOS: jobs running/pending/total, allocated CPUs, GrpTRES CPU limit and its
usage, priority.

## Triggers

Every cluster trigger depends on *slurmctld is not responding*, which in turn
depends on *No data collected*, so a controller outage produces one alert rather
than dozens. Node and partition triggers depend on the corresponding
node-down/partition-down trigger for the same reason.

| Trigger | Severity |
| --- | --- |
| Slurm: No data collected for {$SLURM.DATA.TIMEOUT} | Warning |
| Slurm: Collected data is stale | Warning |
| Slurm: Collector reported errors | Warning |
| Slurm: slurmctld is not responding | **High** |
| Slurm: slurmctld backup controller is not responding | Warning |
| Slurm: slurmdbd is not reachable | Average |
| Slurm: DBD agent queue is too large | Average |
| Slurm: Controller agent queue is too large | Average |
| Slurm: Nodes are down | Average |
| Slurm: Less than {$SLURM.NODES.AVAILABILITY.MIN}% of the nodes are usable | Average |
| Slurm: More than {$SLURM.NODES.DRAIN.MAX.PCT}% of the nodes are drained | Warning |
| Slurm: Nodes are not responding | Warning |
| Slurm: Cluster CPU allocation is above {$SLURM.CPU.UTIL.HIGH}% | Info |
| Slurm: Job backlog is above {$SLURM.JOBS.PENDING.MAX} jobs | Warning |
| Slurm: Jobs are waiting longer than {$SLURM.JOBS.PENDING.AGE.MAX} | Warning |
| Slurm: Jobs are blocked by a limit while the cluster has free CPUs | Warning |
| Slurm: Job table usage is above {$SLURM.JOBS.USAGE.HIGH}% | Average |
| Slurm: Main scheduling cycle is slow | Warning |
| Slurm: Backfill scheduling cycle is slow | Warning |
| Slurm: Backfill scheduler has not run for {$SLURM.BACKFILL.AGE.MAX} (with jobs ready to run) | Warning |
| Slurm: A node has been out of service for more than {$SLURM.NODE.UNAVAILABLE.AGE.MAX} | Warning |
| Slurm: Job failure rate is above {$SLURM.ACCOUNTING.FAILURE.RATE.MAX}% | Warning (needs accounting) |
| Slurm: Jobs are being killed by node failures | Average (needs accounting) |
| Slurm: Version has changed | Info |

Discovered entities:

| Trigger prototype | Severity |
| --- | --- |
| Partition [{#PARTITION}]: State is not UP | Warning |
| Partition [{#PARTITION}]: No usable nodes left | **High** |
| Partition [{#PARTITION}]: Nodes are down | Average |
| Partition [{#PARTITION}]: Job backlog is too large | Warning |
| Partition [{#PARTITION}]: Jobs wait longer than expected | Warning |
| Partition [{#PARTITION}]: CPU allocation is saturated | Info |
| QOS [{#QOS}]: CPU limit is nearly exhausted | Info |
| License [{#LICENSE}]: Pool is exhausted and jobs are waiting | Warning |
| License [{#LICENSE}]: Usage is above {$SLURM.LICENSE.USAGE.HIGH}% | Info |
| Node [{#NODE}]: State is DOWN or FAIL | Average |
| Node [{#NODE}]: Not responding | Average |
| Node [{#NODE}]: Drained | Warning |
| Node [{#NODE}]: Draining | Info |
| Node [{#NODE}]: In maintenance | Info |
| Node [{#NODE}]: Load per core is above {$SLURM.NODE.LOAD.MAX} | Warning |
| Node [{#NODE}]: Free memory is below {$SLURM.NODE.MEMORY.FREE.MIN}% | Warning (off by default) |
| Node [{#NODE}]: Out of service for more than {$SLURM.NODE.UNAVAILABLE.AGE.MAX} | Warning |
| Node [{#NODE}]: Has been restarted | Info |

Two of these are worth calling out because they answer questions raw utilisation
graphs cannot:

* **Jobs are blocked by a limit while the cluster has free CPUs** — jobs are held
  back by a QOS, association or partition limit, CPUs are idle, and *no* job is
  waiting for resources. That combination means a limit is holding the queue
  back, not a lack of hardware. Jobs waiting on a dependency, a licence, a
  reservation or a hold are excluded: they are waiting for something
  identifiable and cannot start however much capacity is free.
* **Cluster CPU allocation is above 95%** is deliberately *Info*: a full cluster
  is a healthy cluster. It exists to correlate with queue growth, not to page
  anyone.

One is deliberately **disabled by default**: *Node free memory is below N%*.
Slurm reports `FreeMem`, which does not count reclaimable page cache as free,
so on any node that has been running jobs it falls towards zero during normal
operation. `{$SLURM.NODE.MEMORY.FREE.MIN}` therefore defaults to `0`, and a
percentage is never below zero. To detect genuine memory pressure, monitor the
compute nodes with an operating system template, which reports *available*
memory. Raise the macro above `0` — globally, per host, or per node with
`{$SLURM.NODE.MEMORY.FREE.MIN:"node001"}` — only where `FreeMem` is known to be
meaningful.

## Dashboards

One template dashboard with four pages:

The slideshow is off by default, so a page stays put while you read it.

1. **Cluster** — node availability, CPU and GPU allocation, running and pending
   jobs as value widgets, then nodes by state, jobs by state, CPU allocation,
   pending jobs by reason, memory, GPUs and queue wait time.
2. **Scheduler** — controller and database availability, agent queues, schedule
   queue length and backfill depth, with cycle times, scheduler queues, job
   throughput and queue size in CPUs.
3. **Partitions** — CPU allocation, jobs and node health per discovered partition.
4. **Nodes** — CPU and memory per discovered node.
5. **Licenses and reservations** — license pools, jobs waiting on licenses,
   active reservations and the longest current node outage.
6. **Accounting** — job outcomes, success and failure rate, CPU hours, queue
   wait and runtime. Empty until the accounting item is enabled.

### Viewing it

Template dashboards render in **host** context. Open them through

*Monitoring → Hosts →* click the host *→ Dashboards*

and pick the time range with the selector at the top right. The dashboard as
shown under *Data collection → Templates → … → Dashboards* is the editing view:
it has no host behind it, so every widget there is empty by design.

The *Partitions* and *Nodes* pages are built from graph prototypes and stay
empty until discovery has created the graphs, which also means they stay empty
if node discovery is filtered out or disabled.

## Macros

| Macro | Default | Description |
| --- | --- | --- |
| `{$SLURM.DATA.TIMEOUT}` | `10m` | Age of the collected data after which the cluster is considered unmonitored. |
| `{$SLURM.DATA.STALE.MAX}` | `5m` | Maximum acceptable age of the cached collector document. |
| `{$SLURM.NODES.DOWN.MAX}` | `0` | Nodes in DOWN/FAIL state that are tolerated before alerting. |
| `{$SLURM.NODES.DRAIN.MAX.PCT}` | `10` | Percentage of drained nodes that is tolerated. |
| `{$SLURM.NODES.AVAILABILITY.MIN}` | `90` | Minimum percentage of usable nodes. |
| `{$SLURM.CPU.UTIL.HIGH}` | `95` | Cluster CPU allocation (%) considered saturated. |
| `{$SLURM.CPU.UTIL.TIME}` | `30m` | How long saturation must last before alerting. |
| `{$SLURM.JOBS.PENDING.MAX}` | `1000` | Pending jobs considered a backlog. |
| `{$SLURM.JOBS.PENDING.TIME}` | `30m` | How long the backlog must last before alerting. |
| `{$SLURM.JOBS.PENDING.AGE.MAX}` | `24h` | Maximum queue wait before alerting. |
| `{$SLURM.JOBS.USAGE.HIGH}` | `80` | Percentage of MaxJobCount in use that warns. |
| `{$SLURM.SCHED.AGENT.QUEUE.MAX}` | `200` | slurmctld agent queue size considered a backlog. |
| `{$SLURM.SCHED.DBD.QUEUE.MAX}` | `500` | slurmdbd agent queue size considered a backlog. |
| `{$SLURM.SCHED.CYCLE.MAX}` | `10` | Mean main scheduling cycle (s) considered slow. |
| `{$SLURM.BACKFILL.CYCLE.MAX}` | `30` | Mean backfill cycle (s) considered slow. |
| `{$SLURM.BACKFILL.AGE.MAX}` | `1h` | Maximum age of the last backfill cycle. |
| `{$SLURM.PARTITION.CPU.UTIL.HIGH}` | `95` | Partition CPU allocation (%) considered saturated. |
| `{$SLURM.PARTITION.JOBS.PENDING.MAX}` | `500` | Pending jobs per partition considered a backlog. |
| `{$SLURM.PARTITION.PENDING.AGE.MAX}` | `12h` | Maximum queue wait per partition. |
| `{$SLURM.PARTITION.NODES.DOWN.MAX}` | `0` | Nodes down per partition that are tolerated. |
| `{$SLURM.NODE.LOAD.MAX}` | `1.5` | Load average per core considered an overload. |
| `{$SLURM.NODE.MEMORY.FREE.MIN}` | `0` | Minimum free memory (%) on a node. 0 disables the check (default), see below. |
| `{$SLURM.NODE.UPTIME.MIN}` | `10m` | Uptime below which a node counts as recently rebooted. |
| `{$SLURM.QOS.CPU.USAGE.HIGH}` | `90` | Percentage of the QOS GrpTRES CPU limit that warns. |
| `{$SLURM.NODE.UNAVAILABLE.AGE.MAX}` | `7d` | How long a node may stay out of service before it counts as forgotten capacity. |
| `{$SLURM.LICENSE.USAGE.HIGH}` | `90` | License pool usage (%) that warns. |
| `{$SLURM.ACCOUNTING.FAILURE.RATE.MAX}` | `20` | Percentage of finished jobs that may fail before alerting. |
| `{$SLURM.ACCOUNTING.MIN.JOBS}` | `20` | Minimum finished jobs before the failure rate is judged. |
| `{$SLURM.NODE.DISCOVERY.MATCHES}` | `.*` | Node names to discover. |
| `{$SLURM.NODE.DISCOVERY.NOT_MATCHES}` | `CHANGE_IF_NEEDED` | Node names to exclude. |
| `{$SLURM.NODE.PARTITION.MATCHES}` | `.*` | Discover only nodes in matching partitions. |
| `{$SLURM.PARTITION.DISCOVERY.MATCHES}` | `.*` | Partitions to discover. |
| `{$SLURM.PARTITION.DISCOVERY.NOT_MATCHES}` | `CHANGE_IF_NEEDED` | Partitions to exclude. |
| `{$SLURM.QOS.DISCOVERY.MATCHES}` | `.*` | QOS names to discover. |
| `{$SLURM.QOS.DISCOVERY.NOT_MATCHES}` | `CHANGE_IF_NEEDED` | QOS names to exclude. |
| `{$SLURM.LICENSE.DISCOVERY.MATCHES}` / `.NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Licenses to discover or exclude. |
| `{$SLURM.RESERVATION.DISCOVERY.MATCHES}` / `.NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Reservations to discover or exclude. |

The four per-entity thresholds accept a macro context, so one node or partition
can be treated differently from the rest:

```
{$SLURM.NODE.LOAD.MAX:"gpu001"}                 = 4
{$SLURM.PARTITION.JOBS.PENDING.MAX:"debug"}     = 50
{$SLURM.LICENSE.USAGE.HIGH:"ansys"}             = 75
{$SLURM.NODE.UNAVAILABLE.AGE.MAX:"oldnode01"}   = 90d
```

## Scaling

Node discovery creates 21 items per node. That is fine for a few hundred nodes
and expensive for a few thousand:

| Nodes | Node items | Recommendation |
| --- | --- | --- |
| < 200 | < 4 200 | Defaults are fine. |
| 200–1 000 | 4 200–21 000 | Raise the `slurm.nodes` interval to 5m; consider narrowing discovery to the partitions you care about. |
| > 1 000 | > 21 000 | Restrict discovery with `{$SLURM.NODE.PARTITION.MATCHES}`, or disable the *Slurm: Node discovery* rule and the `slurm.nodes` master item and rely on the cluster and partition level counters, which stay accurate either way. |

Each node item evaluates a JSONPath filter over the node document, so the cost
grows with nodes × items. The cluster and partition metrics do not depend on node
discovery at all.

Also worth knowing:

* `squeue` output grows with the number of active jobs. On clusters with tens of
  thousands of queued jobs, use the timer mode so a slow query never blocks an
  agent check.
* The two master items keep no history (`history=0`); only the dependent items
  are stored.

## Troubleshooting

**`zabbix_get -k slurm.cluster` returns "Unsupported item key"**
The agent has not loaded the UserParameters. On agent 2 this is almost always
the wrong directory: the file has to be in
`/etc/zabbix/zabbix_agent2.d/plugins.d/`, since the packaged configuration
includes `plugins.d/*.conf` only. Confirm which directories are actually read,

```sh
grep -n '^Include' /etc/zabbix/zabbix_agent2.conf   # or zabbix_agentd.conf
```

move `slurm.conf` there, and restart the agent.

**Timeouts, or `ZBX_NOTSUPPORTED: Timeout while executing a shell script`**
Raise `Timeout=30` in the agent configuration, or switch to the timer mode.

**All items work but the cluster counters are zero**
The `zabbix` user cannot talk to `slurmctld`. Test it directly:

```sh
sudo -u zabbix /usr/local/bin/slurm_zabbix.py --mode cluster --pretty --no-cache
```

The `meta.errors` field names the command that failed.

**QOS discovery finds nothing / slurmdbd shows as down**
The cluster has no accounting database, or `sacctmgr` is not usable by the
`zabbix` user. Add `--no-sacctmgr` to the UserParameters and disable the
*slurmdbd is not reachable* trigger.

**Node items show "no data" for some nodes**
Values Slurm does not report (the load average of a down node, for instance) are
omitted from the JSON on purpose, so the matching item keeps its last value
instead of going unsupported.

## Development

```sh
python3 -m unittest discover -s tests -v    # 126 tests, no Slurm required
python3 tools/build_template.py             # regenerate templates/slurm_cluster_7.0.xml
python3 tools/validate_template.py          # check the generated template
make check                                  # all three
```

GitHub Actions runs the tests on Python 3.9, 3.11 and 3.13, validates the
template, fails if the committed XML no longer matches the builder, and runs
ShellCheck over the shell scripts.

The template is generated from the declarative definition in
`tools/build_template.py`; edit that and regenerate rather than editing the XML.
UUIDs are derived deterministically from each object's path, so regenerating
produces a byte-identical file and Zabbix keeps recognising already imported
objects.

`tools/validate_template.py` is the reason the XML can be trusted without a live
Zabbix. It checks:

* UUIDs are unique and **version 4** — Zabbix refuses the whole import with
  "UUIDv4 is expected" otherwise;
* dependent items point at an existing master item, and no item mixes a master
  item with a polling interval;
* trigger expressions reference existing items on this template, their
  dependencies exist, and string functions such as `length()` are applied to a
  value (`length(last(/host/key))`) rather than to an item reference, which
  fails Zabbix's expression validation;
* dashboard widgets carry the fields Zabbix 7.0 expects — indexed object
  references (`itemid.0`, `graphid.0`) and a `reference` unique within the
  dashboard — since the pre-7.0 spelling imports cleanly and renders an empty
  widget;
* graphs, widgets and value maps reference objects that exist, every `{$MACRO}`
  has a default, and widgets stay inside the 72x64 grid without overlapping;
* **every JSONPath resolves against real collector output**, so a path that
  could never match is an error rather than a silently empty item.

The widget and expression conventions were taken from the official Zabbix 7.0
templates rather than from the documentation alone.

The collector is tested against recorded Slurm command output in
`tests/fixtures/`, replayed by the stub commands in `tests/fakebin/`. To add
support for another Slurm release, record its output into a fixture and add the
expectations.

## Notes on the data

* **CPU accounting** follows `sinfo`'s A/I/O/T convention: unallocated CPUs on
  nodes that cannot accept work (down, drained, not responding) are reported as
  *unusable* rather than *idle*, so idle CPUs always mean schedulable capacity.
* **Memory** is reported in bytes. *Allocated* is what Slurm has granted to jobs;
  *free* is what `slurmd` observes on the nodes. They differ when jobs request
  more memory than they use.
* **Drained vs draining**: a drained node runs nothing and is unusable; a draining
  node still runs jobs but accepts no new ones. They are counted separately.
* **A node in several partitions** is counted in each of them, which is why the
  sum of partition node counts can exceed the cluster node count.
* **GPUs** are read from the node's `Gres` and `GresUsed` fields, so they are
  reported whether or not the cluster lists `gres/gpu` in
  `AccountingStorageTRES` — it is not there by default. Typed (`gpu:a100:4`),
  untyped (`gpu:4`) and multi-type nodes all work, and non-GPU resources such as
  `mps` and `shard` are ignored. Note this is *allocation*, not device
  utilisation: it says how many GPUs Slurm has handed to jobs, not how busy the
  silicon is. For the latter, monitor the nodes themselves (DCGM or
  nvidia-smi).

## License

MIT, see [LICENSE](LICENSE).
