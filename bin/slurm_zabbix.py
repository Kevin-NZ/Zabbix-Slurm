#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Slurm metrics collector for Zabbix.

Queries a Slurm cluster through the standard client commands (scontrol, squeue,
sdiag, sacctmgr) and emits a single JSON document that Zabbix consumes through
one master item per mode.  Every metric exposed by the Zabbix template is a
dependent item derived from that document, so a full poll of the template costs
at most two agent checks.

Design notes
------------
* Text output of the Slurm clients is parsed instead of ``--json``.  The JSON
  schema of Slurm changed repeatedly between 21.08 and 24.11, while the
  ``key=value`` output of ``scontrol --oneliner`` and the ``-o`` format strings
  of ``squeue`` have been stable for a decade.
* The collector never raises through to the caller.  Collection problems are
  reported in ``meta.errors``/``meta.error_count`` so that Zabbix keeps
  receiving a well-formed document and can alert on the error item itself.
* Results are cached on disk.  On busy clusters ``squeue`` can take seconds,
  which is longer than the default Zabbix agent timeout, so the recommended
  deployment refreshes the cache from a systemd timer and lets the agent read
  the cache with ``--cache-only``.

Exit status is 0 unless ``--strict`` is given.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - not available outside Unix
    fcntl = None

__version__ = "1.1.0"

MB = 1024 * 1024

DEFAULT_CACHE_FILE = "/var/tmp/zabbix_slurm_cache.json"
DEFAULT_CACHE_TTL = 55
DEFAULT_TIMEOUT = 25
DEFAULT_LOCK_TIMEOUT = 20
DEFAULT_ACCOUNTING_TTL = 870
DEFAULT_ACCOUNTING_WINDOW = 3600

# Terminal job states asked of sacct, and the buckets they are counted in.
SACCT_STATES = "CD,F,CA,TO,NF,OOM,PR"
JOB_STATE_BUCKETS = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "TIMEOUT": "timeout",
    "NODE_FAIL": "node_fail",
    "OUT_OF_MEMORY": "out_of_memory",
    "PREEMPTED": "preempted",
}
# Endings that mean the cluster or the job broke, as opposed to a user
# cancelling their own job.
FAILURE_BUCKETS = ("failed", "timeout", "node_fail", "out_of_memory")

# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

# Numeric node states.  Kept in sync with the "Slurm node state" value map of
# the Zabbix template; triggers compare against these codes.
NODE_STATE_UNKNOWN = 0
NODE_STATE_IDLE = 1
NODE_STATE_ALLOCATED = 2
NODE_STATE_MIXED = 3
NODE_STATE_DOWN = 4
NODE_STATE_DRAINED = 5
NODE_STATE_DRAINING = 6
NODE_STATE_FAIL = 7
NODE_STATE_MAINT = 8
NODE_STATE_RESERVED = 9
NODE_STATE_POWERED_DOWN = 10
NODE_STATE_FUTURE = 11
NODE_STATE_COMPLETING = 12
NODE_STATE_PLANNED = 13
NODE_STATE_REBOOT = 14

# Node states that still accept or run work.
NODE_STATES_AVAILABLE = (
    NODE_STATE_IDLE,
    NODE_STATE_ALLOCATED,
    NODE_STATE_MIXED,
    NODE_STATE_RESERVED,
    NODE_STATE_COMPLETING,
    NODE_STATE_PLANNED,
)

PARTITION_STATE_UNKNOWN = 0
PARTITION_STATE_UP = 1
PARTITION_STATE_DOWN = 2
PARTITION_STATE_DRAIN = 3
PARTITION_STATE_INACTIVE = 4

PARTITION_STATE_CODES = {
    "UP": PARTITION_STATE_UP,
    "DOWN": PARTITION_STATE_DOWN,
    "DRAIN": PARTITION_STATE_DRAIN,
    "INACTIVE": PARTITION_STATE_INACTIVE,
    "INACT": PARTITION_STATE_INACTIVE,
}

# Buckets used to summarise why jobs are pending.  Matched in order, first hit
# wins; anything unmatched lands in "other".
PENDING_REASON_BUCKETS = (
    ("resources", ("Resources", "Nodes required for job are DOWN")),
    ("priority", ("Priority", "Prolog", "SchedTimeout")),
    ("dependency", ("Dependency", "DependencyNeverSatisfied")),
    ("qos_limit", ("QOS",)),
    ("association_limit", ("Assoc", "Account", "MaxJobsPerAccount")),
    ("licenses", ("Licenses",)),
    ("reservation", ("Reservation", "ReqNodeNotAvail",)),
    ("partition", ("Partition",)),
    ("node_unavailable", ("Node", "BadConstraints", "ReqNodeNotAvail")),
    ("held", ("JobHeld", "BeginTime", "JobArrayTaskLimit", "PartitionTimeLimit")),
)

PENDING_REASON_KEYS = [name for name, _ in PENDING_REASON_BUCKETS] + ["other"]

# Reasons that mean a job is held back by policy or configuration rather than
# by something it is legitimately waiting for.  A job waiting on a dependency,
# a licence, a reservation window or a hold cannot start however much capacity
# is free, so counting it as "blocked" only produces false alarms; a job held
# back by a QOS, association or partition limit is worth looking at when the
# cluster has idle CPUs.
LIMIT_REASON_BUCKETS = ("qos_limit", "association_limit", "partition", "other")

# Reasons that mean the scheduler could not start the job even with the whole
# cluster free: it is waiting for another job, for a hold to be lifted, for a
# start time to arrive, or for a reservation window.  Time spent that way is not
# queue wait, so it is left out of the wait metrics; a job held for a week would
# otherwise report a week of "waiting" on a cluster that is running perfectly.
UNSCHEDULABLE_REASON_BUCKETS = ("dependency", "held", "reservation")


def is_schedulable(reason):
    """True when the job is waiting on the cluster rather than on something else."""
    return classify_pending_reason(reason) not in UNSCHEDULABLE_REASON_BUCKETS

# ---------------------------------------------------------------------------
# Generic parsing helpers
# ---------------------------------------------------------------------------

# ``scontrol --oneliner`` emits space separated ``Key=Value`` pairs where the
# value may itself contain spaces (OS=, Reason=) or equals signs (CfgTRES=).
# A value therefore runs up to the next whitespace-delimited key.
_KV_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_/.\-]*)=(?P<val>.*?)(?=\s+[A-Za-z_][A-Za-z0-9_/.\-]*=|$)")

_NULL_VALUES = frozenset(["", "N/A", "n/a", "(null)", "None", "NONE", "Unknown", "UNKNOWN", "unknown"])

# Typed GRES appears in TRES as "gres/gpu:a100=3", so ':' belongs to the name.
_TRES_RE = re.compile(r"([A-Za-z0-9_/:]+)=([0-9.]+)([KMGTP]?)")

_SIZE_MULTIPLIERS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}


def parse_kv_line(line):
    """Parse one ``scontrol --oneliner`` record into a dict."""
    return dict((m.group("key"), m.group("val").strip()) for m in _KV_RE.finditer(line))


def to_int(value, default=None):
    if value is None:
        return default
    text = str(value).strip()
    if text in _NULL_VALUES or text in ("UNLIMITED", "INFINITE"):
        return default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def to_float(value, default=None):
    if value is None:
        return default
    text = str(value).strip()
    if text in _NULL_VALUES or text in ("UNLIMITED", "INFINITE"):
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


# Gres/GresUsed entries look like "gpu:4", "gpu:a100:4(S:0-1)" or
# "gpu:a100:3(IDX:0-2)", comma separated, and may mention other resources such
# as mps or shard.
_GRES_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_]*)"
    r"(?::(?P<type>[A-Za-z0-9_.+\-]+))?"
    r":(?P<count>\d+)"
    r"(?:\([^)]*\))?")


def parse_gres(value):
    """Parse a Gres or GresUsed field into counts.

    Returns the total per resource plus a per type breakdown, for example
    ``{"gpu": 6, "gpu:a100": 4, "gpu:v100": 2}``.

    Unlike the TRES fields, Gres and GresUsed are always populated, so this is
    the only GPU source on clusters that do not list gres/gpu in
    AccountingStorageTRES - which is the default.
    """
    result = {}
    if not value or value in _NULL_VALUES:
        return result
    for match in _GRES_RE.finditer(value):
        name = match.group("name").lower()
        kind = (match.group("type") or "").lower()
        count = int(match.group("count"))
        result[name] = result.get(name, 0) + count
        if kind:
            key = "%s:%s" % (name, kind)
            result[key] = result.get(key, 0) + count
    return result


def tres_gpu_count(tres):
    """GPUs in a parsed TRES mapping.

    A cluster may track the generic ``gres/gpu``, only the typed
    ``gres/gpu:a100`` entries, or neither.
    """
    if "gres/gpu" in tres:
        return int(tres["gres/gpu"])
    return int(sum(count for name, count in tres.items()
                   if name.startswith("gres/gpu:")))


def parse_tres(value):
    """Parse a TRES string such as ``cpu=32,mem=250G,gres/gpu=2``.

    Memory is normalised to bytes, every other resource is returned as a plain
    number.
    """
    result = {}
    if not value or value in _NULL_VALUES:
        return result
    for name, amount, suffix in _TRES_RE.findall(value):
        try:
            number = float(amount)
        except ValueError:
            continue
        if name == "mem":
            # Slurm reports memory in megabytes when no suffix is given.
            number = number * _SIZE_MULTIPLIERS.get(suffix, 1) if suffix else number * MB
        elif suffix:
            number = number * _SIZE_MULTIPLIERS.get(suffix, 1)
        result[name] = number
    return result


# Slurm annotates a drain/down reason with who set it and when:
#   "hardware maintenance scheduled [root@2024-05-03T02:15:00]"
_REASON_ANNOTATION_RE = re.compile(
    r"\[([^@\[\]]+)@(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]\s*$")


def parse_reason_annotation(reason):
    """Split a node reason into (text, user, epoch).

    Older Slurm releases and manually set reasons may carry no annotation, in
    which case the user is empty and the timestamp is None.
    """
    if not reason:
        return "", "", None
    match = _REASON_ANNOTATION_RE.search(reason)
    if not match:
        return reason, "", None
    text = reason[:match.start()].strip()
    return text, match.group(1).strip(), parse_slurm_datetime(match.group(2))


def parse_slurm_datetime(value):
    """Convert a Slurm timestamp (``2024-05-05T12:00:00``) to epoch seconds."""
    if not value or value in _NULL_VALUES:
        return None
    try:
        parsed = time.strptime(value.strip(), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return int(time.mktime(parsed))


def parse_slurm_duration(value):
    """Convert ``[[days-]hours:]minutes:seconds`` to seconds."""
    if not value or value in _NULL_VALUES:
        return None
    text = value.strip()
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = int(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return int(days * 86400 + seconds)


def clean(value):
    """Normalise Slurm's placeholders for "nothing" to an empty string."""
    return "" if value in _NULL_VALUES else value


def percent(part, total):
    """Percentage of ``part`` in ``total``, guarding against a zero total."""
    if not total:
        return 0.0
    return round(100.0 * part / float(total), 4)


def classify_pending_reason(reason):
    if not reason or reason in _NULL_VALUES:
        return "other"
    for bucket, prefixes in PENDING_REASON_BUCKETS:
        for prefix in prefixes:
            if reason.startswith(prefix):
                return bucket
    return "other"


def node_state_code(base, flags):
    """Map a Slurm node state to the numeric code used by the template."""
    flags = set(flags)
    if base == "DOWN":
        return NODE_STATE_DOWN
    if base in ("FAIL", "FAILING", "ERROR", "ERR"):
        return NODE_STATE_FAIL
    if "MAINT" in flags:
        return NODE_STATE_MAINT
    if "DRAIN" in flags or base in ("DRAINED", "DRAINING", "DRAIN"):
        # A drained node is idle-and-unusable, a draining node still runs jobs.
        if base in ("ALLOCATED", "MIXED", "COMPLETING", "DRAINING"):
            return NODE_STATE_DRAINING
        return NODE_STATE_DRAINED
    if base in ("POWERED_DOWN", "POWER_DOWN", "POWERING_DOWN", "POWERING_UP", "POWER_UP"):
        return NODE_STATE_POWERED_DOWN
    if flags & set(["POWERED_DOWN", "POWER_DOWN", "POWERING_DOWN"]):
        return NODE_STATE_POWERED_DOWN
    if base == "FUTURE" or "FUTURE" in flags:
        return NODE_STATE_FUTURE
    if "REBOOT_REQUESTED" in flags or "REBOOT_ISSUED" in flags or base == "REBOOT":
        return NODE_STATE_REBOOT
    if "RESERVED" in flags or base == "RESERVED":
        return NODE_STATE_RESERVED
    if "COMPLETING" in flags or base == "COMPLETING":
        return NODE_STATE_COMPLETING
    if base == "PLANNED" or "PLANNED" in flags:
        return NODE_STATE_PLANNED
    if base == "IDLE":
        return NODE_STATE_IDLE
    if base == "ALLOCATED" or base == "ALLOC":
        return NODE_STATE_ALLOCATED
    if base == "MIXED":
        return NODE_STATE_MIXED
    return NODE_STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class SlurmCollector(object):
    """Runs the Slurm client commands and assembles the metric document."""

    def __init__(self, bin_dir=None, timeout=DEFAULT_TIMEOUT, enable_sacctmgr=True):
        self.bin_dir = bin_dir
        self.timeout = timeout
        self.enable_sacctmgr = enable_sacctmgr
        self.errors = []
        self.now = int(time.time())

    # -- command execution ---------------------------------------------------

    def _resolve(self, command):
        if self.bin_dir:
            return os.path.join(self.bin_dir, command)
        return command

    def run(self, args, ignore_errors=False):
        """Run a Slurm client command and return its stdout, or None."""
        argv = [self._resolve(args[0])] + list(args[1:])
        env = os.environ.copy()
        env.setdefault("SLURM_TIME_FORMAT", "standard")
        env["LC_ALL"] = "C"
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                universal_newlines=True,
            )
        except OSError as exc:
            if not ignore_errors:
                self.error("%s: %s" % (args[0], exc.strerror or str(exc)))
            return None

        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            if not ignore_errors:
                self.error("%s: timed out after %ss" % (args[0], self.timeout))
            return None

        if process.returncode != 0:
            if not ignore_errors:
                message = (stderr or "").strip().splitlines()
                self.error(
                    "%s: exit %s%s"
                    % (args[0], process.returncode, ": " + message[0] if message else "")
                )
            return None
        return stdout

    def error(self, message):
        if message not in self.errors:
            self.errors.append(message)

    # -- individual sources --------------------------------------------------

    def collect_config(self):
        """``scontrol show config`` -> selected controller settings."""
        config = {
            "cluster_name": "",
            "version": "",
            "scheduler_type": "",
            "select_type": "",
            "max_job_count": None,
            "controller": "",
        }
        output = self.run(["scontrol", "show", "config"])
        if output is None:
            return config
        raw = {}
        for line in output.splitlines():
            if " = " not in line:
                continue
            key, _, value = line.partition(" = ")
            raw[key.strip()] = value.strip()
        config["cluster_name"] = raw.get("ClusterName", "")
        config["version"] = raw.get("SLURM_VERSION", raw.get("SLURM_VERSION_STRING", ""))
        config["scheduler_type"] = raw.get("SchedulerType", "")
        config["select_type"] = raw.get("SelectType", "")
        config["max_job_count"] = to_int(raw.get("MaxJobCount"))
        config["controller"] = raw.get("SlurmctldHost[0]", raw.get("ControlMachine", ""))
        return config

    def collect_ping(self):
        """``scontrol ping`` -> availability of the primary/backup controllers."""
        result = {"slurmctld_up": 0, "slurmctld_backup_up": 0, "slurmctld_hosts": 0}
        output = self.run(["scontrol", "ping"])
        if output is None:
            return result
        # Slurmctld(primary) at head01 is UP
        # Slurmctld(primary/backup) at head01/head02 are UP/DOWN
        for line in output.splitlines():
            line = line.strip()
            if not line.lower().startswith("slurmctld"):
                continue
            roles = re.search(r"\(([^)]*)\)", line)
            states = line.rsplit(" ", 1)[-1]
            role_list = roles.group(1).split("/") if roles else ["primary"]
            state_list = states.split("/")
            for index, role in enumerate(role_list):
                up = 1 if index < len(state_list) and state_list[index].upper() == "UP" else 0
                result["slurmctld_hosts"] += 1
                if role.strip().lower() == "backup":
                    result["slurmctld_backup_up"] = up
                else:
                    result["slurmctld_up"] = up
        return result

    def collect_dbd(self):
        """Availability of slurmdbd, checked through sacctmgr."""
        if not self.enable_sacctmgr:
            return {"slurmdbd_up": None}
        output = self.run(["sacctmgr", "-n", "-P", "show", "cluster", "format=Cluster"],
                          ignore_errors=True)
        if output is None:
            return {"slurmdbd_up": 0}
        return {"slurmdbd_up": 1}

    def collect_nodes(self):
        """``scontrol show node --oneliner`` -> per node metrics."""
        nodes = []
        output = self.run(["scontrol", "show", "node", "--oneliner"])
        if output is None:
            return nodes
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("NodeName="):
                continue
            record = parse_kv_line(line)
            node = self._build_node(record)
            if node is not None:
                nodes.append(node)
        return nodes

    def _build_node(self, record):
        name = record.get("NodeName")
        if not name:
            return None

        raw_state = record.get("State", "UNKNOWN")
        state_parts = raw_state.replace("*", "").split("+")
        base_state = state_parts[0].upper() if state_parts else "UNKNOWN"
        flags = [part.upper() for part in state_parts[1:]]
        # sinfo marks an unresponsive node with a trailing "*", scontrol reports
        # it as a NOT_RESPONDING state flag; both spellings occur in the wild.
        not_responding = 1 if ("*" in raw_state or "NOT_RESPONDING" in flags) else 0
        code = node_state_code(base_state, flags)

        cpus_total = to_int(record.get("CPUTot"), 0) or 0
        cpus_alloc = to_int(record.get("CPUAlloc"), 0) or 0
        cpus_idle = max(cpus_total - cpus_alloc, 0)

        memory_total = (to_int(record.get("RealMemory"), 0) or 0) * MB
        memory_alloc = (to_int(record.get("AllocMem"), 0) or 0) * MB
        memory_free_mb = to_int(record.get("FreeMem"))
        memory_free = memory_free_mb * MB if memory_free_mb is not None else None

        cfg_tres = parse_tres(record.get("CfgTRES", ""))
        alloc_tres = parse_tres(record.get("AllocTRES", ""))
        # gres/gpu is only present in the TRES fields when the cluster lists it
        # in AccountingStorageTRES, which is not the default.  Gres and GresUsed
        # are always populated, so they are the fallback.
        configured_gres = parse_gres(record.get("Gres", ""))
        used_gres = parse_gres(record.get("GresUsed", ""))
        # Slurm describes GPUs twice and either description can be absent or
        # zero depending on the release and on AccountingStorageTRES, so both
        # are read and the larger is kept.  Preferring one source meant a zero
        # from it hid a correct count in the other.
        gpus_total = max(tres_gpu_count(cfg_tres), configured_gres.get("gpu", 0))
        gpus_alloc = max(tres_gpu_count(alloc_tres), used_gres.get("gpu", 0))
        gpu_types = ",".join(sorted(key.split(":", 1)[1] for key in configured_gres
                                    if key.startswith("gpu:")))

        load = to_float(record.get("CPULoad"))
        boot_time = parse_slurm_datetime(record.get("BootTime"))
        slurmd_start = parse_slurm_datetime(record.get("SlurmdStartTime"))

        reason = record.get("Reason", "")
        if reason in _NULL_VALUES:
            reason = ""
        reason_text, reason_user, reason_time = parse_reason_annotation(reason)

        partitions = record.get("Partitions", "")
        if partitions in _NULL_VALUES:
            partitions = ""

        available = 1 if (code in NODE_STATES_AVAILABLE and not not_responding) else 0

        node = {
            "name": name,
            "address": record.get("NodeAddr", name),
            "hostname": record.get("NodeHostName", name),
            "partitions": partitions,
            "state": raw_state,
            "state_base": base_state,
            "state_flags": "+".join(flags),
            "state_code": code,
            "available": available,
            "not_responding": not_responding,
            "drain": 1 if code in (NODE_STATE_DRAINED, NODE_STATE_DRAINING) else 0,
            "maint": 1 if code == NODE_STATE_MAINT else 0,
            "cpus_total": cpus_total,
            "cpus_allocated": cpus_alloc,
            "cpus_idle": cpus_idle,
            "cpu_utilization": percent(cpus_alloc, cpus_total),
            "cpu_load": load,
            "cpu_load_per_core": round(load / cpus_total, 4) if load is not None and cpus_total else None,
            "memory_total_bytes": memory_total,
            "memory_allocated_bytes": memory_alloc,
            "memory_free_bytes": memory_free,
            "memory_utilization": percent(memory_alloc, memory_total),
            "memory_free_pct": percent(memory_free, memory_total) if memory_free is not None else None,
            "tmp_disk_bytes": (to_int(record.get("TmpDisk"), 0) or 0) * MB,
            "gpus_total": gpus_total,
            "gpus_allocated": gpus_alloc,
            "gpus_idle": max(gpus_total - gpus_alloc, 0),
            "gpu_utilization": percent(gpus_alloc, gpus_total),
            "gpu_type": gpu_types,
            "weight": to_int(record.get("Weight"), 0) or 0,
            "version": record.get("Version", ""),
            "reason": reason,
            "reason_text": reason_text,
            "reason_user": reason_user,
            "reason_time": reason_time,
            # How long the node has been out of service.  Reported as 0 while
            # the node is usable, so that a node returning to service resets
            # the metric instead of keeping the age of an old drain reason.
            "unavailable_age": (self.now - reason_time
                                if reason_time and not available else 0),
            "boot_time": boot_time,
            "uptime": self.now - boot_time if boot_time else None,
            "slurmd_start_time": slurmd_start,
        }
        return node

    def collect_partitions(self):
        """``scontrol show partition --oneliner`` -> partition configuration."""
        partitions = []
        output = self.run(["scontrol", "show", "partition", "--oneliner"])
        if output is None:
            return partitions
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("PartitionName="):
                continue
            record = parse_kv_line(line)
            name = record.get("PartitionName")
            if not name:
                continue
            state = record.get("State", "UNKNOWN").upper()
            partitions.append({
                "name": name,
                "state": state,
                "state_code": PARTITION_STATE_CODES.get(state, PARTITION_STATE_UNKNOWN),
                "default": 1 if record.get("Default", "NO").upper() == "YES" else 0,
                "hidden": 1 if record.get("Hidden", "NO").upper() == "YES" else 0,
                "max_time": record.get("MaxTime", ""),
                "max_time_seconds": parse_slurm_duration(record.get("MaxTime", "")),
                "priority_tier": to_int(record.get("PriorityTier"), 0) or 0,
                "total_nodes_configured": to_int(record.get("TotalNodes"), 0) or 0,
                "total_cpus_configured": to_int(record.get("TotalCPUs"), 0) or 0,
            })
        return partitions

    def collect_jobs(self):
        """``squeue`` -> one record per active job."""
        jobs = []
        fmt = "%i|%P|%T|%r|%C|%D|%u|%a|%q|%V|%M|%N"
        output = self.run(["squeue", "-h", "-a", "-o", fmt])
        if output is None:
            return jobs
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            fields = line.split("|")
            if len(fields) < 12:
                continue
            submit_time = parse_slurm_datetime(fields[9])
            jobs.append({
                "id": fields[0],
                "partition": fields[1],
                "state": fields[2].upper(),
                "reason": fields[3],
                "cpus": to_int(fields[4], 0) or 0,
                "nodes": to_int(fields[5], 0) or 0,
                "user": fields[6],
                "account": fields[7],
                "qos": fields[8],
                "submit_time": submit_time,
                "pending_age": max(self.now - submit_time, 0) if submit_time else None,
                "elapsed": parse_slurm_duration(fields[10]) or 0,
            })
        return jobs

    def collect_sdiag(self):
        """``sdiag`` -> scheduler and backfill statistics."""
        stats = {
            "server_thread_count": None,
            "agent_queue_size": None,
            "agent_count": None,
            "agent_thread_count": None,
            "dbd_agent_queue_size": None,
            "jobs_submitted": None,
            "jobs_started": None,
            "jobs_completed": None,
            "jobs_canceled": None,
            "jobs_failed": None,
            "schedule_cycle_last": None,
            "schedule_cycle_max": None,
            "schedule_cycle_mean": None,
            "schedule_cycles_per_minute": None,
            "schedule_queue_length": None,
            "schedule_depth_mean": None,
            "schedule_total_cycles": None,
            "backfill_cycle_last": None,
            "backfill_cycle_max": None,
            "backfill_cycle_mean": None,
            "backfill_depth_mean": None,
            "backfill_last_depth": None,
            "backfill_queue_length": None,
            "backfill_total_cycles": None,
            "backfilled_jobs": None,
            "backfill_last_cycle_age": None,
        }
        output = self.run(["sdiag"])
        if output is None:
            return stats

        # "Last cycle"/"Mean cycle" appear in both the main scheduler and the
        # backfill section, so the current section has to be tracked.
        section = "main"
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if lowered.startswith("main schedule statistics"):
                section = "main"
                continue
            if lowered.startswith("backfilling stats"):
                section = "backfill"
                continue
            if lowered.startswith("remote procedure call statistics") or lowered.startswith("pending rpc"):
                section = "rpc"
                continue
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if section == "rpc":
                continue

            if key == "server thread count":
                stats["server_thread_count"] = to_int(value)
            elif key == "agent queue size":
                stats["agent_queue_size"] = to_int(value)
            elif key == "agent count":
                stats["agent_count"] = to_int(value)
            elif key == "agent thread count":
                stats["agent_thread_count"] = to_int(value)
            elif key == "dbd agent queue size":
                stats["dbd_agent_queue_size"] = to_int(value)
            elif key == "jobs submitted":
                stats["jobs_submitted"] = to_int(value)
            elif key == "jobs started":
                stats["jobs_started"] = to_int(value)
            elif key == "jobs completed":
                stats["jobs_completed"] = to_int(value)
            elif key == "jobs canceled":
                stats["jobs_canceled"] = to_int(value)
            elif key == "jobs failed":
                stats["jobs_failed"] = to_int(value)
            elif section == "main":
                if key == "last cycle":
                    stats["schedule_cycle_last"] = self._microseconds(value)
                elif key == "max cycle":
                    stats["schedule_cycle_max"] = self._microseconds(value)
                elif key == "mean cycle":
                    stats["schedule_cycle_mean"] = self._microseconds(value)
                elif key == "total cycles":
                    stats["schedule_total_cycles"] = to_int(value)
                elif key == "cycles per minute":
                    stats["schedule_cycles_per_minute"] = to_int(value)
                elif key == "last queue length":
                    stats["schedule_queue_length"] = to_int(value)
                elif key == "mean depth cycle":
                    stats["schedule_depth_mean"] = to_int(value)
            elif section == "backfill":
                if key == "last cycle":
                    stats["backfill_cycle_last"] = self._microseconds(value)
                elif key == "max cycle":
                    stats["backfill_cycle_max"] = self._microseconds(value)
                elif key == "mean cycle":
                    stats["backfill_cycle_mean"] = self._microseconds(value)
                elif key == "total cycles":
                    stats["backfill_total_cycles"] = to_int(value)
                elif key == "depth mean":
                    stats["backfill_depth_mean"] = to_int(value)
                elif key == "last depth cycle":
                    stats["backfill_last_depth"] = to_int(value)
                elif key == "last queue length":
                    stats["backfill_queue_length"] = to_int(value)
                elif key.startswith("total backfilled jobs (since last slurm start"):
                    stats["backfilled_jobs"] = to_int(value)
                elif key == "last cycle when":
                    # "Tue May 05 12:00:00 2024" or an epoch in older releases.
                    stats["backfill_last_cycle_age"] = self._cycle_age(value)
        return stats

    @staticmethod
    def _microseconds(value):
        number = to_float(value)
        if number is None:
            return None
        return round(number / 1000000.0, 6)

    def _cycle_age(self, value):
        """Seconds since the timestamp reported by ``Last cycle when``.

        Slurm prints either a bare epoch, a human readable date, or a date
        followed by the epoch in parentheses, depending on the release.
        """
        text = value.strip()
        parenthesised = re.search(r"\((\d{9,})\)", text)
        if parenthesised:
            return max(self.now - int(parenthesised.group(1)), 0)
        epoch = to_int(text)
        if epoch is not None and epoch > 1000000000:
            return max(self.now - epoch, 0)
        for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                return max(self.now - int(time.mktime(time.strptime(text, fmt))), 0)
            except ValueError:
                continue
        return None

    def collect_qos(self):
        """``sacctmgr show qos`` -> configured QOS names and limits."""
        qos_list = []
        if not self.enable_sacctmgr:
            return qos_list
        output = self.run(
            ["sacctmgr", "-n", "-P", "show", "qos", "format=Name,Priority,GrpTRES,MaxTRESPU,GrpJobs"],
            ignore_errors=True,
        )
        if output is None:
            return qos_list
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            fields = line.split("|")
            name = fields[0].strip()
            if not name:
                continue
            grp_tres = parse_tres(fields[2] if len(fields) > 2 else "")
            qos_list.append({
                "name": name,
                "priority": to_int(fields[1] if len(fields) > 1 else None, 0) or 0,
                "grp_cpus": int(grp_tres.get("cpu", 0)),
                "grp_jobs": to_int(fields[4] if len(fields) > 4 else None, 0) or 0,
            })
        return qos_list

    def collect_reservations(self):
        """``scontrol show reservation`` -> per reservation detail and totals."""
        summary = {"reservations_total": 0, "reservations_active": 0, "reservations_nodes": 0}
        reservations = []
        output = self.run(["scontrol", "show", "reservation", "--oneliner"], ignore_errors=True)
        if output is None:
            return reservations, summary

        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("ReservationName="):
                continue
            record = parse_kv_line(line)
            name = record.get("ReservationName")
            if not name:
                continue

            state = record.get("State", "").upper()
            start = parse_slurm_datetime(record.get("StartTime", ""))
            end = parse_slurm_datetime(record.get("EndTime", ""))
            active = 1 if (state == "ACTIVE" or (
                start is not None and end is not None and start <= self.now <= end)) else 0
            nodes = to_int(record.get("NodeCnt"), 0) or 0

            summary["reservations_total"] += 1
            if active:
                summary["reservations_active"] += 1
                summary["reservations_nodes"] += nodes

            reservations.append({
                "name": name,
                "state": state or ("ACTIVE" if active else "INACTIVE"),
                "active": active,
                "nodes": nodes,
                "cores": to_int(record.get("CoreCnt"), 0) or 0,
                "partition": clean(record.get("PartitionName", "")),
                "users": clean(record.get("Users", "")),
                "accounts": clean(record.get("Accounts", "")),
                "flags": clean(record.get("Flags", "")),
                "maintenance": 1 if "MAINT" in record.get("Flags", "").upper() else 0,
                "start_time": start,
                "end_time": end,
                # Negative before the reservation starts, so one item shows both
                # "starts in" and "ends in".
                "starts_in": max(start - self.now, 0) if start else 0,
                "remaining": max(end - self.now, 0) if end else 0,
                "duration": (end - start) if (start and end) else 0,
            })
        return reservations, summary

    def collect_licenses(self):
        """``scontrol show licenses`` -> license pool usage.

        Clusters that configure no licenses simply produce no output.
        """
        licenses = []
        output = self.run(["scontrol", "show", "licenses", "--oneliner"], ignore_errors=True)
        if output is None:
            return licenses
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("LicenseName="):
                continue
            record = parse_kv_line(line)
            name = record.get("LicenseName")
            if not name:
                continue
            total = to_int(record.get("Total"), 0) or 0
            used = to_int(record.get("Used"), 0) or 0
            free = to_int(record.get("Free"))
            if free is None:
                free = max(total - used, 0)
            licenses.append({
                "name": name,
                "total": total,
                "used": used,
                "free": free,
                "reserved": to_int(record.get("Reserved"), 0) or 0,
                "remote": 1 if record.get("Remote", "no").lower() == "yes" else 0,
                "utilization": percent(used, total),
            })
        return licenses

    def collect_accounting(self, window=DEFAULT_ACCOUNTING_WINDOW):
        """``sacct`` -> throughput of the jobs that finished in the last window.

        This is the only collection that reads the accounting database, and it
        is the expensive one on a busy cluster, which is why it lives behind its
        own mode and its own cache.
        """
        stats = dict((key, 0) for key in
                     ["jobs_total", "cpu_hours", "wait_mean", "wait_max",
                      "elapsed_mean", "elapsed_max"])
        for bucket in sorted(set(JOB_STATE_BUCKETS.values())) + ["other"]:
            stats["jobs_" + bucket] = 0
        stats["window"] = window
        stats["success_rate"] = 0.0
        stats["failure_rate"] = 0.0

        start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.now - window))
        output = self.run([
            "sacct", "-a", "-X", "-n", "-P",
            "-S", start, "-E", "now",
            "-s", SACCT_STATES,
            "-o", "JobID,State,Submit,Start,Elapsed,AllocCPUS,Partition",
        ])
        if output is None:
            return stats

        waits = []
        elapsed_times = []
        for line in output.splitlines():
            fields = line.strip().split("|")
            if len(fields) < 7:
                continue
            stats["jobs_total"] += 1

            # "CANCELLED by 1000" carries the cancelling user id.
            state = fields[1].split(" ")[0].upper()
            stats["jobs_" + JOB_STATE_BUCKETS.get(state, "other")] += 1

            submit = parse_slurm_datetime(fields[2])
            began = parse_slurm_datetime(fields[3])
            if submit is not None and began is not None and began >= submit:
                waits.append(began - submit)

            elapsed = parse_slurm_duration(fields[4]) or 0
            elapsed_times.append(elapsed)
            stats["cpu_hours"] += elapsed * (to_int(fields[5], 0) or 0) / 3600.0

        if waits:
            stats["wait_mean"] = int(sum(waits) / len(waits))
            stats["wait_max"] = max(waits)
        if elapsed_times:
            stats["elapsed_mean"] = int(sum(elapsed_times) / len(elapsed_times))
            stats["elapsed_max"] = max(elapsed_times)

        total = stats["jobs_total"]
        stats["cpu_hours"] = round(stats["cpu_hours"], 3)
        stats["success_rate"] = percent(stats["jobs_completed"], total)
        stats["failure_rate"] = percent(
            sum(stats["jobs_" + bucket] for bucket in FAILURE_BUCKETS), total)
        return stats

    # -- aggregation ---------------------------------------------------------

    def summarise_nodes(self, nodes):
        summary = dict((key, 0) for key in (
            "total", "idle", "allocated", "mixed", "down", "drained", "draining", "drain",
            "fail", "maint", "reserved", "powered_down", "future", "completing", "planned",
            "reboot", "unknown", "not_responding", "available", "unavailable",
        ))
        cpus = {"total": 0, "allocated": 0, "idle": 0, "other": 0}
        memory = {"total_bytes": 0, "allocated_bytes": 0, "free_bytes": 0}
        gpus = {"total": 0, "allocated": 0}

        by_code = {
            NODE_STATE_IDLE: "idle",
            NODE_STATE_ALLOCATED: "allocated",
            NODE_STATE_MIXED: "mixed",
            NODE_STATE_DOWN: "down",
            NODE_STATE_DRAINED: "drained",
            NODE_STATE_DRAINING: "draining",
            NODE_STATE_FAIL: "fail",
            NODE_STATE_MAINT: "maint",
            NODE_STATE_RESERVED: "reserved",
            NODE_STATE_POWERED_DOWN: "powered_down",
            NODE_STATE_FUTURE: "future",
            NODE_STATE_COMPLETING: "completing",
            NODE_STATE_PLANNED: "planned",
            NODE_STATE_REBOOT: "reboot",
            NODE_STATE_UNKNOWN: "unknown",
        }

        for node in nodes:
            summary["total"] += 1
            summary[by_code.get(node["state_code"], "unknown")] += 1
            if node["drain"]:
                summary["drain"] += 1
            if node["not_responding"]:
                summary["not_responding"] += 1
            if node["available"]:
                summary["available"] += 1
            else:
                summary["unavailable"] += 1

            cpus["total"] += node["cpus_total"]
            if node["available"]:
                cpus["allocated"] += node["cpus_allocated"]
                cpus["idle"] += node["cpus_idle"]
            else:
                # CPUs on unusable nodes count as "other", matching sinfo's
                # A/I/O/T accounting.
                cpus["allocated"] += node["cpus_allocated"]
                cpus["other"] += node["cpus_idle"]

            memory["total_bytes"] += node["memory_total_bytes"]
            memory["allocated_bytes"] += node["memory_allocated_bytes"]
            if node["memory_free_bytes"] is not None:
                memory["free_bytes"] += node["memory_free_bytes"]
            gpus["total"] += node["gpus_total"]
            gpus["allocated"] += node["gpus_allocated"]

        summary["longest_unavailable_age"] = max(
            [node["unavailable_age"] for node in nodes] or [0])
        cpus["utilization"] = percent(cpus["allocated"], cpus["total"])
        memory["utilization"] = percent(memory["allocated_bytes"], memory["total_bytes"])
        gpus["idle"] = max(gpus["total"] - gpus["allocated"], 0)
        gpus["utilization"] = percent(gpus["allocated"], gpus["total"])
        summary["availability"] = percent(summary["available"], summary["total"])
        return summary, cpus, memory, gpus

    def summarise_jobs(self, jobs, max_job_count):
        summary = {
            "total": 0,
            "running": 0,
            "pending": 0,
            "suspended": 0,
            "completing": 0,
            "configuring": 0,
            "other": 0,
            "cpus_running": 0,
            "cpus_pending": 0,
            "nodes_running": 0,
            "users_active": 0,
            "accounts_active": 0,
            "oldest_pending_age": 0,
            "mean_pending_age": 0,
            "longest_running_age": 0,
            "array_pending": 0,
        }
        for key in PENDING_REASON_KEYS:
            summary["pending_" + key] = 0

        users = set()
        accounts = set()
        pending_ages = []
        reason_counter = {}

        for job in jobs:
            summary["total"] += 1
            state = job["state"]
            if state == "RUNNING":
                summary["running"] += 1
                summary["cpus_running"] += job["cpus"]
                summary["nodes_running"] += job["nodes"]
                if job["elapsed"] > summary["longest_running_age"]:
                    summary["longest_running_age"] = job["elapsed"]
            elif state == "PENDING":
                summary["pending"] += 1
                summary["cpus_pending"] += job["cpus"]
                summary["pending_" + classify_pending_reason(job["reason"])] += 1
                reason = job["reason"] if job["reason"] not in _NULL_VALUES else "unknown"
                reason_counter[reason] = reason_counter.get(reason, 0) + 1
                if job["pending_age"] is not None and is_schedulable(job["reason"]):
                    pending_ages.append(job["pending_age"])
                if "_" in job["id"] or "[" in job["id"]:
                    summary["array_pending"] += 1
            elif state == "SUSPENDED":
                summary["suspended"] += 1
            elif state == "COMPLETING":
                summary["completing"] += 1
            elif state == "CONFIGURING":
                summary["configuring"] += 1
            else:
                summary["other"] += 1

            if job["user"]:
                users.add(job["user"])
            if job["account"]:
                accounts.add(job["account"])

        summary["pending_limited"] = sum(summary["pending_" + bucket]
                                         for bucket in LIMIT_REASON_BUCKETS)
        summary["pending_schedulable"] = summary["pending"] - sum(
            summary["pending_" + bucket] for bucket in UNSCHEDULABLE_REASON_BUCKETS)
        summary["users_active"] = len(users)
        summary["accounts_active"] = len(accounts)
        if pending_ages:
            summary["oldest_pending_age"] = max(pending_ages)
            summary["mean_pending_age"] = int(sum(pending_ages) / len(pending_ages))
        summary["max"] = max_job_count or 0
        summary["usage"] = percent(summary["total"], max_job_count) if max_job_count else 0.0

        top = sorted(reason_counter.items(), key=lambda item: (-item[1], item[0]))[:5]
        summary["top_pending_reasons"] = ", ".join("%s: %d" % (name, count) for name, count in top)
        return summary

    def merge_partitions(self, partitions, nodes, jobs):
        """Fold node and job data into the per-partition records."""
        index = dict((partition["name"], partition) for partition in partitions)

        for partition in partitions:
            partition.update({
                "nodes_total": 0, "nodes_idle": 0, "nodes_allocated": 0, "nodes_mixed": 0,
                "nodes_down": 0, "nodes_drain": 0, "nodes_available": 0, "nodes_unavailable": 0,
                "cpus_total": 0, "cpus_allocated": 0, "cpus_idle": 0, "cpus_other": 0,
                "memory_total_bytes": 0, "memory_allocated_bytes": 0,
                "gpus_total": 0, "gpus_allocated": 0,
                "jobs_running": 0, "jobs_pending": 0, "jobs_total": 0,
                "cpus_pending": 0, "oldest_pending_age": 0,
            })

        for node in nodes:
            for name in [item for item in node["partitions"].split(",") if item]:
                partition = index.get(name)
                if partition is None:
                    continue
                partition["nodes_total"] += 1
                code = node["state_code"]
                if code == NODE_STATE_IDLE:
                    partition["nodes_idle"] += 1
                elif code == NODE_STATE_ALLOCATED:
                    partition["nodes_allocated"] += 1
                elif code == NODE_STATE_MIXED:
                    partition["nodes_mixed"] += 1
                elif code in (NODE_STATE_DOWN, NODE_STATE_FAIL):
                    partition["nodes_down"] += 1
                if node["drain"]:
                    partition["nodes_drain"] += 1
                if node["available"]:
                    partition["nodes_available"] += 1
                else:
                    partition["nodes_unavailable"] += 1
                partition["cpus_total"] += node["cpus_total"]
                partition["cpus_allocated"] += node["cpus_allocated"]
                if node["available"]:
                    partition["cpus_idle"] += node["cpus_idle"]
                else:
                    partition["cpus_other"] += node["cpus_idle"]
                partition["memory_total_bytes"] += node["memory_total_bytes"]
                partition["memory_allocated_bytes"] += node["memory_allocated_bytes"]
                partition["gpus_total"] += node["gpus_total"]
                partition["gpus_allocated"] += node["gpus_allocated"]

        for job in jobs:
            # A job may be submitted to several partitions at once.
            for name in [item for item in job["partition"].split(",") if item]:
                partition = index.get(name)
                if partition is None:
                    continue
                partition["jobs_total"] += 1
                if job["state"] == "RUNNING":
                    partition["jobs_running"] += 1
                elif job["state"] == "PENDING":
                    partition["jobs_pending"] += 1
                    partition["cpus_pending"] += job["cpus"]
                    if (job["pending_age"] and is_schedulable(job["reason"]) and
                            job["pending_age"] > partition["oldest_pending_age"]):
                        partition["oldest_pending_age"] = job["pending_age"]

        for partition in partitions:
            partition["cpu_utilization"] = percent(partition["cpus_allocated"], partition["cpus_total"])
            partition["memory_utilization"] = percent(
                partition["memory_allocated_bytes"], partition["memory_total_bytes"])
            partition["gpu_utilization"] = percent(partition["gpus_allocated"], partition["gpus_total"])
            partition["nodes_availability"] = percent(
                partition["nodes_available"], partition["nodes_total"])
        return partitions

    def merge_qos(self, qos_list, jobs):
        index = dict((entry["name"], entry) for entry in qos_list)
        for entry in qos_list:
            entry.update({"jobs_running": 0, "jobs_pending": 0, "jobs_total": 0,
                          "cpus_allocated": 0})
        for job in jobs:
            name = job["qos"]
            if not name or name in _NULL_VALUES:
                continue
            entry = index.get(name)
            if entry is None:
                # QOS in use but not readable from sacctmgr (no slurmdbd).
                entry = {"name": name, "priority": 0, "grp_cpus": 0, "grp_jobs": 0,
                         "jobs_running": 0, "jobs_pending": 0, "jobs_total": 0,
                         "cpus_allocated": 0}
                index[name] = entry
                qos_list.append(entry)
            entry["jobs_total"] += 1
            if job["state"] == "RUNNING":
                entry["jobs_running"] += 1
                entry["cpus_allocated"] += job["cpus"]
            elif job["state"] == "PENDING":
                entry["jobs_pending"] += 1
        for entry in qos_list:
            entry["cpu_usage"] = percent(entry["cpus_allocated"], entry["grp_cpus"]) \
                if entry.get("grp_cpus") else 0.0
        return sorted(qos_list, key=lambda entry: entry["name"])

    # -- entry point ---------------------------------------------------------

    def collect(self):
        started = time.time()
        self.now = int(started)

        config = self.collect_config()
        ping = self.collect_ping()
        dbd = self.collect_dbd()
        nodes = self.collect_nodes()
        partitions = self.collect_partitions()
        jobs = self.collect_jobs()
        sdiag = self.collect_sdiag()
        qos = self.collect_qos()
        reservations, reservation_summary = self.collect_reservations()
        licenses = self.collect_licenses()

        node_summary, cpus, memory, gpus = self.summarise_nodes(nodes)
        job_summary = self.summarise_jobs(jobs, config.get("max_job_count"))
        partitions = self.merge_partitions(partitions, nodes, jobs)
        qos = self.merge_qos(qos, jobs)

        cluster = {
            "name": config["cluster_name"],
            "version": config["version"],
            "scheduler_type": config["scheduler_type"],
            "select_type": config["select_type"],
            "controller": config["controller"],
            "slurmctld_up": ping["slurmctld_up"],
            "slurmctld_backup_up": ping["slurmctld_backup_up"],
            "slurmdbd_up": 0 if dbd["slurmdbd_up"] is None else dbd["slurmdbd_up"],
            "partitions_total": len(partitions),
            "qos_total": len(qos),
            "licenses_total": len(licenses),
            "users_active": job_summary["users_active"],
            "accounts_active": job_summary["accounts_active"],
        }
        cluster.update(reservation_summary)

        document = {
            "meta": {
                "timestamp": self.now,
                "age": 0,
                "duration": round(time.time() - started, 3),
                "cached": 0,
                "version": __version__,
                "errors": "; ".join(self.errors),
                "error_count": len(self.errors),
            },
            "cluster": cluster,
            "nodes_summary": node_summary,
            "cpus": cpus,
            "memory": memory,
            "gpus": gpus,
            "jobs": job_summary,
            "scheduler": sdiag,
            "partitions": partitions,
            "qos": qos,
            "licenses": licenses,
            "reservations": reservations,
            "nodes": nodes,
        }
        return prune_nulls(document)

    def collect_accounting_document(self, window=DEFAULT_ACCOUNTING_WINDOW):
        """Build the standalone document served by ``--mode accounting``."""
        started = time.time()
        self.now = int(started)
        accounting = self.collect_accounting(window)
        return prune_nulls({
            "meta": {
                "timestamp": self.now,
                "age": 0,
                "duration": round(time.time() - started, 3),
                "cached": 0,
                "version": __version__,
                "errors": "; ".join(self.errors),
                "error_count": len(self.errors),
            },
            "accounting": accounting,
        })


# ---------------------------------------------------------------------------
# Cache handling
# ---------------------------------------------------------------------------


def read_cache(path):
    """Load the cached document, or None when it is missing or unreadable."""
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (IOError, OSError, ValueError):
        return None


def write_cache(path, document):
    """Write the cache atomically so concurrent readers never see a partial file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="w", dir=directory, prefix=".zabbix_slurm", suffix=".tmp", delete=False)
    except (IOError, OSError) as exc:
        return "cache write failed: %s" % exc
    try:
        json.dump(document, handle)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, 0o644)
        os.rename(handle.name, path)
    except (IOError, OSError) as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        return "cache write failed: %s" % exc
    return None


class CollectionLock(object):
    """Serialises collection between concurrent invocations.

    Both master items are polled by the same agent and can find the cache
    expired in the same second.  Without a lock they would each run a full
    sweep of scontrol/squeue against slurmctld for exactly the same data.  The
    loser of the race waits for the winner and then reads the fresh cache.

    Failing to lock is never fatal: the collection still happens, it is just no
    longer serialised.
    """

    def __init__(self, path, timeout=DEFAULT_LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self.acquired = False
        self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exception):
        self.release()
        return False

    def acquire(self):
        if fcntl is None:
            self.acquired = True  # no locking available, proceed unserialised
            return self.acquired
        try:
            self._handle = open(self.path, "a+")
        except (IOError, OSError):
            self.acquired = True
            return self.acquired

        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                break
            except (IOError, OSError):
                if time.time() >= deadline:
                    self.acquired = False
                    break
                time.sleep(0.1)
        return self.acquired

    def release(self):
        if self._handle is None:
            return
        try:
            if self.acquired and fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        finally:
            self._handle.close()
            self._handle = None


def fresh_cache(path, ttl, now):
    """Return the cached document when it is still within its TTL."""
    cached = read_cache(path)
    if cached is None:
        return None
    if now - cached.get("meta", {}).get("timestamp", 0) >= ttl:
        return None
    return cached


def note_error(document, message):
    meta = document.setdefault("meta", {})
    meta["errors"] = "; ".join(filter(None, [meta.get("errors"), message]))
    meta["error_count"] = meta.get("error_count", 0) + 1
    return document


def obtain_document(collect, cache_file, ttl, now, use_cache=True,
                    lock_timeout=DEFAULT_LOCK_TIMEOUT):
    """Return a document, collecting it only when the cache cannot serve it."""
    if use_cache:
        cached = fresh_cache(cache_file, ttl, now)
        if cached is not None:
            return age_document(cached, now)

    if not use_cache:
        return collect()

    with CollectionLock(cache_file + ".lock", lock_timeout) as lock:
        # Whoever held the lock has probably just refreshed the cache.
        cached = fresh_cache(cache_file, ttl, now)
        if cached is not None:
            return age_document(cached, now)

        if not lock.acquired:
            # Still collecting elsewhere: serve what we have rather than pile
            # another sweep onto slurmctld.
            cached = read_cache(cache_file)
            if cached is not None:
                return note_error(age_document(cached, now),
                                  "another collection is still running")

        document = collect()
        failure = write_cache(cache_file, document)
        if failure:
            note_error(document, failure)
        return document


def age_document(document, now):
    """Refresh the age/cached fields of a document loaded from the cache."""
    meta = document.setdefault("meta", {})
    timestamp = meta.get("timestamp") or now
    meta["age"] = max(int(now - timestamp), 0)
    meta["cached"] = 1
    return document


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------


def prune_nulls(value):
    """Drop keys whose value is None, recursively.

    Zabbix would turn a JSON ``null`` into an unsupported item ("cannot convert
    value to numeric"), which the JSONPath error handler cannot intercept.  A
    missing key on the other hand makes the JSONPath step itself fail, which the
    template handles with "discard value", leaving the item untouched.
    """
    if isinstance(value, dict):
        return dict((key, prune_nulls(item)) for key, item in value.items() if item is not None)
    if isinstance(value, list):
        return [prune_nulls(item) for item in value]
    return value


def slice_document(document, mode):
    """Return the part of the document requested by ``mode``.

    ``cluster`` deliberately omits the (potentially very large) node array so
    that the frequently polled master item stays small.
    """
    if mode in ("all", "accounting"):
        return document
    if mode == "nodes":
        return {"meta": document.get("meta", {}), "nodes": document.get("nodes", [])}
    if mode == "cluster":
        result = dict((key, value) for key, value in document.items() if key != "nodes")
        return result
    raise ValueError("unknown mode: %s" % mode)


def empty_document(message):
    """Fallback document used when nothing could be collected at all."""
    return {
        "meta": {
            "timestamp": int(time.time()),
            "age": 0,
            "duration": 0,
            "cached": 0,
            "version": __version__,
            "errors": message,
            "error_count": 1,
        },
        "cluster": {"slurmctld_up": 0, "slurmdbd_up": 0},
        "nodes_summary": {},
        "cpus": {},
        "memory": {},
        "gpus": {},
        "jobs": {},
        "scheduler": {},
        "partitions": [],
        "qos": [],
        "licenses": [],
        "reservations": [],
        "nodes": [],
        "accounting": {},
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Collect Slurm cluster metrics and print them as JSON for Zabbix.")
    parser.add_argument("--mode", default="cluster",
                        choices=("cluster", "nodes", "all", "accounting"),
                        help="which part of the document to print (default: cluster). "
                             "'accounting' queries sacct and is cached separately")
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE,
                        help="cache location (default: %s)" % DEFAULT_CACHE_FILE)
    parser.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL,
                        help="seconds a cached document stays valid (default: %d)" % DEFAULT_CACHE_TTL)
    parser.add_argument("--no-cache", action="store_true",
                        help="always collect, never read or write the cache")
    parser.add_argument("--cache-only", action="store_true",
                        help="only read the cache, never run Slurm commands "
                             "(use together with a systemd timer running --refresh)")
    parser.add_argument("--refresh", action="store_true",
                        help="collect and update the cache without printing anything; "
                             "combine with --mode accounting to refresh that cache")
    parser.add_argument("--accounting-ttl", type=int, default=DEFAULT_ACCOUNTING_TTL,
                        help="seconds a cached accounting document stays valid "
                             "(default: %d)" % DEFAULT_ACCOUNTING_TTL)
    parser.add_argument("--accounting-window", type=int, default=DEFAULT_ACCOUNTING_WINDOW,
                        help="how far back sacct looks, in seconds (default: %d)"
                             % DEFAULT_ACCOUNTING_WINDOW)
    parser.add_argument("--lock-timeout", type=int, default=DEFAULT_LOCK_TIMEOUT,
                        help="seconds to wait for a concurrent collection to finish "
                             "(default: %d)" % DEFAULT_LOCK_TIMEOUT)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="timeout in seconds for a single Slurm command (default: %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--slurm-bin-dir", default=None,
                        help="directory holding the Slurm client commands")
    parser.add_argument("--no-sacctmgr", action="store_true",
                        help="skip slurmdbd/QOS queries on clusters without accounting")
    parser.add_argument("--explain-node", metavar="NODE",
                        help="show how one node's GPU numbers were derived from the "
                             "Slurm output, and exit")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when collection reported errors")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return parser


GRES_FIELDS = ("Gres", "GresUsed", "GresDrain", "CfgTRES", "AllocTRES")


def explain_node(collector, wanted):
    """Print how one node's GPU numbers were derived.

    Slurm describes GRES differently between releases and configurations, so
    when the numbers look wrong this shows the raw fields next to what the
    collector made of them.
    """
    output = collector.run(["scontrol", "show", "node", wanted])
    if output is None:
        print("could not read node %r: %s" % (wanted, "; ".join(collector.errors)))
        return 1

    record = parse_kv_line(" ".join(output.split("\n")))
    if not record.get("NodeName"):
        print("no node record in the output for %r" % wanted)
        return 1

    print("node: %s" % record.get("NodeName"))
    print("\nraw fields:")
    for field in GRES_FIELDS:
        print("  %-10s = %s" % (field, record.get(field, "(field not present)")))

    print("\nparsed:")
    print("  Gres       -> %s" % json.dumps(parse_gres(record.get("Gres", "")),
                                            sort_keys=True))
    print("  GresUsed   -> %s" % json.dumps(parse_gres(record.get("GresUsed", "")),
                                            sort_keys=True))
    configured = parse_tres(record.get("CfgTRES", ""))
    allocated = parse_tres(record.get("AllocTRES", ""))
    print("  CfgTRES    -> gpus=%d" % tres_gpu_count(configured))
    print("  AllocTRES  -> gpus=%d" % tres_gpu_count(allocated))

    node = collector._build_node(record)
    print("\nderived:")
    for field in ("gpus_total", "gpus_allocated", "gpus_idle", "gpu_utilization",
                  "gpu_type"):
        print("  %-16s = %s" % (field, node.get(field)))

    if node.get("gpus_total") and not node.get("gpus_allocated"):
        print("\nNo GPUs look allocated. If jobs are using this node's GPUs, the "
              "raw fields above are the ones to report:\n"
              "  https://github.com/Kevin-NZ/Zabbix-Slurm/issues")
    return 0


def accounting_cache_file(cache_file):
    """Accounting is collected on its own schedule, so it caches separately."""
    return cache_file + ".accounting"


def main(argv=None):
    args = build_parser().parse_args(argv)
    now = int(time.time())

    if args.explain_node:
        return explain_node(
            SlurmCollector(bin_dir=args.slurm_bin_dir, timeout=args.timeout),
            args.explain_node)

    accounting = args.mode == "accounting"
    cache_file = accounting_cache_file(args.cache_file) if accounting else args.cache_file
    ttl = args.accounting_ttl if accounting else args.cache_ttl

    def collect():
        collector = SlurmCollector(
            bin_dir=args.slurm_bin_dir,
            timeout=args.timeout,
            enable_sacctmgr=not args.no_sacctmgr,
        )
        if accounting:
            return collector.collect_accounting_document(args.accounting_window)
        return collector.collect()

    if args.cache_only:
        document = read_cache(cache_file)
        if document is None:
            document = empty_document("cache %s is missing or unreadable" % cache_file)
        else:
            document = age_document(document, now)
    else:
        document = obtain_document(
            collect,
            cache_file,
            0 if args.refresh else ttl,
            now,
            use_cache=not args.no_cache,
            lock_timeout=args.lock_timeout,
        )

    # --refresh only warms the cache, so it stays silent: it runs from a systemd
    # timer, where printing the document would just fill the journal.
    if args.refresh:
        return 1 if (args.strict and document["meta"].get("error_count")) else 0

    payload = slice_document(document, args.mode)
    if args.pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, separators=(",", ":")))

    if args.strict and document.get("meta", {}).get("error_count"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
