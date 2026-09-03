#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the Zabbix 7.0 template for a Slurm cluster.

The template is described here declaratively and rendered to XML.  Two things
make this preferable to hand-editing several thousand lines of XML:

* every element of a Zabbix 6.0+ export needs a UUID, and those are derived
  deterministically from the element's path, so regenerating the template keeps
  the UUIDs (and therefore the link to already-imported templates) stable;
* items, triggers, graphs and dashboard widgets all reference item keys, and
  generating them from one source keeps those references consistent.

Usage:  python3 tools/build_template.py [-o templates/slurm_cluster_7.0.xml]
"""

import argparse
import os
import sys
import uuid as uuidlib
import xml.etree.ElementTree as ET

EXPORT_VERSION = "7.0"
TEMPLATE = "Slurm cluster by Zabbix agent"
TEMPLATE_NAME = "Slurm cluster by Zabbix agent"
TEMPLATE_GROUP = "Templates/Applications"
# "Templates/Applications" is a standard Zabbix group that already exists in
# every installation.  Reusing the UUID Zabbix ships for it makes the import
# map onto that group instead of looking like a different object with the same
# name; every official 7.0 template uses this value.
TEMPLATE_GROUP_UUID = "a571c0d144b14fd4a87a9d9b2aa9fcd6"

# Fixed namespace, chosen once.  Never change it: every UUID in the export is
# derived from it, and changing it would make Zabbix treat the import as a set
# of brand new objects.
UUID_NAMESPACE = uuidlib.UUID("1f9a2c30-6d54-5f8b-9a11-5c0b7f0d21ae")

MASTER_CLUSTER = "slurm.cluster"
MASTER_NODES = "slurm.nodes"
MASTER_ACCOUNTING = "slurm.accounting"

TEMPLATE_DESCRIPTION = """Monitoring of a Slurm workload manager cluster.

The template collects the whole cluster state with two Zabbix agent checks that
run bin/slurm_zabbix.py on a host with the Slurm client commands installed
(usually the controller or a login node).  Every metric is a dependent item of
one of those two master items, so adding metrics costs no extra load on
slurmctld.

Requires: Slurm 20.02 or newer, Python 3.6 or newer, and the UserParameters
shipped in agent/slurm.conf.

Discovery: partitions, QOS and compute nodes.
Repository: https://github.com/Kevin-NZ/Zabbix-Slurm"""

# --- palette ---------------------------------------------------------------
GREEN = "1A7C11"
BLUE = "2774A4"
RED = "F63100"
BROWN = "A54F10"
PINK = "FC6EA3"
PURPLE = "6C59DC"
OLIVE = "AC8C14"
DARKRED = "611F27"
MAGENTA = "F230E0"
LIGHTGREEN = "5CCD18"
ORANGE = "FF9A00"
TEAL = "00B0C0"
LIGHTBLUE = "89ABF8"
SAGE = "7EC28E"
PLUM = "5A2B57"
GREY = "8B8B8B"


def make_uuid(*parts):
    """Derive a stable UUID from an object's path.

    Zabbix rejects an import unless every uuid is a version 4 UUID ("UUIDv4 is
    expected"), but random UUIDs would change on every build and make Zabbix
    treat a re-import as a set of new objects.  The value is therefore derived
    from a SHA-1 hash as usual, and only the six bits that carry the version
    and the variant are overwritten to make it well formed.  That leaves 122
    bits of hash, so distinct paths still get distinct UUIDs.
    """
    number = uuidlib.uuid5(UUID_NAMESPACE, "/".join(parts)).int
    number &= ~(0xF << 76)   # clear the version nibble
    number |= 0x4 << 76      # version 4
    number &= ~(0x3 << 62)   # clear the variant bits
    number |= 0x2 << 62      # RFC 4122 variant (10xx)
    return uuidlib.UUID(int=number).hex


# ---------------------------------------------------------------------------
# Value maps
# ---------------------------------------------------------------------------

VALUE_MAPS = [
    ("Slurm service state", [("0", "Down"), ("1", "Up")]),
    ("Slurm node availability", [("0", "Unavailable"), ("1", "Available")]),
    ("Slurm yes/no", [("0", "No"), ("1", "Yes")]),
    ("Slurm data source", [("0", "Freshly collected"), ("1", "From cache")]),
    ("Slurm node state", [
        ("0", "Unknown"),
        ("1", "Idle"),
        ("2", "Allocated"),
        ("3", "Mixed"),
        ("4", "Down"),
        ("5", "Drained"),
        ("6", "Draining"),
        ("7", "Fail"),
        ("8", "Maintenance"),
        ("9", "Reserved"),
        ("10", "Powered down"),
        ("11", "Future"),
        ("12", "Completing"),
        ("13", "Planned"),
        ("14", "Reboot"),
    ]),
    ("Slurm partition state", [
        ("0", "Unknown"),
        ("1", "Up"),
        ("2", "Down"),
        ("3", "Drain"),
        ("4", "Inactive"),
    ]),
]

# ---------------------------------------------------------------------------
# User macros
# ---------------------------------------------------------------------------

MACROS = [
    ("{$SLURM.DATA.TIMEOUT}", "10m",
     "Age of the collected data after which the cluster is considered unmonitored."),
    ("{$SLURM.DATA.STALE.MAX}", "5m",
     "Maximum acceptable age of the cached collector document."),
    ("{$SLURM.NODES.DOWN.MAX}", "0",
     "Number of nodes in DOWN/FAIL state that is tolerated before alerting."),
    ("{$SLURM.NODES.DRAIN.MAX.PCT}", "10",
     "Percentage of drained nodes that is tolerated before alerting."),
    ("{$SLURM.NODES.AVAILABILITY.MIN}", "90",
     "Minimum percentage of usable nodes in the cluster."),
    ("{$SLURM.CPU.UTIL.HIGH}", "95",
     "Cluster CPU allocation (%) considered saturated."),
    ("{$SLURM.CPU.UTIL.TIME}", "30m",
     "How long the cluster has to stay saturated before alerting."),
    ("{$SLURM.JOBS.PENDING.MAX}", "1000",
     "Number of pending jobs considered a backlog."),
    ("{$SLURM.JOBS.PENDING.TIME}", "30m",
     "How long the pending backlog has to last before alerting."),
    ("{$SLURM.JOBS.PENDING.AGE.MAX}", "24h",
     "Maximum time a job may wait in the queue before alerting."),
    ("{$SLURM.JOBS.USAGE.HIGH}", "80",
     "Percentage of MaxJobCount in use that triggers a warning."),
    ("{$SLURM.SCHED.AGENT.QUEUE.MAX}", "200",
     "slurmctld agent queue size considered a backlog."),
    ("{$SLURM.SCHED.DBD.QUEUE.MAX}", "500",
     "slurmdbd agent queue size considered a backlog; a growing queue means "
     "accounting records are not reaching slurmdbd."),
    ("{$SLURM.SCHED.CYCLE.MAX}", "10",
     "Mean main scheduling cycle time (seconds) considered too slow."),
    ("{$SLURM.BACKFILL.CYCLE.MAX}", "30",
     "Mean backfill cycle time (seconds) considered too slow."),
    ("{$SLURM.BACKFILL.AGE.MAX}", "1h",
     "Maximum age of the last backfill cycle."),
    ("{$SLURM.PARTITION.CPU.UTIL.HIGH}", "95",
     "Partition CPU allocation (%) considered saturated. Usable with a "
     "partition name as macro context."),
    ("{$SLURM.PARTITION.JOBS.PENDING.MAX}", "500",
     "Pending jobs per partition considered a backlog. Usable with a partition "
     "name as macro context."),
    ("{$SLURM.PARTITION.PENDING.AGE.MAX}", "12h",
     "Maximum queue wait per partition. Usable with a partition name as macro "
     "context."),
    ("{$SLURM.PARTITION.NODES.DOWN.MAX}", "0",
     "Nodes down per partition that are tolerated. Usable with a partition "
     "name as macro context."),
    ("{$SLURM.NODE.LOAD.MAX}", "1.5",
     "Load average per allocated core considered an overload. Usable with a "
     "node name as macro context."),
    ("{$SLURM.NODE.MEMORY.FREE.MIN}", "0",
     "Minimum free memory (%) on a node before alerting. 0 disables the check, "
     "which is the default: Slurm reports FreeMem, which does not count "
     "reclaimable page cache as free, so on a healthy busy node it drops "
     "towards zero on its own. Raise it only on clusters where the value is "
     "known to track real memory pressure. Usable with a node name as macro "
     "context."),
    ("{$SLURM.NODE.UPTIME.MIN}", "10m",
     "Uptime below which a node is reported as recently rebooted."),
    ("{$SLURM.NODE.UNAVAILABLE.AGE.MAX}", "7d",
     "How long a node may stay drained or down before it is reported as "
     "forgotten capacity. Usable with a node name as macro context."),
    ("{$SLURM.LICENSE.USAGE.HIGH}", "90",
     "License pool usage (%) that triggers a warning. Usable with a license "
     "name as macro context."),
    ("{$SLURM.ACCOUNTING.FAILURE.RATE.MAX}", "20",
     "Percentage of finished jobs that may fail before alerting. Requires the "
     "accounting collection, which is disabled by default."),
    ("{$SLURM.ACCOUNTING.MIN.JOBS}", "20",
     "Minimum number of finished jobs before the failure rate is judged, so "
     "that a quiet cluster does not alert on a couple of failures."),
    ("{$SLURM.QOS.CPU.USAGE.HIGH}", "90",
     "Percentage of the QOS GrpTRES CPU limit in use that triggers a warning."),
    ("{$SLURM.NODE.DISCOVERY.MATCHES}", ".*",
     "Node names to discover."),
    ("{$SLURM.NODE.DISCOVERY.NOT_MATCHES}", "CHANGE_IF_NEEDED",
     "Node names to exclude from discovery."),
    ("{$SLURM.NODE.PARTITION.MATCHES}", ".*",
     "Discover only nodes belonging to a partition matching this expression."),
    ("{$SLURM.PARTITION.DISCOVERY.MATCHES}", ".*",
     "Partitions to discover."),
    ("{$SLURM.PARTITION.DISCOVERY.NOT_MATCHES}", "CHANGE_IF_NEEDED",
     "Partitions to exclude from discovery."),
    ("{$SLURM.QOS.DISCOVERY.MATCHES}", ".*",
     "QOS names to discover."),
    ("{$SLURM.QOS.DISCOVERY.NOT_MATCHES}", "CHANGE_IF_NEEDED",
     "QOS names to exclude from discovery."),
    ("{$SLURM.LICENSE.DISCOVERY.MATCHES}", ".*",
     "License names to discover."),
    ("{$SLURM.LICENSE.DISCOVERY.NOT_MATCHES}", "CHANGE_IF_NEEDED",
     "License names to exclude from discovery."),
    ("{$SLURM.RESERVATION.DISCOVERY.MATCHES}", ".*",
     "Reservation names to discover."),
    ("{$SLURM.RESERVATION.DISCOVERY.NOT_MATCHES}", "CHANGE_IF_NEEDED",
     "Reservation names to exclude from discovery."),
]

# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def item(key, name, path, value_type="UNSIGNED", units="", component="cluster",
         valuemap=None, master=MASTER_CLUSTER, description="", heartbeat=None,
         rate=False, history="7d", trends="365d"):
    """Definition of a dependent item derived from a master item."""
    return {
        "key": key,
        "name": name,
        "path": path,
        "value_type": value_type,
        "units": units,
        "component": component,
        "valuemap": valuemap,
        "master": master,
        "description": description,
        "heartbeat": heartbeat,
        "rate": rate,
        "history": history,
        "trends": trends,
    }


def text_item(key, name, path, component="cluster", master=MASTER_CLUSTER,
              description="", heartbeat="1d"):
    return item(key, name, path, value_type="CHAR", component=component, master=master,
                description=description, heartbeat=heartbeat, trends="0")


def pct_item(key, name, path, component, master=MASTER_CLUSTER, description=""):
    return item(key, name, path, value_type="FLOAT", units="%", component=component,
                master=master, description=description)


MASTER_ITEMS = [
    {
        "key": MASTER_CLUSTER,
        "name": "Slurm: Get cluster data",
        "delay": "1m",
        "description": (
            "Cluster, partition, QOS, job and scheduler state as a single JSON "
            "document, collected by bin/slurm_zabbix.py --mode cluster.\n"
            "All cluster level items depend on this item."),
    },
    {
        "key": MASTER_NODES,
        "name": "Slurm: Get node data",
        "delay": "2m",
        "description": (
            "Per node metrics as a single JSON document, collected by "
            "bin/slurm_zabbix.py --mode nodes.\n"
            "Node discovery and all node items depend on this item.\n"
            "Disable this item on very large clusters if per node monitoring is "
            "not wanted."),
    },
    {
        "key": MASTER_ACCOUNTING,
        "name": "Slurm: Get accounting data",
        "delay": "15m",
        "status": "DISABLED",
        "description": (
            "Throughput of the jobs that finished recently, from sacct, "
            "collected by bin/slurm_zabbix.py --mode accounting.\n\n"
            "Disabled by default because it is the only collection that queries "
            "the accounting database, which is expensive on a busy cluster. To "
            "enable it:\n"
            "1. add the slurm.accounting UserParameter on the agent host;\n"
            "2. enable this item.\n"
            "Every accounting metric depends on this item, so enabling it here "
            "switches the whole feature on."),
    },
]

ACCOUNTING_ITEMS = [
    item("slurm.accounting.window", "Slurm: Accounting window", "$.accounting.window",
         units="s", component="accounting", master=MASTER_ACCOUNTING, heartbeat="1d",
         description="Period sacct was asked about. Every job count below covers "
                     "this window."),
    item("slurm.accounting.jobs.total", "Slurm: Jobs finished", "$.accounting.jobs_total",
         component="accounting", master=MASTER_ACCOUNTING),
    item("slurm.accounting.jobs.completed", "Slurm: Jobs completed",
         "$.accounting.jobs_completed", component="accounting", master=MASTER_ACCOUNTING),
    item("slurm.accounting.jobs.failed", "Slurm: Jobs failed", "$.accounting.jobs_failed",
         component="accounting", master=MASTER_ACCOUNTING),
    item("slurm.accounting.jobs.cancelled", "Slurm: Jobs cancelled",
         "$.accounting.jobs_cancelled", component="accounting", master=MASTER_ACCOUNTING,
         description="Jobs cancelled by a user or an administrator. Not counted as "
                     "failures."),
    item("slurm.accounting.jobs.timeout", "Slurm: Jobs killed by the time limit",
         "$.accounting.jobs_timeout", component="accounting", master=MASTER_ACCOUNTING),
    item("slurm.accounting.jobs.node_fail", "Slurm: Jobs killed by a node failure",
         "$.accounting.jobs_node_fail", component="accounting", master=MASTER_ACCOUNTING,
         description="Jobs lost because a node failed under them. A persistent count "
                     "here points at unhealthy hardware."),
    item("slurm.accounting.jobs.out_of_memory", "Slurm: Jobs killed out of memory",
         "$.accounting.jobs_out_of_memory", component="accounting",
         master=MASTER_ACCOUNTING),
    item("slurm.accounting.jobs.preempted", "Slurm: Jobs preempted",
         "$.accounting.jobs_preempted", component="accounting", master=MASTER_ACCOUNTING),
    item("slurm.accounting.jobs.other", "Slurm: Jobs with another ending",
         "$.accounting.jobs_other", component="accounting", master=MASTER_ACCOUNTING),
    pct_item("slurm.accounting.success_rate", "Slurm: Job success rate",
             "$.accounting.success_rate", "accounting", master=MASTER_ACCOUNTING,
             description="Completed jobs as a percentage of all jobs that finished in "
                         "the window."),
    pct_item("slurm.accounting.failure_rate", "Slurm: Job failure rate",
             "$.accounting.failure_rate", "accounting", master=MASTER_ACCOUNTING,
             description="Jobs that failed, timed out, hit a node failure or ran out of "
                         "memory, as a percentage of all jobs that finished. User "
                         "cancellations are excluded."),
    item("slurm.accounting.wait.mean", "Slurm: Mean queue wait", "$.accounting.wait_mean",
         units="s", component="accounting", master=MASTER_ACCOUNTING,
         description="Mean time between submission and start, for the jobs that "
                     "finished in the window."),
    item("slurm.accounting.wait.max", "Slurm: Longest queue wait", "$.accounting.wait_max",
         units="s", component="accounting", master=MASTER_ACCOUNTING),
    item("slurm.accounting.elapsed.mean", "Slurm: Mean job runtime",
         "$.accounting.elapsed_mean", units="s", component="accounting",
         master=MASTER_ACCOUNTING),
    item("slurm.accounting.elapsed.max", "Slurm: Longest job runtime",
         "$.accounting.elapsed_max", units="s", component="accounting",
         master=MASTER_ACCOUNTING),
    item("slurm.accounting.cpu_hours", "Slurm: CPU hours delivered",
         "$.accounting.cpu_hours", value_type="FLOAT", units="h", component="accounting",
         master=MASTER_ACCOUNTING,
         description="CPU hours consumed by the jobs that finished in the window."),
]

CLUSTER_ITEMS = [
    # -- collector health ---------------------------------------------------
    text_item("slurm.cluster.name", "Slurm: Cluster name", "$.cluster.name", "health",
              description="ClusterName as configured in slurm.conf."),
    text_item("slurm.cluster.version", "Slurm: Version", "$.cluster.version", "health",
              description="Slurm version reported by the controller."),
    text_item("slurm.cluster.controller", "Slurm: Controller host", "$.cluster.controller",
              "health"),
    text_item("slurm.cluster.scheduler_type", "Slurm: Scheduler type",
              "$.cluster.scheduler_type", "health"),
    text_item("slurm.cluster.select_type", "Slurm: Select type",
              "$.cluster.select_type", "health"),
    text_item("slurm.collector.version", "Slurm: Collector version", "$.meta.version",
              "health"),
    text_item("slurm.collector.errors", "Slurm: Collector errors", "$.meta.errors", "health",
              heartbeat=None,
              description="Errors reported by the collector while querying Slurm. "
                          "Empty when everything was collected successfully."),
    item("slurm.collector.error_count", "Slurm: Collector error count", "$.meta.error_count",
         component="health"),
    item("slurm.data.age", "Slurm: Data age", "$.meta.age", units="s", component="health",
         description="Seconds between the moment the data was collected and the moment "
                     "Zabbix received it. Grows when the collector cache is not being "
                     "refreshed."),
    item("slurm.data.duration", "Slurm: Collection duration", "$.meta.duration",
         value_type="FLOAT", units="s", component="health",
         description="Time the collector needed to query Slurm."),
    item("slurm.data.cached", "Slurm: Data served from cache", "$.meta.cached",
         component="health", valuemap="Slurm data source"),

    # -- controller availability -------------------------------------------
    item("slurm.ctld.available", "Slurm: slurmctld availability",
         "$.cluster.slurmctld_up", component="health", valuemap="Slurm service state",
         description="Result of scontrol ping for the primary controller."),
    item("slurm.ctld.backup.available", "Slurm: slurmctld backup availability",
         "$.cluster.slurmctld_backup_up", component="health",
         valuemap="Slurm service state",
         description="Result of scontrol ping for the backup controller. Stays 0 on "
                     "clusters without a backup controller."),
    item("slurm.dbd.available", "Slurm: slurmdbd availability", "$.cluster.slurmdbd_up",
         component="health", valuemap="Slurm service state",
         description="Set to 1 when sacctmgr can reach the accounting database."),

    # -- inventory ----------------------------------------------------------
    item("slurm.partitions.total", "Slurm: Partitions total", "$.cluster.partitions_total",
         component="partitions", heartbeat="1d"),
    item("slurm.qos.total", "Slurm: QOS total", "$.cluster.qos_total", component="qos",
         heartbeat="1d"),
    item("slurm.reservations.total", "Slurm: Reservations total",
         "$.cluster.reservations_total", component="cluster"),
    item("slurm.reservations.active", "Slurm: Reservations active",
         "$.cluster.reservations_active", component="cluster"),
    item("slurm.reservations.nodes", "Slurm: Nodes in active reservations",
         "$.cluster.reservations_nodes", component="cluster"),
    item("slurm.licenses.total", "Slurm: Licenses configured", "$.cluster.licenses_total",
         component="licenses", heartbeat="1d"),

    # -- node states --------------------------------------------------------
    item("slurm.nodes.total", "Slurm: Nodes total", "$.nodes_summary.total",
         component="nodes", heartbeat="1d"),
    item("slurm.nodes.idle", "Slurm: Nodes idle", "$.nodes_summary.idle", component="nodes"),
    item("slurm.nodes.allocated", "Slurm: Nodes allocated", "$.nodes_summary.allocated",
         component="nodes"),
    item("slurm.nodes.mixed", "Slurm: Nodes mixed", "$.nodes_summary.mixed",
         component="nodes"),
    item("slurm.nodes.down", "Slurm: Nodes down", "$.nodes_summary.down", component="nodes"),
    item("slurm.nodes.fail", "Slurm: Nodes failed", "$.nodes_summary.fail", component="nodes"),
    item("slurm.nodes.drained", "Slurm: Nodes drained", "$.nodes_summary.drained",
         component="nodes",
         description="Nodes that are drained and no longer running jobs."),
    item("slurm.nodes.draining", "Slurm: Nodes draining", "$.nodes_summary.draining",
         component="nodes",
         description="Nodes that are marked for draining but still run jobs."),
    item("slurm.nodes.drain", "Slurm: Nodes in drain", "$.nodes_summary.drain",
         component="nodes", description="Drained and draining nodes together."),
    item("slurm.nodes.maint", "Slurm: Nodes in maintenance", "$.nodes_summary.maint",
         component="nodes"),
    item("slurm.nodes.reserved", "Slurm: Nodes reserved", "$.nodes_summary.reserved",
         component="nodes"),
    item("slurm.nodes.completing", "Slurm: Nodes completing", "$.nodes_summary.completing",
         component="nodes"),
    item("slurm.nodes.planned", "Slurm: Nodes planned", "$.nodes_summary.planned",
         component="nodes",
         description="Nodes reserved by the backfill scheduler for a future job."),
    item("slurm.nodes.powered_down", "Slurm: Nodes powered down",
         "$.nodes_summary.powered_down", component="nodes"),
    item("slurm.nodes.reboot", "Slurm: Nodes pending reboot", "$.nodes_summary.reboot",
         component="nodes"),
    item("slurm.nodes.future", "Slurm: Nodes in future state", "$.nodes_summary.future",
         component="nodes"),
    item("slurm.nodes.unknown", "Slurm: Nodes in unknown state", "$.nodes_summary.unknown",
         component="nodes"),
    item("slurm.nodes.not_responding", "Slurm: Nodes not responding",
         "$.nodes_summary.not_responding", component="nodes"),
    item("slurm.nodes.available", "Slurm: Nodes available", "$.nodes_summary.available",
         component="nodes",
         description="Nodes that can accept work: idle, allocated, mixed, reserved, "
                     "completing or planned, and responding."),
    item("slurm.nodes.unavailable", "Slurm: Nodes unavailable",
         "$.nodes_summary.unavailable", component="nodes"),
    pct_item("slurm.nodes.availability", "Slurm: Nodes availability",
             "$.nodes_summary.availability", "nodes"),
    item("slurm.nodes.longest_unavailable_age", "Slurm: Longest node outage",
         "$.nodes_summary.longest_unavailable_age", units="s", component="nodes",
         description="How long the node that has been out of service the longest has "
                     "been unusable, taken from the timestamp Slurm records with the "
                     "drain or down reason. Reports 0 when every node is usable."),

    # -- CPU ----------------------------------------------------------------
    item("slurm.cpus.total", "Slurm: CPUs total", "$.cpus.total", component="cpu",
         heartbeat="1d"),
    item("slurm.cpus.allocated", "Slurm: CPUs allocated", "$.cpus.allocated",
         component="cpu"),
    item("slurm.cpus.idle", "Slurm: CPUs idle", "$.cpus.idle", component="cpu",
         description="Unallocated CPUs on usable nodes."),
    item("slurm.cpus.other", "Slurm: CPUs unusable", "$.cpus.other", component="cpu",
         description="Unallocated CPUs on nodes that cannot accept work "
                     "(down, drained, not responding)."),
    pct_item("slurm.cpus.utilization", "Slurm: CPU allocation", "$.cpus.utilization", "cpu",
             description="Allocated CPUs as a percentage of all configured CPUs."),

    # -- memory -------------------------------------------------------------
    item("slurm.memory.total", "Slurm: Memory total", "$.memory.total_bytes", units="B",
         component="memory", heartbeat="1d"),
    item("slurm.memory.allocated", "Slurm: Memory allocated", "$.memory.allocated_bytes",
         units="B", component="memory",
         description="Memory allocated to jobs, as accounted by Slurm."),
    item("slurm.memory.free", "Slurm: Memory free", "$.memory.free_bytes", units="B",
         component="memory",
         description="Memory actually free on the nodes, as reported by slurmd."),
    pct_item("slurm.memory.utilization", "Slurm: Memory allocation",
             "$.memory.utilization", "memory"),

    # -- GPU ----------------------------------------------------------------
    item("slurm.gpus.total", "Slurm: GPUs total", "$.gpus.total", component="gpu",
         heartbeat="1d", description="Configured gres/gpu across all nodes."),
    item("slurm.gpus.allocated", "Slurm: GPUs allocated", "$.gpus.allocated",
         component="gpu"),
    item("slurm.gpus.idle", "Slurm: GPUs idle", "$.gpus.idle", component="gpu"),
    pct_item("slurm.gpus.utilization", "Slurm: GPU allocation", "$.gpus.utilization", "gpu"),

    # -- jobs ---------------------------------------------------------------
    item("slurm.jobs.total", "Slurm: Jobs total", "$.jobs.total", component="jobs"),
    item("slurm.jobs.running", "Slurm: Jobs running", "$.jobs.running", component="jobs"),
    item("slurm.jobs.pending", "Slurm: Jobs pending", "$.jobs.pending", component="jobs"),
    item("slurm.jobs.suspended", "Slurm: Jobs suspended", "$.jobs.suspended",
         component="jobs"),
    item("slurm.jobs.completing", "Slurm: Jobs completing", "$.jobs.completing",
         component="jobs"),
    item("slurm.jobs.configuring", "Slurm: Jobs configuring", "$.jobs.configuring",
         component="jobs"),
    item("slurm.jobs.other", "Slurm: Jobs in other states", "$.jobs.other", component="jobs"),
    item("slurm.jobs.cpus_running", "Slurm: CPUs requested by running jobs",
         "$.jobs.cpus_running", component="jobs"),
    item("slurm.jobs.cpus_pending", "Slurm: CPUs requested by pending jobs",
         "$.jobs.cpus_pending", component="jobs",
         description="Size of the queue expressed in CPUs. Compare with the number of "
                     "idle CPUs to judge whether the cluster is short of capacity."),
    item("slurm.jobs.nodes_running", "Slurm: Nodes used by running jobs",
         "$.jobs.nodes_running", component="jobs"),
    item("slurm.jobs.users_active", "Slurm: Active users", "$.jobs.users_active",
         component="jobs"),
    item("slurm.jobs.accounts_active", "Slurm: Active accounts", "$.jobs.accounts_active",
         component="jobs"),
    item("slurm.jobs.oldest_pending_age", "Slurm: Oldest pending job age",
         "$.jobs.oldest_pending_age", units="s", component="jobs",
         description="Time the longest waiting job has spent in the queue.\n\n"
                     "Counts only jobs the scheduler could start: jobs waiting on a "
                     "dependency, on a hold or begin time, or on a reservation window are "
                     "excluded, since they would report their whole wait even on a "
                     "completely idle cluster."),
    item("slurm.jobs.mean_pending_age", "Slurm: Mean pending job age",
         "$.jobs.mean_pending_age", units="s", component="jobs",
         description="Mean queue wait, over the same schedulable jobs as the oldest "
                     "pending job age."),
    item("slurm.jobs.longest_running_age", "Slurm: Longest running job",
         "$.jobs.longest_running_age", units="s", component="jobs"),
    item("slurm.jobs.array_pending", "Slurm: Pending array jobs", "$.jobs.array_pending",
         component="jobs"),
    item("slurm.jobs.max", "Slurm: MaxJobCount", "$.jobs.max", component="jobs",
         heartbeat="1d",
         description="MaxJobCount from slurm.conf. Once reached, slurmctld refuses new "
                     "submissions."),
    pct_item("slurm.jobs.usage", "Slurm: Job table usage", "$.jobs.usage", "jobs",
             description="Active jobs as a percentage of MaxJobCount."),
    text_item("slurm.jobs.pending.top_reasons", "Slurm: Top pending reasons",
              "$.jobs.top_pending_reasons", "jobs", heartbeat=None,
              description="Five most frequent pending reasons with their job counts."),

    # -- pending reasons ----------------------------------------------------
    item("slurm.jobs.pending.resources", "Slurm: Pending jobs - resources",
         "$.jobs.pending_resources", component="jobs",
         description="Jobs waiting for free resources. This is the healthy reason for a "
                     "queue to exist."),
    item("slurm.jobs.pending.priority", "Slurm: Pending jobs - priority",
         "$.jobs.pending_priority", component="jobs",
         description="Jobs held back by higher priority jobs."),
    item("slurm.jobs.pending.dependency", "Slurm: Pending jobs - dependency",
         "$.jobs.pending_dependency", component="jobs"),
    item("slurm.jobs.pending.qos_limit", "Slurm: Pending jobs - QOS limit",
         "$.jobs.pending_qos_limit", component="jobs"),
    item("slurm.jobs.pending.association_limit", "Slurm: Pending jobs - association limit",
         "$.jobs.pending_association_limit", component="jobs"),
    item("slurm.jobs.pending.licenses", "Slurm: Pending jobs - licenses",
         "$.jobs.pending_licenses", component="jobs"),
    item("slurm.jobs.pending.reservation", "Slurm: Pending jobs - reservation",
         "$.jobs.pending_reservation", component="jobs"),
    item("slurm.jobs.pending.partition", "Slurm: Pending jobs - partition",
         "$.jobs.pending_partition", component="jobs",
         description="Jobs blocked because their partition is down, inactive or too "
                     "small for the request."),
    item("slurm.jobs.pending.node_unavailable", "Slurm: Pending jobs - nodes unavailable",
         "$.jobs.pending_node_unavailable", component="jobs"),
    item("slurm.jobs.pending.held", "Slurm: Pending jobs - held",
         "$.jobs.pending_held", component="jobs",
         description="Jobs held by an administrator or by the user, waiting for a start "
                     "time, or limited by an array task limit."),
    item("slurm.jobs.pending.other", "Slurm: Pending jobs - other reasons",
         "$.jobs.pending_other", component="jobs"),
    item("slurm.jobs.pending.schedulable", "Slurm: Pending jobs - ready to run",
         "$.jobs.pending_schedulable", component="jobs",
         description="Pending jobs the scheduler can actually consider starting, that is "
                     "everything except jobs waiting on a dependency, on a hold or begin "
                     "time, or on a reservation window.\n\n"
                     "This is the number that says whether there is work for the "
                     "scheduler to do: a queue made up entirely of dependencies gives the "
                     "backfill scheduler nothing to backfill."),
    item("slurm.jobs.pending.limited", "Slurm: Pending jobs - blocked by a limit",
         "$.jobs.pending_limited", component="jobs",
         description="Pending jobs held back by a QOS, association or partition limit, or "
                     "by a reason the collector does not recognise.\n\n"
                     "Jobs waiting for resources, priority, a dependency, a license, a "
                     "reservation or a hold are deliberately excluded: they are waiting "
                     "for something identifiable and cannot start whatever the cluster "
                     "looks like."),

    # -- scheduler ----------------------------------------------------------
    item("slurm.sched.server_thread_count", "Slurm: Controller thread count",
         "$.scheduler.server_thread_count", component="scheduler",
         description="Threads busy in slurmctld. A permanently high value means the "
                     "controller is struggling with RPC load."),
    item("slurm.sched.agent_queue_size", "Slurm: Agent queue size",
         "$.scheduler.agent_queue_size", component="scheduler",
         description="Outgoing RPCs queued by slurmctld towards the nodes."),
    item("slurm.sched.agent_count", "Slurm: Agent count", "$.scheduler.agent_count",
         component="scheduler"),
    item("slurm.sched.agent_thread_count", "Slurm: Agent thread count",
         "$.scheduler.agent_thread_count", component="scheduler"),
    item("slurm.sched.dbd_agent_queue_size", "Slurm: DBD agent queue size",
         "$.scheduler.dbd_agent_queue_size", component="scheduler",
         description="Accounting records buffered by slurmctld because slurmdbd is slow "
                     "or unreachable. A steadily growing queue ends in lost accounting "
                     "data."),
    item("slurm.sched.jobs_submitted.rate", "Slurm: Job submission rate",
         "$.scheduler.jobs_submitted", value_type="FLOAT", units="jobs/s",
         component="scheduler", rate=True),
    item("slurm.sched.jobs_started.rate", "Slurm: Job start rate",
         "$.scheduler.jobs_started", value_type="FLOAT", units="jobs/s",
         component="scheduler", rate=True),
    item("slurm.sched.jobs_completed.rate", "Slurm: Job completion rate",
         "$.scheduler.jobs_completed", value_type="FLOAT", units="jobs/s",
         component="scheduler", rate=True),
    item("slurm.sched.jobs_canceled.rate", "Slurm: Job cancellation rate",
         "$.scheduler.jobs_canceled", value_type="FLOAT", units="jobs/s",
         component="scheduler", rate=True),
    item("slurm.sched.jobs_failed.rate", "Slurm: Job failure rate",
         "$.scheduler.jobs_failed", value_type="FLOAT", units="jobs/s",
         component="scheduler", rate=True),
    item("slurm.sched.cycle.last", "Slurm: Main schedule cycle, last",
         "$.scheduler.schedule_cycle_last", value_type="FLOAT", units="s",
         component="scheduler"),
    item("slurm.sched.cycle.mean", "Slurm: Main schedule cycle, mean",
         "$.scheduler.schedule_cycle_mean", value_type="FLOAT", units="s",
         component="scheduler"),
    item("slurm.sched.cycle.max", "Slurm: Main schedule cycle, max",
         "$.scheduler.schedule_cycle_max", value_type="FLOAT", units="s",
         component="scheduler"),
    item("slurm.sched.cycles_per_minute", "Slurm: Main schedule cycles per minute",
         "$.scheduler.schedule_cycles_per_minute", component="scheduler"),
    item("slurm.sched.queue_length", "Slurm: Main schedule queue length",
         "$.scheduler.schedule_queue_length", component="scheduler"),
    item("slurm.sched.depth_mean", "Slurm: Main schedule depth, mean",
         "$.scheduler.schedule_depth_mean", component="scheduler",
         description="Average number of jobs the main scheduler looks at per cycle. "
                     "Capped by default_queue_depth."),
    item("slurm.backfill.cycle.last", "Slurm: Backfill cycle, last",
         "$.scheduler.backfill_cycle_last", value_type="FLOAT", units="s",
         component="scheduler"),
    item("slurm.backfill.cycle.mean", "Slurm: Backfill cycle, mean",
         "$.scheduler.backfill_cycle_mean", value_type="FLOAT", units="s",
         component="scheduler"),
    item("slurm.backfill.cycle.max", "Slurm: Backfill cycle, max",
         "$.scheduler.backfill_cycle_max", value_type="FLOAT", units="s",
         component="scheduler"),
    item("slurm.backfill.depth_mean", "Slurm: Backfill depth, mean",
         "$.scheduler.backfill_depth_mean", component="scheduler"),
    item("slurm.backfill.last_depth", "Slurm: Backfill depth, last cycle",
         "$.scheduler.backfill_last_depth", component="scheduler",
         description="Jobs considered during the last backfill cycle. When it stays at "
                     "bf_max_job_test the backfill scheduler cannot see the whole queue."),
    item("slurm.backfill.queue_length", "Slurm: Backfill queue length",
         "$.scheduler.backfill_queue_length", component="scheduler"),
    item("slurm.backfill.jobs.rate", "Slurm: Backfilled jobs rate",
         "$.scheduler.backfilled_jobs", value_type="FLOAT", units="jobs/s",
         component="scheduler", rate=True),
    item("slurm.backfill.last_cycle_age", "Slurm: Time since last backfill cycle",
         "$.scheduler.backfill_last_cycle_age", units="s", component="scheduler"),
]

# ---------------------------------------------------------------------------
# Discovery rules
# ---------------------------------------------------------------------------


def lld_item(key_template, name_template, field, value_type="UNSIGNED", units="",
             component="", valuemap=None, master=MASTER_CLUSTER, description="",
             heartbeat=None, history="7d", trends="365d", selector=None, macro=None):
    """Definition of an item prototype selecting one field of one array entry."""
    return {
        "key": key_template,
        "name": name_template,
        "field": field,
        "value_type": value_type,
        "units": units,
        "component": component,
        "valuemap": valuemap,
        "master": master,
        "description": description,
        "heartbeat": heartbeat,
        "history": history,
        "trends": trends,
        "selector": selector,
        "macro": macro,
    }


PARTITION_ITEMS = [
    lld_item("slurm.partition.state[{#PARTITION}]", "Partition [{#PARTITION}]: State",
             "state", value_type="CHAR", heartbeat="1d", trends="0"),
    lld_item("slurm.partition.state.code[{#PARTITION}]",
             "Partition [{#PARTITION}]: State code", "state_code",
             valuemap="Slurm partition state",
             description="UP, DOWN, DRAIN or INACTIVE as a number, for triggers."),
    lld_item("slurm.partition.nodes.total[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes total", "nodes_total", heartbeat="1d"),
    lld_item("slurm.partition.nodes.idle[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes idle", "nodes_idle"),
    lld_item("slurm.partition.nodes.allocated[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes allocated", "nodes_allocated"),
    lld_item("slurm.partition.nodes.mixed[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes mixed", "nodes_mixed"),
    lld_item("slurm.partition.nodes.down[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes down", "nodes_down"),
    lld_item("slurm.partition.nodes.drain[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes in drain", "nodes_drain"),
    lld_item("slurm.partition.nodes.available[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes available", "nodes_available"),
    lld_item("slurm.partition.nodes.availability[{#PARTITION}]",
             "Partition [{#PARTITION}]: Nodes availability", "nodes_availability",
             value_type="FLOAT", units="%"),
    lld_item("slurm.partition.cpus.total[{#PARTITION}]",
             "Partition [{#PARTITION}]: CPUs total", "cpus_total", heartbeat="1d"),
    lld_item("slurm.partition.cpus.allocated[{#PARTITION}]",
             "Partition [{#PARTITION}]: CPUs allocated", "cpus_allocated"),
    lld_item("slurm.partition.cpus.idle[{#PARTITION}]",
             "Partition [{#PARTITION}]: CPUs idle", "cpus_idle"),
    lld_item("slurm.partition.cpus.other[{#PARTITION}]",
             "Partition [{#PARTITION}]: CPUs unusable", "cpus_other"),
    lld_item("slurm.partition.cpu.utilization[{#PARTITION}]",
             "Partition [{#PARTITION}]: CPU allocation", "cpu_utilization",
             value_type="FLOAT", units="%"),
    lld_item("slurm.partition.memory.total[{#PARTITION}]",
             "Partition [{#PARTITION}]: Memory total", "memory_total_bytes", units="B",
             heartbeat="1d"),
    lld_item("slurm.partition.memory.allocated[{#PARTITION}]",
             "Partition [{#PARTITION}]: Memory allocated", "memory_allocated_bytes",
             units="B"),
    lld_item("slurm.partition.memory.utilization[{#PARTITION}]",
             "Partition [{#PARTITION}]: Memory allocation", "memory_utilization",
             value_type="FLOAT", units="%"),
    lld_item("slurm.partition.gpus.total[{#PARTITION}]",
             "Partition [{#PARTITION}]: GPUs total", "gpus_total", heartbeat="1d"),
    lld_item("slurm.partition.gpus.allocated[{#PARTITION}]",
             "Partition [{#PARTITION}]: GPUs allocated", "gpus_allocated"),
    lld_item("slurm.partition.gpu.utilization[{#PARTITION}]",
             "Partition [{#PARTITION}]: GPU allocation", "gpu_utilization",
             value_type="FLOAT", units="%"),
    lld_item("slurm.partition.jobs.running[{#PARTITION}]",
             "Partition [{#PARTITION}]: Jobs running", "jobs_running"),
    lld_item("slurm.partition.jobs.pending[{#PARTITION}]",
             "Partition [{#PARTITION}]: Jobs pending", "jobs_pending"),
    lld_item("slurm.partition.jobs.total[{#PARTITION}]",
             "Partition [{#PARTITION}]: Jobs total", "jobs_total"),
    lld_item("slurm.partition.cpus.pending[{#PARTITION}]",
             "Partition [{#PARTITION}]: CPUs requested by pending jobs", "cpus_pending"),
    lld_item("slurm.partition.jobs.oldest_pending[{#PARTITION}]",
             "Partition [{#PARTITION}]: Oldest pending job age", "oldest_pending_age",
             units="s",
             description="Counts only jobs the scheduler could start: jobs waiting on a "
                         "dependency, a hold or a reservation window are excluded."),
]

QOS_ITEMS = [
    lld_item("slurm.qos.jobs.running[{#QOS}]", "QOS [{#QOS}]: Jobs running", "jobs_running"),
    lld_item("slurm.qos.jobs.pending[{#QOS}]", "QOS [{#QOS}]: Jobs pending", "jobs_pending"),
    lld_item("slurm.qos.jobs.total[{#QOS}]", "QOS [{#QOS}]: Jobs total", "jobs_total"),
    lld_item("slurm.qos.cpus.allocated[{#QOS}]", "QOS [{#QOS}]: CPUs allocated",
             "cpus_allocated"),
    lld_item("slurm.qos.cpus.limit[{#QOS}]", "QOS [{#QOS}]: CPU limit (GrpTRES)",
             "grp_cpus", heartbeat="1d",
             description="0 means the QOS has no GrpTRES CPU limit."),
    lld_item("slurm.qos.cpus.usage[{#QOS}]", "QOS [{#QOS}]: CPU limit usage", "cpu_usage",
             value_type="FLOAT", units="%",
             description="Allocated CPUs as a percentage of the GrpTRES CPU limit. "
                         "Stays at 0 when the QOS is unlimited."),
    lld_item("slurm.qos.priority[{#QOS}]", "QOS [{#QOS}]: Priority", "priority",
             heartbeat="1d"),
]

LICENSE_ITEMS = [
    lld_item("slurm.license.total[{#LICENSE}]", "License [{#LICENSE}]: Total", "total",
             heartbeat="1d"),
    lld_item("slurm.license.used[{#LICENSE}]", "License [{#LICENSE}]: Used", "used"),
    lld_item("slurm.license.free[{#LICENSE}]", "License [{#LICENSE}]: Free", "free"),
    lld_item("slurm.license.reserved[{#LICENSE}]", "License [{#LICENSE}]: Reserved",
             "reserved", heartbeat="1d"),
    lld_item("slurm.license.utilization[{#LICENSE}]", "License [{#LICENSE}]: Usage",
             "utilization", value_type="FLOAT", units="%"),
    lld_item("slurm.license.remote[{#LICENSE}]", "License [{#LICENSE}]: Remote",
             "remote", valuemap="Slurm yes/no", heartbeat="1d",
             description="Set when the pool is served by a remote license server rather "
                         "than configured locally in Slurm."),
]

RESERVATION_ITEMS = [
    lld_item("slurm.reservation.state[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: State", "state",
             value_type="CHAR", heartbeat="1d", trends="0"),
    lld_item("slurm.reservation.active[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Active", "active", valuemap="Slurm yes/no"),
    lld_item("slurm.reservation.maintenance[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Maintenance", "maintenance",
             valuemap="Slurm yes/no", heartbeat="1d",
             description="Set for reservations carrying the MAINT flag, which take their "
                         "nodes out of service."),
    lld_item("slurm.reservation.nodes[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Nodes", "nodes"),
    lld_item("slurm.reservation.cores[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Cores", "cores"),
    lld_item("slurm.reservation.starts_in[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Starts in", "starts_in", units="s",
             description="0 once the reservation has started."),
    lld_item("slurm.reservation.remaining[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Time remaining", "remaining", units="s"),
    lld_item("slurm.reservation.duration[{#RESERVATION}]",
             "Reservation [{#RESERVATION}]: Duration", "duration", units="s",
             heartbeat="1d"),
]

NODE_ITEMS = [
    lld_item("slurm.node.state[{#NODE}]", "Node [{#NODE}]: State", "state",
             value_type="CHAR", heartbeat="1d", trends="0", master=MASTER_NODES,
             description="Raw Slurm state including flags, for example MIXED+DRAIN."),
    lld_item("slurm.node.state.code[{#NODE}]", "Node [{#NODE}]: State code", "state_code",
             valuemap="Slurm node state", master=MASTER_NODES),
    lld_item("slurm.node.available[{#NODE}]", "Node [{#NODE}]: Availability", "available",
             valuemap="Slurm node availability", master=MASTER_NODES),
    lld_item("slurm.node.not_responding[{#NODE}]", "Node [{#NODE}]: Not responding",
             "not_responding", valuemap="Slurm yes/no", master=MASTER_NODES),
    lld_item("slurm.node.reason[{#NODE}]", "Node [{#NODE}]: State reason", "reason",
             value_type="CHAR", heartbeat="1d", trends="0", master=MASTER_NODES,
             description="Reason recorded by the administrator or by Slurm when the node "
                         "was drained or marked down."),
    lld_item("slurm.node.cpus.total[{#NODE}]", "Node [{#NODE}]: CPUs total", "cpus_total",
             heartbeat="1d", master=MASTER_NODES),
    lld_item("slurm.node.cpus.allocated[{#NODE}]", "Node [{#NODE}]: CPUs allocated",
             "cpus_allocated", master=MASTER_NODES),
    lld_item("slurm.node.cpus.idle[{#NODE}]", "Node [{#NODE}]: CPUs idle", "cpus_idle",
             master=MASTER_NODES),
    lld_item("slurm.node.cpu.utilization[{#NODE}]", "Node [{#NODE}]: CPU allocation",
             "cpu_utilization", value_type="FLOAT", units="%", master=MASTER_NODES),
    lld_item("slurm.node.cpu.load[{#NODE}]", "Node [{#NODE}]: Load average", "cpu_load",
             value_type="FLOAT", master=MASTER_NODES,
             description="One minute load average reported by slurmd."),
    lld_item("slurm.node.cpu.load_per_core[{#NODE}]", "Node [{#NODE}]: Load per core",
             "cpu_load_per_core", value_type="FLOAT", master=MASTER_NODES,
             description="Load average divided by the number of configured CPUs."),
    lld_item("slurm.node.memory.total[{#NODE}]", "Node [{#NODE}]: Memory total",
             "memory_total_bytes", units="B", heartbeat="1d", master=MASTER_NODES),
    lld_item("slurm.node.memory.allocated[{#NODE}]", "Node [{#NODE}]: Memory allocated",
             "memory_allocated_bytes", units="B", master=MASTER_NODES),
    lld_item("slurm.node.memory.free[{#NODE}]", "Node [{#NODE}]: Memory free",
             "memory_free_bytes", units="B", master=MASTER_NODES),
    lld_item("slurm.node.memory.utilization[{#NODE}]", "Node [{#NODE}]: Memory allocation",
             "memory_utilization", value_type="FLOAT", units="%", master=MASTER_NODES),
    lld_item("slurm.node.memory.free.pct[{#NODE}]", "Node [{#NODE}]: Memory free, percent",
             "memory_free_pct", value_type="FLOAT", units="%", master=MASTER_NODES),
    lld_item("slurm.node.tmp_disk[{#NODE}]", "Node [{#NODE}]: Temporary disk space",
             "tmp_disk_bytes", units="B", heartbeat="1d", master=MASTER_NODES),
    lld_item("slurm.node.gpus.total[{#NODE}]", "Node [{#NODE}]: GPUs total", "gpus_total",
             heartbeat="1d", master=MASTER_NODES),
    lld_item("slurm.node.gpus.allocated[{#NODE}]", "Node [{#NODE}]: GPUs allocated",
             "gpus_allocated", master=MASTER_NODES),
    lld_item("slurm.node.gpu.utilization[{#NODE}]", "Node [{#NODE}]: GPU allocation",
             "gpu_utilization", value_type="FLOAT", units="%", master=MASTER_NODES),
    lld_item("slurm.node.gpu.type[{#NODE}]", "Node [{#NODE}]: GPU type", "gpu_type",
             value_type="CHAR", heartbeat="1d", trends="0", master=MASTER_NODES,
             description="GPU model as configured in the node's Gres, for example a100. "
                         "Empty on nodes without GPUs, or where the GRES is untyped."),
    lld_item("slurm.node.uptime[{#NODE}]", "Node [{#NODE}]: Uptime", "uptime",
             units="uptime", master=MASTER_NODES),
    lld_item("slurm.node.unavailable.age[{#NODE}]", "Node [{#NODE}]: Unavailable for",
             "unavailable_age", units="s", master=MASTER_NODES,
             description="How long the node has been out of service, taken from the "
                         "timestamp Slurm records with the drain or down reason. Back to "
                         "0 as soon as the node is usable again."),
    lld_item("slurm.node.reason.user[{#NODE}]", "Node [{#NODE}]: State set by",
             "reason_user", value_type="CHAR", heartbeat="1d", trends="0",
             master=MASTER_NODES,
             description="Who drained or downed the node: an administrator, or slurm "
                         "itself when it did so automatically."),
]

DISCOVERY_RULES = [
    {
        "key": "slurm.partitions.discovery",
        "name": "Slurm: Partition discovery",
        "master": MASTER_CLUSTER,
        "path": "$.partitions",
        "component": "partitions",
        "macros": [("{#PARTITION}", "$.name"), ("{#PARTITION_STATE}", "$.state"),
                   ("{#PARTITION_DEFAULT}", "$.default")],
        "filters": [("{#PARTITION}", "MATCHES_REGEX", "{$SLURM.PARTITION.DISCOVERY.MATCHES}"),
                    ("{#PARTITION}", "NOT_MATCHES_REGEX",
                     "{$SLURM.PARTITION.DISCOVERY.NOT_MATCHES}")],
        "selector": "$.partitions[?(@.name=='{#PARTITION}')].%s.first()",
        "items": PARTITION_ITEMS,
        "description": "Discovers the partitions reported by scontrol show partition.",
    },
    {
        "key": "slurm.qos.discovery",
        "name": "Slurm: QOS discovery",
        "master": MASTER_CLUSTER,
        "path": "$.qos",
        "component": "qos",
        "macros": [("{#QOS}", "$.name")],
        "filters": [("{#QOS}", "MATCHES_REGEX", "{$SLURM.QOS.DISCOVERY.MATCHES}"),
                    ("{#QOS}", "NOT_MATCHES_REGEX", "{$SLURM.QOS.DISCOVERY.NOT_MATCHES}")],
        "selector": "$.qos[?(@.name=='{#QOS}')].%s.first()",
        "items": QOS_ITEMS,
        "description": "Discovers the QOS entries known to the accounting database, plus "
                       "any QOS currently used by a job.",
    },
    {
        "key": "slurm.licenses.discovery",
        "name": "Slurm: License discovery",
        "master": MASTER_CLUSTER,
        "path": "$.licenses",
        "component": "licenses",
        "macros": [("{#LICENSE}", "$.name")],
        "filters": [("{#LICENSE}", "MATCHES_REGEX", "{$SLURM.LICENSE.DISCOVERY.MATCHES}"),
                    ("{#LICENSE}", "NOT_MATCHES_REGEX",
                     "{$SLURM.LICENSE.DISCOVERY.NOT_MATCHES}")],
        "selector": "$.licenses[?(@.name=='{#LICENSE}')].%s.first()",
        "items": LICENSE_ITEMS,
        "description": "Discovers the license pools reported by scontrol show licenses. "
                       "Clusters that configure no licenses discover nothing.",
    },
    {
        "key": "slurm.reservations.discovery",
        "name": "Slurm: Reservation discovery",
        "master": MASTER_CLUSTER,
        "path": "$.reservations",
        "component": "reservations",
        # Reservations come and go, so lost ones are cleaned up quickly.
        "lifetime": "1d",
        "macros": [("{#RESERVATION}", "$.name"), ("{#RESERVATION_PARTITION}", "$.partition")],
        "filters": [("{#RESERVATION}", "MATCHES_REGEX",
                     "{$SLURM.RESERVATION.DISCOVERY.MATCHES}"),
                    ("{#RESERVATION}", "NOT_MATCHES_REGEX",
                     "{$SLURM.RESERVATION.DISCOVERY.NOT_MATCHES}")],
        "selector": "$.reservations[?(@.name=='{#RESERVATION}')].%s.first()",
        "items": RESERVATION_ITEMS,
        "description": "Discovers the reservations reported by scontrol show reservation.\n"
                       "Reservations explain why nodes are unavailable: a MAINT "
                       "reservation takes its nodes out of service for the duration.",
    },
    {
        "key": "slurm.nodes.discovery",
        "name": "Slurm: Node discovery",
        "master": MASTER_NODES,
        "path": "$.nodes",
        "component": "nodes",
        "macros": [("{#NODE}", "$.name"), ("{#NODE_STATE}", "$.state_base"),
                   ("{#NODE_ADDR}", "$.address"), ("{#PARTITIONS}", "$.partitions")],
        "filters": [("{#NODE}", "MATCHES_REGEX", "{$SLURM.NODE.DISCOVERY.MATCHES}"),
                    ("{#NODE}", "NOT_MATCHES_REGEX", "{$SLURM.NODE.DISCOVERY.NOT_MATCHES}"),
                    ("{#PARTITIONS}", "MATCHES_REGEX", "{$SLURM.NODE.PARTITION.MATCHES}")],
        "selector": "$.nodes[?(@.name=='{#NODE}')].%s.first()",
        "items": NODE_ITEMS,
        "description": "Discovers compute nodes from scontrol show node.\n"
                       "On clusters with more than a few hundred nodes, restrict the "
                       "discovery with {$SLURM.NODE.DISCOVERY.MATCHES} or "
                       "{$SLURM.NODE.PARTITION.MATCHES}, or disable the rule entirely and "
                       "rely on the cluster level node counters.",
    },
]

# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def expr(key, function="last", args="", value=""):
    """A history function applied to an item: min(/template/key,15m)>5."""
    reference = "/%s/%s" % (TEMPLATE, key)
    if args:
        reference = "%s,%s" % (reference, args)
    return "%s(%s)%s" % (function, reference, value)


def length_of(key):
    """length() is a string function: it takes a value, not an item reference.

    "length(/template/key)" does not parse, so the value has to come from a
    history function first, exactly as the official templates do it.
    """
    return "length(%s)" % expr(key, "last")


CTLD_DOWN = "slurmctld-down"
NO_DATA = "no-data"

TRIGGERS = [
    {
        "id": NO_DATA,
        "name": "Slurm: No data collected for {$SLURM.DATA.TIMEOUT}",
        "expression": expr("slurm.data.age", "nodata", "{$SLURM.DATA.TIMEOUT}", "=1"),
        "priority": "WARNING",
        "scope": "availability",
        "description": "Zabbix has not received any Slurm data. Check the Zabbix agent on "
                       "the collector host, the UserParameter configuration and whether "
                       "the Slurm client commands are usable by the zabbix user.",
    },
    {
        "id": "data-stale",
        "name": "Slurm: Collected data is stale",
        "expression": expr("slurm.data.age", "min", "10m", ">{$SLURM.DATA.STALE.MAX}"),
        "priority": "WARNING",
        "scope": "availability",
        "depends": [NO_DATA],
        "description": "The collector keeps serving an old cached document. When the cache "
                       "is refreshed by a systemd timer, check that "
                       "zabbix-slurm-collector.timer is active.",
    },
    {
        "id": "collector-errors",
        "name": "Slurm: Collector reported errors",
        "expression": expr("slurm.collector.error_count", "min", "10m", ">0"),
        "priority": "WARNING",
        "scope": "availability",
        "opdata": "Errors: {ITEM.LASTVALUE1}",
        "depends": [NO_DATA],
        "description": "One or more Slurm commands failed. The item "
                       "'Slurm: Collector errors' holds the messages.",
    },
    {
        "id": CTLD_DOWN,
        "name": "Slurm: slurmctld is not responding",
        "expression": expr("slurm.ctld.available", "last", "", "=0"),
        "priority": "HIGH",
        "scope": "availability",
        "depends": [NO_DATA],
        "description": "scontrol ping reports the primary controller as DOWN. No job can "
                       "be submitted or scheduled while this lasts.",
    },
    {
        "id": "ctld-backup-down",
        "name": "Slurm: slurmctld backup controller is not responding",
        "expression": (expr("slurm.ctld.backup.available", "last", "", "=0") + " and " +
                       expr("slurm.ctld.backup.available", "max", "7d", "=1")),
        "priority": "WARNING",
        "scope": "availability",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "The backup controller stopped answering scontrol ping. The trigger "
                       "only fires on clusters where the backup controller has been seen "
                       "up during the last week, so it stays silent without a backup "
                       "controller.",
    },
    {
        "id": "dbd-down",
        "name": "Slurm: slurmdbd is not reachable",
        "expression": expr("slurm.dbd.available", "min", "10m", "=0"),
        "priority": "AVERAGE",
        "scope": "availability",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "sacctmgr cannot reach the accounting database. Jobs keep running, "
                       "but accounting records are buffered by slurmctld and fair-share "
                       "priorities stop being updated.",
    },
    {
        "id": "dbd-queue",
        "name": "Slurm: DBD agent queue is too large",
        "expression": expr("slurm.sched.dbd_agent_queue_size", "min", "15m",
                           ">{$SLURM.SCHED.DBD.QUEUE.MAX}"),
        "priority": "AVERAGE",
        "scope": "performance",
        "opdata": "Queue: {ITEM.LASTVALUE1}",
        "depends": ["dbd-down", CTLD_DOWN, NO_DATA],
        "description": "slurmctld is buffering accounting records because slurmdbd is not "
                       "keeping up. Accounting data is lost if the queue overflows.",
    },
    {
        "id": "agent-queue",
        "name": "Slurm: Controller agent queue is too large",
        "expression": expr("slurm.sched.agent_queue_size", "min", "15m",
                           ">{$SLURM.SCHED.AGENT.QUEUE.MAX}"),
        "priority": "AVERAGE",
        "scope": "performance",
        "opdata": "Queue: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "slurmctld cannot deliver its RPCs to the compute nodes fast "
                       "enough. Usually caused by unreachable nodes or by a network "
                       "problem.",
    },
    {
        "id": "nodes-down",
        "name": "Slurm: Nodes are down",
        "expression": (expr("slurm.nodes.down", "min", "10m", ">{$SLURM.NODES.DOWN.MAX}") +
                       " or " +
                       expr("slurm.nodes.fail", "min", "10m", ">{$SLURM.NODES.DOWN.MAX}")),
        "priority": "AVERAGE",
        "scope": "availability",
        "opdata": "Down: {ITEM.LASTVALUE1}, failed: {ITEM.LASTVALUE2}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Nodes are in DOWN or FAIL state and cannot run jobs.",
    },
    {
        "id": "nodes-availability",
        "name": "Slurm: Less than {$SLURM.NODES.AVAILABILITY.MIN}% of the nodes are usable",
        "expression": expr("slurm.nodes.availability", "max", "15m",
                           "<{$SLURM.NODES.AVAILABILITY.MIN}"),
        "priority": "AVERAGE",
        "scope": "availability",
        "opdata": "Usable: {ITEM.LASTVALUE1}",
        "depends": ["nodes-down", CTLD_DOWN, NO_DATA],
        "description": "A large part of the cluster is down, drained or not responding.",
    },
    {
        "id": "nodes-drain",
        "name": "Slurm: More than {$SLURM.NODES.DRAIN.MAX.PCT}% of the nodes are drained",
        "expression": ("100*" + expr("slurm.nodes.drain", "last") + "/" +
                       "(" + expr("slurm.nodes.total", "last") + "+0.0001)" +
                       ">{$SLURM.NODES.DRAIN.MAX.PCT}"),
        "priority": "WARNING",
        "scope": "capacity",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Drained nodes accumulate, usually because failed nodes are never "
                       "put back into service.",
    },
    {
        "id": "nodes-not-responding",
        "name": "Slurm: Nodes are not responding",
        "expression": expr("slurm.nodes.not_responding", "min", "10m", ">0"),
        "priority": "WARNING",
        "scope": "availability",
        "opdata": "Nodes: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "slurmctld cannot reach one or more slurmd daemons.",
    },
    {
        "id": "cpu-saturated",
        "name": "Slurm: Cluster CPU allocation is above {$SLURM.CPU.UTIL.HIGH}%",
        "expression": (expr("slurm.cpus.utilization", "min", "{$SLURM.CPU.UTIL.TIME}",
                            ">{$SLURM.CPU.UTIL.HIGH}") + " and " +
                       expr("slurm.jobs.pending", "min", "{$SLURM.CPU.UTIL.TIME}", ">0")),
        "priority": "INFO",
        "scope": "capacity",
        "opdata": "Allocation: {ITEM.LASTVALUE1}, pending: {ITEM.LASTVALUE2}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "The cluster is fully allocated while jobs are still queueing. This "
                       "is normal for a busy cluster and is meant as a capacity signal, "
                       "not as a fault.",
    },
    {
        "id": "jobs-pending",
        "name": "Slurm: Job backlog is above {$SLURM.JOBS.PENDING.MAX} jobs",
        "expression": expr("slurm.jobs.pending", "min", "{$SLURM.JOBS.PENDING.TIME}",
                           ">{$SLURM.JOBS.PENDING.MAX}"),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Pending: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "The queue is longer than expected. Check the pending reason "
                       "breakdown to tell a capacity shortage from a limit or a "
                       "configuration problem.",
    },
    {
        "id": "jobs-waiting",
        "name": "Slurm: Jobs are waiting longer than {$SLURM.JOBS.PENDING.AGE.MAX}",
        "expression": expr("slurm.jobs.oldest_pending_age", "min", "30m",
                           ">{$SLURM.JOBS.PENDING.AGE.MAX}"),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Oldest: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "At least one job the scheduler could start has been queued for "
                       "longer than the accepted waiting time.\n\n"
                       "Jobs waiting on a dependency, a hold or a reservation window do "
                       "not count: they are waiting for something other than the cluster. "
                       "If this fires on a genuinely busy cluster, raise "
                       "{$SLURM.JOBS.PENDING.AGE.MAX} rather than muting it.",
    },
    {
        "id": "jobs-blocked",
        "name": "Slurm: Jobs are blocked by a limit while the cluster has free CPUs",
        # Only jobs held back by a limit count here.  Counting every pending job
        # made this fire whenever something was waiting on a dependency, which
        # no amount of free capacity can resolve.
        "expression": (expr("slurm.jobs.pending.resources", "min", "30m", "=0") + " and " +
                       expr("slurm.jobs.pending.limited", "min", "30m", ">0") + " and " +
                       expr("slurm.cpus.idle", "min", "30m", ">0")),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Blocked: {ITEM.LASTVALUE2}, idle CPUs: {ITEM.LASTVALUE3}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Jobs are held back by a QOS, association or partition limit while "
                       "CPUs are idle and nothing is waiting for resources. That points at "
                       "a limit or a scheduling problem rather than at a lack of "
                       "capacity.\n\n"
                       "Jobs waiting on a dependency, a license, a reservation or a hold "
                       "are excluded: they are waiting for something identifiable and "
                       "cannot start however much capacity is free. Use the pending reason "
                       "breakdown to see the whole queue.",
    },
    {
        "id": "jobs-table",
        "name": "Slurm: Job table usage is above {$SLURM.JOBS.USAGE.HIGH}%",
        "expression": expr("slurm.jobs.usage", "min", "15m", ">{$SLURM.JOBS.USAGE.HIGH}"),
        "priority": "AVERAGE",
        "scope": "capacity",
        "opdata": "Usage: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "The number of active jobs is approaching MaxJobCount. New "
                       "submissions are rejected once the limit is reached.",
    },
    {
        "id": "sched-cycle",
        "name": "Slurm: Main scheduling cycle is slow",
        "expression": expr("slurm.sched.cycle.mean", "min", "30m", ">{$SLURM.SCHED.CYCLE.MAX}"),
        "priority": "WARNING",
        "scope": "performance",
        "opdata": "Mean cycle: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "The main scheduler needs a long time per cycle, so jobs start late. "
                       "Consider tuning SchedulerParameters (default_queue_depth, "
                       "sched_min_interval).",
    },
    {
        "id": "backfill-cycle",
        "name": "Slurm: Backfill scheduling cycle is slow",
        "expression": expr("slurm.backfill.cycle.mean", "min", "30m",
                           ">{$SLURM.BACKFILL.CYCLE.MAX}"),
        "priority": "WARNING",
        "scope": "performance",
        "opdata": "Mean cycle: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Backfill cycles take long, which delays the start of backfilled "
                       "jobs. Consider tuning bf_max_job_test, bf_window or bf_interval.",
    },
    {
        "id": "backfill-stalled",
        "name": "Slurm: Backfill scheduler has not run for {$SLURM.BACKFILL.AGE.MAX}",
        # The backfill scheduler only runs when there is something to backfill,
        # so the age of the last cycle grows on its own whenever nothing is
        # waiting to execute.  Jobs blocked on a dependency, a hold or a
        # reservation do not give it anything to do, so they must not count.
        "expression": (expr("slurm.backfill.last_cycle_age", "min", "15m",
                            ">{$SLURM.BACKFILL.AGE.MAX}") + " and " +
                       expr("slurm.jobs.pending.schedulable", "min", "15m", ">0")),
        "priority": "WARNING",
        "scope": "performance",
        "opdata": "Last cycle: {ITEM.LASTVALUE1}, ready to run: {ITEM.LASTVALUE2}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "No backfill cycle completed recently while jobs were waiting to "
                       "execute, so small jobs are no longer being started ahead of large "
                       "ones.\n\n"
                       "Jobs that are ready to run have to have been present for the last "
                       "15 minutes. A queue made up of jobs waiting on a dependency, a "
                       "hold or a reservation gives the backfill scheduler nothing to do, "
                       "so the age of its last cycle grows by itself, which is normal and "
                       "not worth an alert.",
    },
    {
        "id": "job-failure-rate",
        "name": "Slurm: Job failure rate is above {$SLURM.ACCOUNTING.FAILURE.RATE.MAX}%",
        "expression": (expr("slurm.accounting.failure_rate", "min", "1h",
                            ">{$SLURM.ACCOUNTING.FAILURE.RATE.MAX}") + " and " +
                       expr("slurm.accounting.jobs.total", "min", "1h",
                            ">{$SLURM.ACCOUNTING.MIN.JOBS}")),
        "priority": "WARNING",
        "scope": "performance",
        "opdata": "Failure rate: {ITEM.LASTVALUE1}, jobs: {ITEM.LASTVALUE2}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Jobs are failing, timing out, hitting node failures or running out "
                       "of memory more often than expected. The job count guard keeps the "
                       "trigger quiet on an idle cluster where a couple of failures would "
                       "otherwise be a large percentage.\n\n"
                       "Requires the accounting collection, which is disabled by default.",
    },
    {
        "id": "node-failures",
        "name": "Slurm: Jobs are being killed by node failures",
        "expression": expr("slurm.accounting.jobs.node_fail", "min", "1h", ">0"),
        "priority": "AVERAGE",
        "scope": "availability",
        "opdata": "Jobs lost: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Jobs were lost because the nodes running them failed. Unlike a "
                       "drained node, this destroys work that has already been done.\n\n"
                       "Requires the accounting collection, which is disabled by default.",
    },
    {
        "id": "nodes-unavailable-long",
        "name": "Slurm: A node has been out of service for more than "
                "{$SLURM.NODE.UNAVAILABLE.AGE.MAX}",
        "expression": expr("slurm.nodes.longest_unavailable_age", "min", "30m",
                           ">{$SLURM.NODE.UNAVAILABLE.AGE.MAX}"),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Longest outage: {ITEM.LASTVALUE1}",
        "depends": [CTLD_DOWN, NO_DATA],
        "description": "Capacity has been out of service for a long time. This works "
                       "without node discovery, so it still fires on clusters where per "
                       "node monitoring is switched off.",
    },
    {
        "id": "version-changed",
        "name": "Slurm: Version has changed",
        # change() is numeric only, so string values are compared explicitly.
        # The length() guard keeps the trigger from firing on the first value
        # ever collected, when there is no previous value to compare against.
        "expression": "%s<>%s and %s>0" % (expr("slurm.cluster.version", "last", "#1"),
                                           expr("slurm.cluster.version", "last", "#2"),
                                           length_of("slurm.cluster.version")),
        "priority": "INFO",
        "scope": "notice",
        "manual_close": True,
        "opdata": "Version: {ITEM.LASTVALUE1}",
        "description": "The Slurm version reported by the controller changed, which "
                       "normally means the cluster was upgraded.",
    },
]

PARTITION_TRIGGERS = [
    {
        "id": "partition-not-up",
        "name": "Partition [{#PARTITION}]: State is not UP",
        "expression": expr("slurm.partition.state.code[{#PARTITION}]", "last", "", "<>1"),
        "priority": "WARNING",
        "scope": "availability",
        "opdata": "State: {ITEM.LASTVALUE1}",
        "description": "The partition is DOWN, DRAIN or INACTIVE and does not start jobs.",
    },
    {
        "id": "partition-no-nodes",
        "name": "Partition [{#PARTITION}]: No usable nodes left",
        "expression": (expr("slurm.partition.nodes.available[{#PARTITION}]", "last", "", "=0")
                       + " and " +
                       expr("slurm.partition.nodes.total[{#PARTITION}]", "last", "", ">0")),
        "priority": "HIGH",
        "scope": "availability",
        "description": "Every node of the partition is down, drained or not responding.",
    },
    {
        "id": "partition-nodes-down",
        "name": "Partition [{#PARTITION}]: Nodes are down",
        "expression": expr("slurm.partition.nodes.down[{#PARTITION}]", "min", "10m",
                           ">{$SLURM.PARTITION.NODES.DOWN.MAX:\"{#PARTITION}\"}"),
        "priority": "AVERAGE",
        "scope": "availability",
        "opdata": "Down: {ITEM.LASTVALUE1}",
        "depends": ["partition-no-nodes"],
    },
    {
        "id": "partition-backlog",
        "name": "Partition [{#PARTITION}]: Job backlog is too large",
        "expression": expr("slurm.partition.jobs.pending[{#PARTITION}]", "min",
                           "{$SLURM.JOBS.PENDING.TIME}",
                           ">{$SLURM.PARTITION.JOBS.PENDING.MAX:\"{#PARTITION}\"}"),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Pending: {ITEM.LASTVALUE1}",
    },
    {
        "id": "partition-waiting",
        "name": "Partition [{#PARTITION}]: Jobs wait longer than expected",
        "expression": expr("slurm.partition.jobs.oldest_pending[{#PARTITION}]", "min", "30m",
                           ">{$SLURM.PARTITION.PENDING.AGE.MAX:\"{#PARTITION}\"}"),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Oldest: {ITEM.LASTVALUE1}",
        "description": "A job the scheduler could start has been queued in this partition "
                       "for longer than expected. Jobs waiting on a dependency, a hold or "
                       "a reservation window do not count.\n\n"
                       "Partitions differ, so the threshold takes the partition name as "
                       "macro context: {$SLURM.PARTITION.PENDING.AGE.MAX:\"{#PARTITION}\"}.",
    },
    {
        "id": "partition-saturated",
        "name": "Partition [{#PARTITION}]: CPU allocation is saturated",
        "expression": (expr("slurm.partition.cpu.utilization[{#PARTITION}]", "min",
                            "{$SLURM.CPU.UTIL.TIME}",
                            ">{$SLURM.PARTITION.CPU.UTIL.HIGH:\"{#PARTITION}\"}") +
                       " and " +
                       expr("slurm.partition.jobs.pending[{#PARTITION}]", "min",
                            "{$SLURM.CPU.UTIL.TIME}", ">0")),
        "priority": "INFO",
        "scope": "capacity",
        "opdata": "Allocation: {ITEM.LASTVALUE1}, pending: {ITEM.LASTVALUE2}",
        "depends": ["partition-backlog"],
    },
]

LICENSE_TRIGGERS = [
    {
        "id": "license-exhausted",
        "name": "License [{#LICENSE}]: Pool is exhausted and jobs are waiting",
        "expression": (expr("slurm.license.free[{#LICENSE}]", "max", "15m", "=0") +
                       " and " +
                       expr("slurm.jobs.pending.licenses", "min", "15m", ">0")),
        "priority": "WARNING",
        "scope": "capacity",
        "opdata": "Used: {ITEM.LASTVALUE1}",
        "description": "Every token of this license is checked out and jobs are queueing "
                       "for a license. Either the pool is too small or tokens are held by "
                       "jobs that are not using them.",
    },
    {
        "id": "license-usage",
        "name": "License [{#LICENSE}]: Usage is above {$SLURM.LICENSE.USAGE.HIGH}%",
        "expression": expr("slurm.license.utilization[{#LICENSE}]", "min", "30m",
                           ">{$SLURM.LICENSE.USAGE.HIGH:\"{#LICENSE}\"}"),
        "priority": "INFO",
        "scope": "capacity",
        "opdata": "Usage: {ITEM.LASTVALUE1}",
        "depends": ["license-exhausted"],
        "description": "The license pool is close to being fully consumed.",
    },
]

QOS_TRIGGERS = [
    {
        "id": "qos-limit",
        "name": "QOS [{#QOS}]: CPU limit is nearly exhausted",
        "expression": (expr("slurm.qos.cpus.usage[{#QOS}]", "min", "15m",
                            ">{$SLURM.QOS.CPU.USAGE.HIGH}") + " and " +
                       expr("slurm.qos.jobs.pending[{#QOS}]", "min", "15m", ">0")),
        "priority": "INFO",
        "scope": "capacity",
        "opdata": "Usage: {ITEM.LASTVALUE1}",
        "description": "Jobs of this QOS are queueing while the QOS is close to its "
                       "GrpTRES CPU limit.",
    },
]

NODE_TRIGGERS = [
    {
        "id": "node-down",
        "name": "Node [{#NODE}]: State is DOWN or FAIL",
        "expression": (expr("slurm.node.state.code[{#NODE}]", "last", "", "=4") + " or " +
                       expr("slurm.node.state.code[{#NODE}]", "last", "", "=7")),
        "priority": "AVERAGE",
        "scope": "availability",
        "description": "The node cannot run jobs. The item 'State reason' holds the reason "
                       "recorded by Slurm.",
    },
    {
        "id": "node-not-responding",
        "name": "Node [{#NODE}]: Not responding",
        "expression": expr("slurm.node.not_responding[{#NODE}]", "last", "", "=1"),
        "priority": "AVERAGE",
        "scope": "availability",
        "depends": ["node-down"],
        "description": "slurmctld cannot reach slurmd on this node.",
    },
    {
        "id": "node-drained",
        "name": "Node [{#NODE}]: Drained",
        # The reason item is part of the expression so that the alert can show
        # it through {ITEM.LASTVALUE2}; length()>=0 is always true and does not
        # change when the trigger fires.
        "expression": (expr("slurm.node.state.code[{#NODE}]", "last", "", "=5") + " and " +
                       length_of("slurm.node.reason[{#NODE}]") + ">=0"),
        "priority": "WARNING",
        "scope": "availability",
        "opdata": "Reason: {ITEM.LASTVALUE2}",
        "depends": ["node-down"],
        "description": "The node is drained and runs no jobs any more.",
    },
    {
        "id": "node-draining",
        "name": "Node [{#NODE}]: Draining",
        "expression": (expr("slurm.node.state.code[{#NODE}]", "last", "", "=6") + " and " +
                       length_of("slurm.node.reason[{#NODE}]") + ">=0"),
        "priority": "INFO",
        "scope": "availability",
        "opdata": "Reason: {ITEM.LASTVALUE2}",
        "depends": ["node-down"],
        "description": "The node is marked for draining and stops accepting new jobs while "
                       "the running ones finish.",
    },
    {
        "id": "node-maint",
        "name": "Node [{#NODE}]: In maintenance",
        "expression": expr("slurm.node.state.code[{#NODE}]", "last", "", "=8"),
        "priority": "INFO",
        "scope": "notice",
        "depends": ["node-down"],
    },
    {
        "id": "node-load",
        "name": "Node [{#NODE}]: Load per core is above {$SLURM.NODE.LOAD.MAX}",
        "expression": expr("slurm.node.cpu.load_per_core[{#NODE}]", "min", "15m",
                           ">{$SLURM.NODE.LOAD.MAX:\"{#NODE}\"}"),
        "priority": "WARNING",
        "scope": "performance",
        "opdata": "Load per core: {ITEM.LASTVALUE1}",
        "depends": ["node-down"],
        "description": "The node is running more work than it has cores, which usually "
                       "means jobs are oversubscribing their CPU allocation.",
    },
    {
        "id": "node-memory",
        "name": "Node [{#NODE}]: Free memory is below {$SLURM.NODE.MEMORY.FREE.MIN}%",
        "expression": expr("slurm.node.memory.free.pct[{#NODE}]", "max", "15m",
                           "<{$SLURM.NODE.MEMORY.FREE.MIN:\"{#NODE}\"}"),
        "priority": "WARNING",
        "scope": "performance",
        "opdata": "Free: {ITEM.LASTVALUE1}",
        "depends": ["node-down"],
        "description": "Little memory is reported free on the node.\n\n"
                       "Disabled by default: {$SLURM.NODE.MEMORY.FREE.MIN} is 0, and a "
                       "percentage is never below 0. Slurm's FreeMem does not count "
                       "reclaimable page cache as free, so on a node that has been running "
                       "jobs it falls towards zero during normal operation and this trigger "
                       "would fire constantly.\n\n"
                       "To detect real memory pressure, monitor the compute nodes with an "
                       "operating system template instead, which uses available memory "
                       "rather than free memory. Set this macro above 0, per node if "
                       "wanted, only where FreeMem is known to be meaningful.",
    },
    {
        "id": "node-unavailable-long",
        "name": "Node [{#NODE}]: Out of service for more than "
                "{$SLURM.NODE.UNAVAILABLE.AGE.MAX}",
        "expression": (expr("slurm.node.unavailable.age[{#NODE}]", "min", "30m",
                            ">{$SLURM.NODE.UNAVAILABLE.AGE.MAX:\"{#NODE}\"}") +
                       " and " +
                       expr("slurm.node.available[{#NODE}]", "last", "", "=0")),
        "priority": "WARNING",
        "scope": "availability",
        "opdata": "Unavailable for: {ITEM.LASTVALUE1}",
        "description": "The node has been drained or down for a long time, measured from "
                       "the timestamp Slurm recorded with the reason.\n\n"
                       "This deliberately does not depend on the node being down: it is "
                       "the escalation for capacity that was taken out of service and "
                       "never brought back, which is easy to lose track of on a large "
                       "cluster.",
    },
    {
        "id": "node-rebooted",
        "name": "Node [{#NODE}]: Has been restarted",
        "expression": expr("slurm.node.uptime[{#NODE}]", "last", "", "<{$SLURM.NODE.UPTIME.MIN}"),
        "priority": "INFO",
        "scope": "notice",
        "manual_close": True,
        "depends": ["node-down"],
    },
]

# Trigger prototypes belong to their discovery rule; they are attached here
# because the rules are defined before the triggers.
for _rule in DISCOVERY_RULES:
    _rule["triggers"] = {
        "slurm.partitions.discovery": PARTITION_TRIGGERS,
        "slurm.qos.discovery": QOS_TRIGGERS,
        "slurm.nodes.discovery": NODE_TRIGGERS,
        "slurm.licenses.discovery": LICENSE_TRIGGERS,
        # Reservations are informational: they explain alerts rather than
        # raising any of their own.
        "slurm.reservations.discovery": [],
    }[_rule["key"]]

# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------

# Graphs whose series are mutually exclusive and add up to a meaningful total.
# Stacking them makes the height of the graph the total, so the breakdown can be
# read without adding the lines up by eye.  Only genuinely exclusive series
# belong here: "Nodes by state" is deliberately absent because "not responding"
# is a flag that overlaps the state counts, and stacking it would double count.
STACKED_GRAPHS = frozenset([
    "Slurm: Pending jobs by reason",
    "Slurm: Jobs by state",
])

GRAPHS = [
    ("Slurm: Nodes by state", [
        ("slurm.nodes.idle", GREEN), ("slurm.nodes.allocated", BLUE),
        ("slurm.nodes.mixed", TEAL), ("slurm.nodes.down", RED),
        ("slurm.nodes.drained", ORANGE), ("slurm.nodes.draining", OLIVE),
        ("slurm.nodes.maint", PURPLE), ("slurm.nodes.not_responding", DARKRED),
    ]),
    ("Slurm: Node availability", [
        ("slurm.nodes.total", GREY), ("slurm.nodes.available", GREEN),
        ("slurm.nodes.unavailable", RED),
    ]),
    ("Slurm: CPU allocation", [
        ("slurm.cpus.allocated", BLUE), ("slurm.cpus.idle", GREEN),
        ("slurm.cpus.other", RED), ("slurm.cpus.total", GREY),
    ]),
    ("Slurm: Memory allocation", [
        ("slurm.memory.total", GREY), ("slurm.memory.allocated", BLUE),
        ("slurm.memory.free", GREEN),
    ]),
    ("Slurm: GPU allocation", [
        ("slurm.gpus.total", GREY), ("slurm.gpus.allocated", BLUE),
        ("slurm.gpus.idle", GREEN),
    ]),
    ("Slurm: Jobs by state", [
        ("slurm.jobs.running", GREEN), ("slurm.jobs.pending", ORANGE),
        ("slurm.jobs.suspended", RED), ("slurm.jobs.completing", BLUE),
        ("slurm.jobs.configuring", TEAL), ("slurm.jobs.other", GREY),
    ]),
    ("Slurm: Pending jobs by reason", [
        ("slurm.jobs.pending.resources", GREEN),
        ("slurm.jobs.pending.priority", BLUE),
        ("slurm.jobs.pending.dependency", TEAL),
        ("slurm.jobs.pending.qos_limit", ORANGE),
        ("slurm.jobs.pending.association_limit", OLIVE),
        ("slurm.jobs.pending.licenses", PURPLE),
        ("slurm.jobs.pending.reservation", PINK),
        ("slurm.jobs.pending.partition", RED),
        ("slurm.jobs.pending.node_unavailable", DARKRED),
        ("slurm.jobs.pending.held", PLUM),
        ("slurm.jobs.pending.other", GREY),
    ]),
    ("Slurm: Queue wait time", [
        ("slurm.jobs.oldest_pending_age", RED),
        ("slurm.jobs.mean_pending_age", BLUE),
        ("slurm.jobs.longest_running_age", GREEN),
    ]),
    ("Slurm: Queue size in CPUs", [
        ("slurm.jobs.cpus_pending", ORANGE), ("slurm.cpus.idle", GREEN),
    ]),
    ("Slurm: Job throughput", [
        ("slurm.sched.jobs_submitted.rate", BLUE),
        ("slurm.sched.jobs_started.rate", GREEN),
        ("slurm.sched.jobs_completed.rate", TEAL),
        ("slurm.sched.jobs_canceled.rate", ORANGE),
        ("slurm.sched.jobs_failed.rate", RED),
    ]),
    ("Slurm: Scheduler cycle times", [
        ("slurm.sched.cycle.last", LIGHTBLUE), ("slurm.sched.cycle.mean", BLUE),
        ("slurm.backfill.cycle.last", SAGE), ("slurm.backfill.cycle.mean", GREEN),
    ]),
    ("Slurm: Scheduler queues", [
        ("slurm.sched.queue_length", BLUE), ("slurm.sched.agent_queue_size", ORANGE),
        ("slurm.sched.dbd_agent_queue_size", RED),
        ("slurm.sched.server_thread_count", GREEN),
    ]),
    ("Slurm: Backfill scheduler", [
        ("slurm.backfill.last_depth", BLUE), ("slurm.backfill.depth_mean", GREEN),
        ("slurm.backfill.queue_length", ORANGE), ("slurm.backfill.jobs.rate", TEAL),
    ]),
    ("Slurm: Job outcomes", [
        ("slurm.accounting.jobs.completed", GREEN),
        ("slurm.accounting.jobs.failed", RED),
        ("slurm.accounting.jobs.cancelled", GREY),
        ("slurm.accounting.jobs.timeout", ORANGE),
        ("slurm.accounting.jobs.node_fail", DARKRED),
        ("slurm.accounting.jobs.out_of_memory", PURPLE),
        ("slurm.accounting.jobs.preempted", OLIVE),
        ("slurm.accounting.jobs.other", PLUM),
    ]),
    ("Slurm: Job wait and runtime", [
        ("slurm.accounting.wait.mean", BLUE), ("slurm.accounting.wait.max", LIGHTBLUE),
        ("slurm.accounting.elapsed.mean", GREEN), ("slurm.accounting.elapsed.max", SAGE),
    ]),
]

GRAPH_PROTOTYPES = [
    ("slurm.partitions.discovery", "Partition [{#PARTITION}]: CPU allocation", [
        ("slurm.partition.cpus.allocated[{#PARTITION}]", BLUE),
        ("slurm.partition.cpus.idle[{#PARTITION}]", GREEN),
        ("slurm.partition.cpus.other[{#PARTITION}]", RED),
    ]),
    ("slurm.partitions.discovery", "Partition [{#PARTITION}]: Jobs", [
        ("slurm.partition.jobs.running[{#PARTITION}]", GREEN),
        ("slurm.partition.jobs.pending[{#PARTITION}]", ORANGE),
    ]),
    ("slurm.partitions.discovery", "Partition [{#PARTITION}]: Nodes", [
        ("slurm.partition.nodes.available[{#PARTITION}]", GREEN),
        ("slurm.partition.nodes.down[{#PARTITION}]", RED),
        ("slurm.partition.nodes.drain[{#PARTITION}]", ORANGE),
    ]),
    ("slurm.licenses.discovery", "License [{#LICENSE}]: Usage", [
        ("slurm.license.used[{#LICENSE}]", BLUE),
        ("slurm.license.free[{#LICENSE}]", GREEN),
        ("slurm.license.total[{#LICENSE}]", GREY),
    ]),
    ("slurm.nodes.discovery", "Node [{#NODE}]: CPU", [
        ("slurm.node.cpus.allocated[{#NODE}]", BLUE),
        ("slurm.node.cpus.idle[{#NODE}]", GREEN),
        ("slurm.node.cpu.load[{#NODE}]", RED),
    ]),
    ("slurm.nodes.discovery", "Node [{#NODE}]: Memory", [
        ("slurm.node.memory.total[{#NODE}]", GREY),
        ("slurm.node.memory.allocated[{#NODE}]", BLUE),
        ("slurm.node.memory.free[{#NODE}]", GREEN),
    ]),
]

# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------

# Zabbix 7.0 dashboards use a 72 column grid, and widget fields that point at an
# object are indexed ("itemid.0", "graphid.0") because a widget may take several
# data sources.  Widgets that can broadcast to other widgets additionally carry a
# "reference" that has to be unique within the dashboard; the reference is filled
# in while rendering, see render_dashboards().

# Item value widget "Show" options.
SHOW_DESCRIPTION = "1"
SHOW_VALUE = "2"


def value_widget(name, key, x, y, width=12, height=3):
    return {"type": "item", "name": name, "x": x, "y": y, "width": width, "height": height,
            "fields": [("ITEM", "itemid.0", key),
                       ("INTEGER", "show.0", SHOW_DESCRIPTION),
                       ("INTEGER", "show.1", SHOW_VALUE)]}


def graph_widget(name, graph, x, y, width=36, height=6):
    return {"type": "graph", "name": name, "x": x, "y": y, "width": width, "height": height,
            "reference": True,
            "fields": [("GRAPH", "graphid.0", graph)]}


def graph_prototype_widget(name, graph, x, y, width=36, height=12, columns=1, rows=2):
    return {"type": "graphprototype", "name": name, "x": x, "y": y,
            "width": width, "height": height,
            "reference": True,
            "fields": [("GRAPH_PROTOTYPE", "graphid.0", graph),
                       ("INTEGER", "columns", str(columns)),
                       ("INTEGER", "rows", str(rows))]}


def reference_code(index):
    """Widget references are five letter codes: AAAAA, AAAAB, AAAAC..."""
    code = ""
    for _ in range(5):
        code = chr(ord("A") + index % 26) + code
        index //= 26
    return code


DASHBOARDS = [
    ("Slurm cluster overview", [
        ("Cluster", [
            value_widget("Nodes available", "slurm.nodes.available", 0, 0),
            value_widget("Nodes down", "slurm.nodes.down", 12, 0),
            value_widget("CPU allocation", "slurm.cpus.utilization", 24, 0),
            value_widget("GPU allocation", "slurm.gpus.utilization", 36, 0),
            value_widget("Jobs running", "slurm.jobs.running", 48, 0),
            value_widget("Jobs pending", "slurm.jobs.pending", 60, 0),
            graph_widget("Nodes by state", "Slurm: Nodes by state", 0, 3),
            graph_widget("Jobs by state", "Slurm: Jobs by state", 36, 3),
            graph_widget("CPU allocation", "Slurm: CPU allocation", 0, 9),
            graph_widget("Pending jobs by reason", "Slurm: Pending jobs by reason", 36, 9),
            graph_widget("Memory allocation", "Slurm: Memory allocation", 0, 15, width=24),
            graph_widget("GPU allocation", "Slurm: GPU allocation", 24, 15, width=24),
            graph_widget("Queue wait time", "Slurm: Queue wait time", 48, 15, width=24),
            # The graphs above are averaged over whatever period the dashboard
            # is showing; this reads the queue as it is right now, in Slurm's
            # own words.
            value_widget("Top pending reasons", "slurm.jobs.pending.top_reasons",
                         0, 21, width=72),
        ]),
        ("Scheduler", [
            value_widget("slurmctld", "slurm.ctld.available", 0, 0),
            value_widget("slurmdbd", "slurm.dbd.available", 12, 0),
            value_widget("Agent queue", "slurm.sched.agent_queue_size", 24, 0),
            value_widget("DBD agent queue", "slurm.sched.dbd_agent_queue_size", 36, 0),
            value_widget("Schedule queue length", "slurm.sched.queue_length", 48, 0),
            value_widget("Backfill depth", "slurm.backfill.last_depth", 60, 0),
            graph_widget("Scheduler cycle times", "Slurm: Scheduler cycle times", 0, 3),
            graph_widget("Backfill scheduler", "Slurm: Backfill scheduler", 36, 3),
            graph_widget("Scheduler queues", "Slurm: Scheduler queues", 0, 9),
            graph_widget("Job throughput", "Slurm: Job throughput", 36, 9),
            graph_widget("Queue size in CPUs", "Slurm: Queue size in CPUs", 0, 15),
            value_widget("Cluster", "slurm.cluster.name", 36, 15, width=18),
            value_widget("Slurm version", "slurm.cluster.version", 54, 15, width=18),
            value_widget("Data age", "slurm.data.age", 36, 18, width=18),
            value_widget("Collector errors", "slurm.collector.errors", 54, 18, width=18),
        ]),
        ("Partitions", [
            graph_prototype_widget("Partition CPU allocation",
                                   "Partition [{#PARTITION}]: CPU allocation", 0, 0),
            graph_prototype_widget("Partition jobs", "Partition [{#PARTITION}]: Jobs",
                                   36, 0),
            graph_prototype_widget("Partition nodes", "Partition [{#PARTITION}]: Nodes",
                                   0, 12, width=72, columns=2, rows=2),
        ]),
        ("Nodes", [
            graph_prototype_widget("Node CPU", "Node [{#NODE}]: CPU", 0, 0,
                                   columns=1, rows=3),
            graph_prototype_widget("Node memory", "Node [{#NODE}]: Memory", 36, 0,
                                   columns=1, rows=3),
        ]),
        ("Licenses and reservations", [
            value_widget("Licenses configured", "slurm.licenses.total", 0, 0),
            value_widget("Jobs waiting on licenses", "slurm.jobs.pending.licenses", 12, 0),
            value_widget("Reservations active", "slurm.reservations.active", 24, 0),
            value_widget("Reserved nodes", "slurm.reservations.nodes", 36, 0),
            value_widget("Reservations total", "slurm.reservations.total", 48, 0),
            value_widget("Longest node outage", "slurm.nodes.longest_unavailable_age",
                         60, 0),
            graph_prototype_widget("License usage", "License [{#LICENSE}]: Usage",
                                   0, 3, width=72, height=12, columns=2, rows=2),
        ]),
        ("Accounting", [
            value_widget("Jobs finished", "slurm.accounting.jobs.total", 0, 0),
            value_widget("Success rate", "slurm.accounting.success_rate", 12, 0),
            value_widget("Failure rate", "slurm.accounting.failure_rate", 24, 0),
            value_widget("CPU hours delivered", "slurm.accounting.cpu_hours", 36, 0),
            value_widget("Mean queue wait", "slurm.accounting.wait.mean", 48, 0),
            value_widget("Mean job runtime", "slurm.accounting.elapsed.mean", 60, 0),
            graph_widget("Job outcomes", "Slurm: Job outcomes", 0, 3),
            graph_widget("Job wait and runtime", "Slurm: Job wait and runtime", 36, 3),
        ]),
    ]),
]


# ---------------------------------------------------------------------------
# XML rendering
# ---------------------------------------------------------------------------


def sub(parent, tag, text=None):
    element = ET.SubElement(parent, tag)
    if text is not None:
        element.text = str(text)
    return element


def add_tags(parent, pairs):
    if not pairs:
        return
    container = sub(parent, "tags")
    for name, value in pairs:
        entry = sub(container, "tag")
        sub(entry, "tag", name)
        sub(entry, "value", value)


def add_preprocessing(parent, steps):
    if not steps:
        return
    container = sub(parent, "preprocessing")
    for step_type, parameters, error_handler in steps:
        step = sub(container, "step")
        sub(step, "type", step_type)
        if parameters:
            params = sub(step, "parameters")
            for parameter in parameters:
                sub(params, "parameter", parameter)
        if error_handler:
            sub(step, "error_handler", error_handler)


def item_preprocessing(path, rate=False, heartbeat=None):
    """JSONPath first, then optional rate conversion and change filtering."""
    steps = [("JSONPATH", [path], "DISCARD_VALUE")]
    if rate:
        steps.append(("CHANGE_PER_SECOND", None, "DISCARD_VALUE"))
    if heartbeat:
        steps.append(("DISCARD_UNCHANGED_HEARTBEAT", [heartbeat], None))
    return steps


def render_item(parent, definition, uuid_scope, tag="item"):
    element = sub(parent, tag)
    sub(element, "uuid", make_uuid(uuid_scope, definition["key"]))
    sub(element, "name", definition["name"])
    sub(element, "type", "DEPENDENT")
    sub(element, "key", definition["key"])
    sub(element, "history", definition["history"])
    sub(element, "trends", definition["trends"])
    sub(element, "value_type", definition["value_type"])
    if definition["units"]:
        sub(element, "units", definition["units"])
    if definition["description"]:
        sub(element, "description", definition["description"])
    if definition["valuemap"]:
        valuemap = sub(element, "valuemap")
        sub(valuemap, "name", definition["valuemap"])
    add_preprocessing(element, item_preprocessing(
        definition["path"], definition.get("rate", False), definition.get("heartbeat")))
    master = sub(element, "master_item")
    sub(master, "key", definition["master"])
    add_tags(element, [("component", definition["component"])])
    return element


def render_master_item(parent, definition):
    element = sub(parent, "item")
    sub(element, "uuid", make_uuid("item", definition["key"]))
    sub(element, "name", definition["name"])
    # No <type>: Zabbix agent is the default, and that is how the official
    # templates spell it.
    sub(element, "key", definition["key"])
    sub(element, "delay", definition["delay"])
    sub(element, "history", "0")
    sub(element, "trends", "0")
    sub(element, "value_type", "TEXT")
    if definition.get("status"):
        sub(element, "status", definition["status"])
    sub(element, "description", definition["description"])
    add_tags(element, [("component", "raw")])
    return element


def render_trigger(parent, definition, uuid_scope, tag_name="trigger"):
    element = sub(parent, tag_name)
    sub(element, "uuid", make_uuid(uuid_scope, definition["id"]))
    sub(element, "expression", definition["expression"])
    sub(element, "name", definition["name"])
    if definition.get("opdata"):
        sub(element, "opdata", definition["opdata"])
    sub(element, "priority", definition["priority"])
    if definition.get("description"):
        sub(element, "description", definition["description"])
    if definition.get("manual_close"):
        sub(element, "manual_close", "YES")
    return element


def render_dependencies(element, definition, index, tag_name="dependencies"):
    if not definition.get("depends"):
        return
    container = sub(element, tag_name)
    for dependency_id in definition["depends"]:
        target = index[dependency_id]
        entry = sub(container, "dependency")
        sub(entry, "name", target["name"])
        sub(entry, "expression", target["expression"])


def render_graph_items(parent, items):
    container = sub(parent, "graph_items")
    for sortorder, (key, color) in enumerate(items):
        entry = sub(container, "graph_item")
        sub(entry, "sortorder", sortorder)
        sub(entry, "color", color)
        reference = sub(entry, "item")
        sub(reference, "host", TEMPLATE)
        sub(reference, "key", key)


def build():
    root = ET.Element("zabbix_export")
    sub(root, "version", EXPORT_VERSION)

    groups = sub(root, "template_groups")
    group = sub(groups, "template_group")
    sub(group, "uuid", TEMPLATE_GROUP_UUID)
    sub(group, "name", TEMPLATE_GROUP)

    templates = sub(root, "templates")
    template = sub(templates, "template")
    sub(template, "uuid", make_uuid("template", TEMPLATE))
    sub(template, "template", TEMPLATE)
    sub(template, "name", TEMPLATE_NAME)
    sub(template, "description", TEMPLATE_DESCRIPTION)

    template_groups = sub(template, "groups")
    template_group = sub(template_groups, "group")
    sub(template_group, "name", TEMPLATE_GROUP)

    # -- items --------------------------------------------------------------
    items_element = sub(template, "items")
    for definition in MASTER_ITEMS:
        render_master_item(items_element, definition)
    for definition in CLUSTER_ITEMS + ACCOUNTING_ITEMS:
        render_item(items_element, definition, "item")

    # -- discovery rules ----------------------------------------------------
    rules_element = sub(template, "discovery_rules")
    for rule in DISCOVERY_RULES:
        rule_element = sub(rules_element, "discovery_rule")
        sub(rule_element, "uuid", make_uuid("lld", rule["key"]))
        sub(rule_element, "name", rule["name"])
        sub(rule_element, "type", "DEPENDENT")
        sub(rule_element, "key", rule["key"])
        sub(rule_element, "lifetime", rule.get("lifetime", "7d"))
        sub(rule_element, "description", rule["description"])

        prototypes = sub(rule_element, "item_prototypes")
        for definition in rule["items"]:
            prototype = dict(definition)
            prototype["path"] = rule["selector"] % definition["field"]
            prototype["component"] = definition["component"] or rule["component"]
            render_item(prototypes, prototype, "lld/%s" % rule["key"], "item_prototype")

        rule_triggers = rule.get("triggers") or []
        if rule_triggers:
            trigger_container = sub(rule_element, "trigger_prototypes")
            index = dict((trigger["id"], trigger) for trigger in rule_triggers)
            for definition in rule_triggers:
                element = render_trigger(trigger_container, definition,
                                         "lld/%s" % rule["key"], "trigger_prototype")
                render_dependencies(element, definition, index)
                add_tags(element, [("scope", definition["scope"])])

        rule_graphs = [entry for entry in GRAPH_PROTOTYPES if entry[0] == rule["key"]]
        if rule_graphs:
            graph_container = sub(rule_element, "graph_prototypes")
            for _, name, graph_items in rule_graphs:
                graph = sub(graph_container, "graph_prototype")
                sub(graph, "uuid", make_uuid("graph_prototype", name))
                sub(graph, "name", name)
                render_graph_items(graph, graph_items)

        master = sub(rule_element, "master_item")
        sub(master, "key", rule["master"])

        add_preprocessing(rule_element, [("JSONPATH", [rule["path"]], "DISCARD_VALUE")])

        macro_paths = sub(rule_element, "lld_macro_paths")
        for macro, path in rule["macros"]:
            entry = sub(macro_paths, "lld_macro_path")
            sub(entry, "lld_macro", macro)
            sub(entry, "path", path)

        filter_element = sub(rule_element, "filter")
        sub(filter_element, "evaltype", "AND")
        conditions = sub(filter_element, "conditions")
        for position, (macro, operator, value) in enumerate(rule["filters"]):
            condition = sub(conditions, "condition")
            sub(condition, "macro", macro)
            sub(condition, "value", value)
            sub(condition, "operator", operator)
            sub(condition, "formulaid", chr(ord("A") + position))

    # -- valuemaps ----------------------------------------------------------
    valuemaps = sub(template, "valuemaps")
    for name, mappings in VALUE_MAPS:
        valuemap = sub(valuemaps, "valuemap")
        sub(valuemap, "uuid", make_uuid("valuemap", name))
        sub(valuemap, "name", name)
        container = sub(valuemap, "mappings")
        for value, newvalue in mappings:
            mapping = sub(container, "mapping")
            sub(mapping, "value", value)
            sub(mapping, "newvalue", newvalue)

    # -- macros -------------------------------------------------------------
    macros = sub(template, "macros")
    for macro, value, description in MACROS:
        entry = sub(macros, "macro")
        sub(entry, "macro", macro)
        sub(entry, "value", value)
        sub(entry, "description", description)

    # -- dashboards ---------------------------------------------------------
    dashboards = sub(template, "dashboards")
    for dashboard_name, pages in DASHBOARDS:
        dashboard = sub(dashboards, "dashboard")
        sub(dashboard, "uuid", make_uuid("dashboard", dashboard_name))
        sub(dashboard, "name", dashboard_name)
        # Zabbix starts the slideshow by itself on a multi-page dashboard, which
        # rotates the pages away while somebody is reading one of them.
        sub(dashboard, "auto_start", "NO")
        pages_element = sub(dashboard, "pages")
        references = 0
        for page_name, widgets in pages:
            page = sub(pages_element, "page")
            sub(page, "name", page_name)
            widgets_element = sub(page, "widgets")
            for widget in widgets:
                widget_element = sub(widgets_element, "widget")
                sub(widget_element, "type", widget["type"])
                sub(widget_element, "name", widget["name"])
                sub(widget_element, "x", widget["x"])
                sub(widget_element, "y", widget["y"])
                sub(widget_element, "width", widget["width"])
                sub(widget_element, "height", widget["height"])
                widget_fields = list(widget["fields"])
                if widget.get("reference"):
                    widget_fields.append(("STRING", "reference", reference_code(references)))
                    references += 1
                fields = sub(widget_element, "fields")
                for field_type, field_name, field_value in widget_fields:
                    field = sub(fields, "field")
                    sub(field, "type", field_type)
                    sub(field, "name", field_name)
                    if field_type == "ITEM":
                        value = sub(field, "value")
                        sub(value, "host", TEMPLATE)
                        sub(value, "key", field_value)
                    elif field_type in ("GRAPH", "GRAPH_PROTOTYPE"):
                        value = sub(field, "value")
                        sub(value, "host", TEMPLATE)
                        sub(value, "name", field_value)
                    else:
                        sub(field, "value", field_value)

    # -- template level tags ------------------------------------------------
    add_tags(template, [("class", "software"), ("target", "slurm")])

    # -- triggers -----------------------------------------------------------
    triggers_element = sub(root, "triggers")
    index = dict((trigger["id"], trigger) for trigger in TRIGGERS)
    for definition in TRIGGERS:
        element = render_trigger(triggers_element, definition, "trigger")
        render_dependencies(element, definition, index)
        add_tags(element, [("scope", definition["scope"])])

    # -- graphs -------------------------------------------------------------
    graphs_element = sub(root, "graphs")
    for name, graph_items in GRAPHS:
        graph = sub(graphs_element, "graph")
        sub(graph, "uuid", make_uuid("graph", name))
        sub(graph, "name", name)
        if name in STACKED_GRAPHS:
            sub(graph, "type", "STACKED")
        render_graph_items(graph, graph_items)

    return root


def indent(element, level=0):
    padding = "\n" + "    " * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = padding + "    "
        for child in element:
            indent(child, level + 1)
        if not element.tail or not element.tail.strip():
            element.tail = padding
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = padding
    else:
        if level and (not element.tail or not element.tail.strip()):
            element.tail = padding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_output = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "slurm_cluster_7.0.xml")
    parser.add_argument("-o", "--output", default=default_output,
                        help="where to write the template (default: %s)" % default_output)
    args = parser.parse_args(argv)

    root = build()
    indent(root)
    tree = ET.ElementTree(root)

    directory = os.path.dirname(os.path.abspath(args.output))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(args.output, "wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(handle, encoding="utf-8", xml_declaration=False)
        handle.write(b"\n")

    sys.stderr.write("wrote %s\n" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
