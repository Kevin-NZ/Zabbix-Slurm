#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit and end-to-end tests for the Slurm collector.

The end-to-end tests run the collector against ``tests/fakebin``, a set of
shell stubs that replay the recorded Slurm command output in
``tests/fixtures``.  No Slurm installation is required.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAKEBIN = os.path.join(HERE, "fakebin")
COLLECTOR = os.path.join(ROOT, "bin", "slurm_zabbix.py")

sys.path.insert(0, os.path.join(ROOT, "bin"))

import slurm_zabbix as sz  # noqa: E402

# Epoch of 2024-05-05T12:00:00 in the local timezone, matching the fixtures.
FIXTURE_NOW = int(time.mktime(time.strptime("2024-05-05T12:00:00", "%Y-%m-%dT%H:%M:%S")))


class ParsingTest(unittest.TestCase):
    def test_kv_line_handles_values_with_spaces(self):
        line = ("NodeName=n001 CPUTot=32 OS=Linux 4.18.0-513.el8.x86_64 #1 SMP Fri Dec 1 12:00:00 "
                "UTC 2023 RealMemory=515000 State=MIXED Reason=disk failure [root@2024-05-01T10:00:00]")
        record = sz.parse_kv_line(line)
        self.assertEqual(record["NodeName"], "n001")
        self.assertEqual(record["CPUTot"], "32")
        self.assertEqual(record["RealMemory"], "515000")
        self.assertEqual(record["State"], "MIXED")
        self.assertEqual(record["Reason"], "disk failure [root@2024-05-01T10:00:00]")
        self.assertTrue(record["OS"].startswith("Linux 4.18.0"))

    def test_kv_line_keeps_embedded_equals(self):
        record = sz.parse_kv_line("NodeName=n1 CfgTRES=cpu=32,mem=515000M,gres/gpu=4 Weight=1")
        self.assertEqual(record["CfgTRES"], "cpu=32,mem=515000M,gres/gpu=4")
        self.assertEqual(record["Weight"], "1")

    def test_parse_tres(self):
        tres = sz.parse_tres("cpu=48,mem=768G,gres/gpu=3,billing=48")
        self.assertEqual(tres["cpu"], 48)
        self.assertEqual(tres["gres/gpu"], 3)
        self.assertEqual(tres["mem"], 768 * 1024 ** 3)

    def test_parse_tres_megabytes_without_suffix(self):
        self.assertEqual(sz.parse_tres("mem=515000M")["mem"], 515000 * 1024 ** 3 / 1024)
        self.assertEqual(sz.parse_tres("mem=1024")["mem"], 1024 * 1024 ** 2)

    def test_parse_tres_empty(self):
        self.assertEqual(sz.parse_tres(""), {})
        self.assertEqual(sz.parse_tres("(null)"), {})

    def test_parse_duration(self):
        self.assertEqual(sz.parse_slurm_duration("1-04:00:00"), 100800)
        self.assertEqual(sz.parse_slurm_duration("3:04:05"), 11045)
        self.assertEqual(sz.parse_slurm_duration("4:05"), 245)
        self.assertIsNone(sz.parse_slurm_duration("UNLIMITED"))
        self.assertIsNone(sz.parse_slurm_duration(""))

    def test_to_int_handles_slurm_placeholders(self):
        self.assertIsNone(sz.to_int("N/A"))
        self.assertIsNone(sz.to_int("UNLIMITED"))
        self.assertEqual(sz.to_int("N/A", 0), 0)
        self.assertEqual(sz.to_int("42"), 42)

    def test_percent_guards_zero(self):
        self.assertEqual(sz.percent(5, 0), 0.0)
        self.assertEqual(sz.percent(1, 4), 25.0)


class NodeStateTest(unittest.TestCase):
    def test_base_states(self):
        self.assertEqual(sz.node_state_code("IDLE", []), sz.NODE_STATE_IDLE)
        self.assertEqual(sz.node_state_code("ALLOCATED", []), sz.NODE_STATE_ALLOCATED)
        self.assertEqual(sz.node_state_code("MIXED", []), sz.NODE_STATE_MIXED)
        self.assertEqual(sz.node_state_code("BOGUS", []), sz.NODE_STATE_UNKNOWN)

    def test_down_wins_over_drain(self):
        self.assertEqual(sz.node_state_code("DOWN", ["DRAIN"]), sz.NODE_STATE_DOWN)

    def test_drain_split_by_base_state(self):
        # Idle + DRAIN is fully drained; allocated + DRAIN is still draining.
        self.assertEqual(sz.node_state_code("IDLE", ["DRAIN"]), sz.NODE_STATE_DRAINED)
        self.assertEqual(sz.node_state_code("MIXED", ["DRAIN"]), sz.NODE_STATE_DRAINING)
        self.assertEqual(sz.node_state_code("ALLOCATED", ["DRAIN"]), sz.NODE_STATE_DRAINING)

    def test_maint_wins_over_drain(self):
        self.assertEqual(sz.node_state_code("IDLE", ["MAINT", "DRAIN"]), sz.NODE_STATE_MAINT)

    def test_flag_states(self):
        self.assertEqual(sz.node_state_code("IDLE", ["RESERVED"]), sz.NODE_STATE_RESERVED)
        self.assertEqual(sz.node_state_code("IDLE", ["POWERED_DOWN"]), sz.NODE_STATE_POWERED_DOWN)
        self.assertEqual(sz.node_state_code("IDLE", ["REBOOT_REQUESTED"]), sz.NODE_STATE_REBOOT)
        self.assertEqual(sz.node_state_code("FUTURE", []), sz.NODE_STATE_FUTURE)


class PendingReasonTest(unittest.TestCase):
    def test_buckets(self):
        cases = {
            "Resources": "resources",
            "Priority": "priority",
            "Dependency": "dependency",
            "DependencyNeverSatisfied": "dependency",
            "QOSMaxGRESPerUser": "qos_limit",
            "AssocGrpCpuLimit": "association_limit",
            "Licenses": "licenses",
            "PartitionDown": "partition",
            "JobArrayTaskLimit": "held",
            "BeginTime": "held",
            "SomethingNew": "other",
            "None": "other",
            "": "other",
        }
        for reason, expected in cases.items():
            self.assertEqual(sz.classify_pending_reason(reason), expected, reason)

    def test_every_bucket_has_a_summary_field(self):
        summary = sz.SlurmCollector().summarise_jobs([], None)
        for bucket in sz.PENDING_REASON_KEYS:
            self.assertIn("pending_" + bucket, summary)


class SdiagTest(unittest.TestCase):
    def setUp(self):
        self.collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        self.collector.now = FIXTURE_NOW
        self.stats = self.collector.collect_sdiag()

    def test_counters(self):
        self.assertEqual(self.stats["server_thread_count"], 8)
        self.assertEqual(self.stats["agent_queue_size"], 12)
        self.assertEqual(self.stats["dbd_agent_queue_size"], 4)
        self.assertEqual(self.stats["jobs_submitted"], 15234)
        self.assertEqual(self.stats["jobs_failed"], 33)

    def test_cycles_are_converted_to_seconds(self):
        self.assertAlmostEqual(self.stats["schedule_cycle_last"], 0.15234)
        self.assertAlmostEqual(self.stats["schedule_cycle_mean"], 0.285412)
        self.assertAlmostEqual(self.stats["backfill_cycle_last"], 2.841233)
        self.assertAlmostEqual(self.stats["backfill_cycle_max"], 29.1234)

    def test_sections_are_kept_apart(self):
        # "Last cycle" exists in the main, backfill and RPC sections; the RPC
        # one must never overwrite the scheduler values.
        self.assertNotEqual(self.stats["schedule_cycle_last"], self.stats["backfill_cycle_last"])
        self.assertAlmostEqual(self.stats["schedule_cycle_last"], 0.15234)

    def test_backfill_depth_and_age(self):
        self.assertEqual(self.stats["backfill_last_depth"], 380)
        self.assertEqual(self.stats["backfill_depth_mean"], 341)
        self.assertEqual(self.stats["backfilled_jobs"], 8823)
        self.assertEqual(self.stats["backfill_last_cycle_age"], 48)


class CollectorEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        cls.document = collector.collect()
        cls.errors = collector.errors

    def test_no_collection_errors(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.document["meta"]["error_count"], 0)

    def test_cluster_identity(self):
        cluster = self.document["cluster"]
        self.assertEqual(cluster["name"], "hpc-prod")
        self.assertEqual(cluster["version"], "23.02.7")
        self.assertEqual(cluster["scheduler_type"], "sched/backfill")
        self.assertEqual(cluster["partitions_total"], 3)

    def test_ping_reports_primary_up_backup_down(self):
        self.assertEqual(self.document["cluster"]["slurmctld_up"], 1)
        self.assertEqual(self.document["cluster"]["slurmctld_backup_up"], 0)
        self.assertEqual(self.document["cluster"]["slurmdbd_up"], 1)

    def test_reservations(self):
        cluster = self.document["cluster"]
        self.assertEqual(cluster["reservations_total"], 2)
        self.assertEqual(cluster["reservations_active"], 1)
        self.assertEqual(cluster["reservations_nodes"], 2)

    def test_node_count_and_states(self):
        summary = self.document["nodes_summary"]
        self.assertEqual(summary["total"], 9)
        self.assertEqual(summary["idle"], 3)       # n003, g002, dbg01
        self.assertEqual(summary["allocated"], 1)  # n002
        self.assertEqual(summary["mixed"], 2)      # n001, g001
        self.assertEqual(summary["down"], 1)       # n004
        self.assertEqual(summary["drained"], 1)    # n005
        self.assertEqual(summary["draining"], 1)   # n006
        self.assertEqual(summary["drain"], 2)
        self.assertEqual(summary["not_responding"], 2)  # n004 (flag), g002 ("*")
        self.assertEqual(summary["available"], 5)
        self.assertEqual(summary["unavailable"], 4)

    def test_cpu_totals(self):
        cpus = self.document["cpus"]
        self.assertEqual(cpus["total"], 336)      # 6*32 + 2*64 + 16
        self.assertEqual(cpus["allocated"], 126)  # 30 + 32 + 16 + 48
        # Idle CPUs on unusable nodes are accounted as "other", like sinfo does.
        self.assertEqual(cpus["idle"], 66)
        self.assertEqual(cpus["other"], 144)
        self.assertEqual(cpus["allocated"] + cpus["idle"] + cpus["other"], cpus["total"])
        self.assertAlmostEqual(cpus["utilization"], 37.5)

    def test_gpu_totals(self):
        gpus = self.document["gpus"]
        self.assertEqual(gpus["total"], 8)
        self.assertEqual(gpus["allocated"], 3)
        self.assertEqual(gpus["idle"], 5)
        self.assertAlmostEqual(gpus["utilization"], 37.5)

    def test_memory_totals_in_bytes(self):
        memory = self.document["memory"]
        expected_total = (6 * 515000 + 2 * 1030000 + 128000) * sz.MB
        self.assertEqual(memory["total_bytes"], expected_total)
        self.assertGreater(memory["allocated_bytes"], 0)

    def test_job_states(self):
        jobs = self.document["jobs"]
        self.assertEqual(jobs["total"], 14)
        self.assertEqual(jobs["running"], 4)
        self.assertEqual(jobs["pending"], 8)
        self.assertEqual(jobs["completing"], 1)
        self.assertEqual(jobs["suspended"], 1)
        self.assertEqual(jobs["users_active"], 7)
        self.assertEqual(jobs["accounts_active"], 4)
        self.assertEqual(jobs["max"], 50000)

    def test_pending_reason_breakdown(self):
        jobs = self.document["jobs"]
        self.assertEqual(jobs["pending_resources"], 1)
        self.assertEqual(jobs["pending_priority"], 2)
        self.assertEqual(jobs["pending_dependency"], 1)
        self.assertEqual(jobs["pending_qos_limit"], 1)
        self.assertEqual(jobs["pending_association_limit"], 1)
        self.assertEqual(jobs["pending_partition"], 1)
        self.assertEqual(jobs["pending_held"], 1)
        bucketed = sum(jobs["pending_" + key] for key in sz.PENDING_REASON_KEYS)
        self.assertEqual(bucketed, jobs["pending"])
        self.assertIn("Priority: 2", jobs["top_pending_reasons"])

    def test_only_limit_reasons_count_as_blocked(self):
        """A job waiting on a dependency is not blocked by the cluster."""
        jobs = self.document["jobs"]
        # QOS limit, association limit and partition; the dependency, held,
        # priority and resources jobs are all excluded.
        self.assertEqual(jobs["pending_limited"], 3)
        self.assertEqual(
            jobs["pending_limited"],
            sum(jobs["pending_" + bucket] for bucket in sz.LIMIT_REASON_BUCKETS))
        self.assertLess(jobs["pending_limited"], jobs["pending"])

    def test_a_queue_of_dependencies_is_never_blocked(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        waiting = [{"id": str(index), "partition": "compute", "state": "PENDING",
                    "reason": reason, "cpus": 1, "nodes": 1, "user": "alice",
                    "account": "physics", "qos": "normal", "submit_time": None,
                    "pending_age": None, "elapsed": 0}
                   for index, reason in enumerate(
                       ["Dependency", "DependencyNeverSatisfied", "JobHeldUser",
                        "BeginTime", "Licenses", "Reservation", "Priority"])]
        summary = collector.summarise_jobs(waiting, None)
        self.assertEqual(summary["pending"], 7)
        self.assertEqual(summary["pending_limited"], 0)

    def test_limit_reasons_are_counted_as_blocked(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        limited = [{"id": str(index), "partition": "compute", "state": "PENDING",
                    "reason": reason, "cpus": 1, "nodes": 1, "user": "alice",
                    "account": "physics", "qos": "normal", "submit_time": None,
                    "pending_age": None, "elapsed": 0}
                   for index, reason in enumerate(
                       ["QOSMaxCpuPerUserLimit", "AssocGrpCpuLimit", "PartitionNodeLimit",
                        "SomethingUnrecognised"])]
        summary = collector.summarise_jobs(limited, None)
        self.assertEqual(summary["pending_limited"], 4)

    def test_job_ages_use_submit_time(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        collector.now = FIXTURE_NOW
        jobs = collector.collect_jobs()
        summary = collector.summarise_jobs(jobs, 50000)
        # Job 1008 was submitted 2024-05-04T12:00:00, i.e. 24h before "now".
        self.assertEqual(summary["oldest_pending_age"], 86400)
        # Job 1003 has been running for 1-04:00:00.
        self.assertEqual(summary["longest_running_age"], 100800)

    def test_partition_aggregation(self):
        partitions = dict((p["name"], p) for p in self.document["partitions"])
        self.assertEqual(sorted(partitions), ["compute", "debug", "gpu"])

        compute = partitions["compute"]
        self.assertEqual(compute["state_code"], sz.PARTITION_STATE_UP)
        self.assertEqual(compute["default"], 1)
        self.assertEqual(compute["nodes_total"], 6)
        self.assertEqual(compute["cpus_total"], 192)
        self.assertEqual(compute["jobs_running"], 3)
        self.assertEqual(compute["jobs_pending"], 6)
        self.assertEqual(compute["max_time_seconds"], 604800)

        gpu = partitions["gpu"]
        self.assertEqual(gpu["gpus_total"], 8)
        self.assertEqual(gpu["gpus_allocated"], 3)
        self.assertEqual(gpu["priority_tier"], 10)

        debug = partitions["debug"]
        self.assertEqual(debug["state_code"], sz.PARTITION_STATE_INACTIVE)
        # n001 belongs to both compute and debug and must be counted in both.
        self.assertEqual(debug["nodes_total"], 2)

    def test_qos_merges_configuration_and_running_jobs(self):
        qos = dict((entry["name"], entry) for entry in self.document["qos"])
        self.assertEqual(sorted(qos), ["gpu", "high", "normal"])
        self.assertEqual(qos["gpu"]["priority"], 500)
        self.assertEqual(qos["gpu"]["grp_cpus"], 256)
        self.assertEqual(qos["gpu"]["jobs_running"], 1)
        self.assertEqual(qos["normal"]["jobs_pending"], 7)

    def test_node_details(self):
        nodes = dict((node["name"], node) for node in self.document["nodes"])
        self.assertEqual(len(nodes), 9)

        n001 = nodes["n001"]
        self.assertEqual(n001["cpus_total"], 32)
        self.assertEqual(n001["cpus_allocated"], 30)
        self.assertEqual(n001["cpus_idle"], 2)
        self.assertAlmostEqual(n001["cpu_utilization"], 93.75)
        self.assertAlmostEqual(n001["cpu_load"], 29.85)
        self.assertEqual(n001["partitions"], "compute,debug")
        self.assertEqual(n001["memory_total_bytes"], 515000 * sz.MB)
        self.assertEqual(n001["reason"], "")

        n004 = nodes["n004"]
        self.assertEqual(n004["state_code"], sz.NODE_STATE_DOWN)
        self.assertEqual(n004["available"], 0)
        self.assertEqual(n004["not_responding"], 1)
        self.assertTrue(n004["reason"].startswith("Not responding"))
        # Unknown values are omitted rather than reported as null, so that the
        # matching Zabbix items simply keep their previous value.
        for absent in ("cpu_load", "cpu_load_per_core", "memory_free_bytes", "boot_time", "uptime"):
            self.assertNotIn(absent, n004)

        n005 = nodes["n005"]
        self.assertEqual(n005["state_code"], sz.NODE_STATE_DRAINED)
        self.assertEqual(n005["drain"], 1)
        self.assertIn("hardware maintenance", n005["reason"])

        g001 = nodes["g001"]
        self.assertEqual(g001["gpus_total"], 4)
        self.assertEqual(g001["gpus_allocated"], 3)
        self.assertEqual(g001["gpus_idle"], 1)

        g002 = nodes["g002"]
        self.assertEqual(g002["not_responding"], 1)
        self.assertEqual(g002["available"], 0)
        self.assertEqual(g002["state_base"], "IDLE")

    def test_document_is_json_serialisable(self):
        json.dumps(self.document)

    def test_document_contains_no_nulls(self):
        """Zabbix cannot convert a JSON null, so the document must not hold any."""

        def walk(value, path):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertIsNotNone(item, "%s.%s is null" % (path, key))
                    walk(item, "%s.%s" % (path, key))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    self.assertIsNotNone(item, "%s[%d] is null" % (path, index))
                    walk(item, "%s[%d]" % (path, index))

        walk(self.document, "$")


class GresParsingTest(unittest.TestCase):
    def test_typed_gres(self):
        self.assertEqual(sz.parse_gres("gpu:a100:4"), {"gpu": 4, "gpu:a100": 4})

    def test_untyped_gres(self):
        self.assertEqual(sz.parse_gres("gpu:4"), {"gpu": 4})

    def test_socket_and_index_suffixes_are_ignored(self):
        self.assertEqual(sz.parse_gres("gpu:a100:4(S:0-1)"), {"gpu": 4, "gpu:a100": 4})
        self.assertEqual(sz.parse_gres("gpu:a100:3(IDX:0-2)"), {"gpu": 3, "gpu:a100": 3})
        self.assertEqual(sz.parse_gres("gpu:a100:0(IDX:N/A)"), {"gpu": 0, "gpu:a100": 0})

    def test_several_types_on_one_node(self):
        self.assertEqual(sz.parse_gres("gpu:a100:2(S:0-1),gpu:v100:2(S:0-1)"),
                         {"gpu": 4, "gpu:a100": 2, "gpu:v100": 2})

    def test_other_resources_are_kept_apart_from_gpus(self):
        parsed = sz.parse_gres("gpu:a100:2,mps:200,shard:8")
        self.assertEqual(parsed["gpu"], 2)
        self.assertEqual(parsed["mps"], 200)
        self.assertEqual(parsed["shard"], 8)

    def test_empty_and_null(self):
        self.assertEqual(sz.parse_gres(""), {})
        self.assertEqual(sz.parse_gres("(null)"), {})


class GpuAccountingTest(unittest.TestCase):
    """GPUs must be reported whether or not the cluster tracks them in TRES."""

    @staticmethod
    def node(line):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        collector.now = FIXTURE_NOW
        return collector._build_node(sz.parse_kv_line(line))

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "fixtures",
                               "scontrol_show_node_untracked_gres.txt")) as handle:
            cls.untracked = dict((node["name"], node) for node in
                                 (cls.node(line) for line in handle if line.strip()))

    def test_gpus_are_found_without_gres_gpu_in_tres(self):
        """AccountingStorageTRES does not include gres/gpu by default."""
        gpu01 = self.untracked["gpu01"]
        self.assertNotIn("gres/gpu", sz.parse_tres(
            "cpu=64,mem=1030000M,billing=64"))  # what CfgTRES looks like there
        self.assertEqual(gpu01["gpus_total"], 8)
        self.assertEqual(gpu01["gpus_allocated"], 5)
        self.assertEqual(gpu01["gpus_idle"], 3)
        self.assertAlmostEqual(gpu01["gpu_utilization"], 62.5)
        self.assertEqual(gpu01["gpu_type"], "a100")

    def test_idle_gpu_node(self):
        gpu02 = self.untracked["gpu02"]
        self.assertEqual(gpu02["gpus_total"], 4)
        self.assertEqual(gpu02["gpus_allocated"], 0)
        self.assertEqual(gpu02["gpu_type"], "v100")

    def test_node_with_two_gpu_types(self):
        gpu03 = self.untracked["gpu03"]
        self.assertEqual(gpu03["gpus_total"], 4)     # 2 A100 + 2 V100, mps ignored
        self.assertEqual(gpu03["gpus_allocated"], 1)
        self.assertEqual(gpu03["gpu_type"], "a100,v100")

    def test_untyped_gres_node(self):
        gpu04 = self.untracked["gpu04"]
        self.assertEqual(gpu04["gpus_total"], 2)
        self.assertEqual(gpu04["gpus_allocated"], 2)
        self.assertEqual(gpu04["gpu_type"], "")

    def test_a_mixed_cluster_reports_the_totals(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        _, _, _, gpus = collector.summarise_nodes(list(self.untracked.values()))
        self.assertEqual(gpus["total"], 18)      # 8 + 4 + 4 + 2
        self.assertEqual(gpus["allocated"], 8)   # 5 + 0 + 1 + 2
        self.assertGreater(gpus["utilization"], 0)

    def test_tres_is_still_used_when_the_cluster_tracks_gpus(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        nodes = dict((node["name"], node) for node in collector.collect_nodes())
        self.assertEqual(nodes["g001"]["gpus_total"], 4)
        self.assertEqual(nodes["g001"]["gpus_allocated"], 3)
        self.assertEqual(nodes["g001"]["gpu_type"], "a100")

    def test_typed_only_tres_is_understood(self):
        """Some clusters track gres/gpu:a100 without the generic gres/gpu."""
        tres = sz.parse_tres("cpu=64,mem=1030000M,billing=64,gres/gpu:a100=3")
        self.assertEqual(tres["gres/gpu:a100"], 3)
        self.assertEqual(sz.tres_gpu_count(tres), 3)

    def test_generic_tres_wins_over_typed_entries(self):
        """Both are listed together; counting both would double the GPUs."""
        tres = sz.parse_tres("gres/gpu=4,gres/gpu:a100=4")
        self.assertEqual(sz.tres_gpu_count(tres), 4)

    def test_a_zero_in_one_source_does_not_hide_the_other(self):
        line = ("NodeName=z1 CPUTot=8 CPUAlloc=0 RealMemory=1000 AllocMem=0 State=MIXED "
                "Partitions=gpu Gres=gpu:a100:4 GresUsed=gpu:a100:2 "
                "CfgTRES=cpu=8,mem=1000M,gres/gpu=4 AllocTRES=cpu=0,mem=0,gres/gpu=0")
        node = self.node(line)
        self.assertEqual(node["gpus_total"], 4)
        self.assertEqual(node["gpus_allocated"], 2)

    def test_gpus_found_when_only_tres_reports_them(self):
        """Older releases may not print GresUsed at all."""
        line = ("NodeName=z2 CPUTot=8 CPUAlloc=8 RealMemory=1000 AllocMem=1000 "
                "State=ALLOCATED Partitions=gpu Gres=gpu:2 "
                "CfgTRES=cpu=8,mem=1000M,gres/gpu=2 AllocTRES=cpu=8,mem=1000M,gres/gpu=2")
        node = self.node(line)
        self.assertEqual(node["gpus_total"], 2)
        self.assertEqual(node["gpus_allocated"], 2)
        self.assertAlmostEqual(node["gpu_utilization"], 100.0)

    def test_typed_only_tres_end_to_end(self):
        line = ("NodeName=z3 CPUTot=8 CPUAlloc=4 RealMemory=1000 AllocMem=500 State=MIXED "
                "Partitions=gpu Gres=(null) "
                "CfgTRES=cpu=8,mem=1000M,gres/gpu:v100=4 "
                "AllocTRES=cpu=4,mem=500M,gres/gpu:v100=1")
        node = self.node(line)
        self.assertEqual(node["gpus_total"], 4)
        self.assertEqual(node["gpus_allocated"], 1)

    def test_nodes_without_gpus_report_zero(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        nodes = dict((node["name"], node) for node in collector.collect_nodes())
        self.assertEqual(nodes["n001"]["gpus_total"], 0)
        self.assertEqual(nodes["n001"]["gpu_type"], "")


class GresUsedFallbackTest(unittest.TestCase):
    """Some Slurm releases never print GresUsed in scontrol show node."""

    @staticmethod
    def nodes_without_gresused():
        """Nodes with GPUs configured but no usable allocation information."""
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        built = []
        with open(os.path.join(HERE, "fixtures",
                               "scontrol_show_node_untracked_gres.txt")) as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = sz.parse_kv_line(line)
                record.pop("GresUsed", None)      # as if scontrol never printed it
                built.append(collector._build_node(record))
        return built

    def test_without_the_fallback_everything_reads_zero(self):
        nodes = self.nodes_without_gresused()
        self.assertTrue(all(node["gpus_total"] > 0 for node in nodes))
        self.assertEqual(sum(node["gpus_allocated"] for node in nodes), 0)

    def test_sinfo_fills_in_the_allocation(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        nodes = dict((node["name"], node) for node in
                     collector.fill_gpu_allocation(self.nodes_without_gresused()))
        self.assertEqual(nodes["gpu01"]["gpus_allocated"], 5)
        self.assertEqual(nodes["gpu01"]["gpus_idle"], 3)
        self.assertAlmostEqual(nodes["gpu01"]["gpu_utilization"], 62.5)
        self.assertEqual(nodes["gpu03"]["gpus_allocated"], 1)
        self.assertEqual(nodes["gpu04"]["gpus_allocated"], 2)
        # Genuinely idle nodes stay at zero.
        self.assertEqual(nodes["gpu02"]["gpus_allocated"], 0)

    def test_allocation_is_capped_at_the_configured_total(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        node = dict(self.nodes_without_gresused()[0])
        node["gpus_total"] = 2          # fewer than sinfo reports
        filled = collector.fill_gpu_allocation([node])[0]
        self.assertEqual(filled["gpus_allocated"], 2)
        self.assertEqual(filled["gpus_idle"], 0)

    def test_the_fallback_is_skipped_when_scontrol_already_answers(self):
        """No extra command on clusters where GresUsed works."""
        directory = tempfile.mkdtemp(prefix="slurm-fallback")
        counter = os.path.join(directory, "log")
        try:
            environment = os.environ.copy()
            environment["SLURM_TEST_COUNTER"] = counter
            process = subprocess.Popen(
                [sys.executable, COLLECTOR, "--mode", "nodes", "--slurm-bin-dir", FAKEBIN,
                 "--no-cache"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, env=environment)
            stdout, stderr = process.communicate()
            self.assertEqual(process.returncode, 0, stderr)
            # g001 has GPUs allocated through the normal path.
            nodes = dict((node["name"], node) for node in json.loads(stdout)["nodes"])
            self.assertEqual(nodes["g001"]["gpus_allocated"], 3)
            with open(counter) as handle:
                self.assertNotIn("sinfo", handle.read())
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_no_gpu_cluster_never_calls_sinfo(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        plain = [node for node in collector.collect_nodes() if node["gpus_total"] == 0]
        self.assertTrue(plain)
        self.assertEqual(collector.fill_gpu_allocation(plain), plain)


class ExplainNodeTest(unittest.TestCase):
    """The diagnostic has to show the raw fields, not just the conclusion."""

    def run_explain(self, node):
        process = subprocess.Popen(
            [sys.executable, COLLECTOR, "--slurm-bin-dir", FAKEBIN, "--explain-node", node],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def test_reports_raw_fields_and_derived_values(self):
        code, stdout, stderr = self.run_explain("gpu01")
        self.assertEqual(code, 0, stderr)
        for expected in ("Gres", "GresUsed", "CfgTRES", "AllocTRES",
                         "gpu:a100:8(S:0-1)", "gpu:a100:5(IDX:0-4)",
                         "gpus_total", "gpus_allocated"):
            self.assertIn(expected, stdout)
        self.assertIn("62.5", stdout)

    def test_marks_a_field_that_is_absent(self):
        code, stdout, _ = self.run_explain("gpu01")
        self.assertEqual(code, 0)
        self.assertIn("field not present", stdout)

    def test_unknown_node(self):
        code, stdout, _ = self.run_explain("does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("no node record", stdout)


class QueueWaitTest(unittest.TestCase):
    """Queue wait must measure jobs the scheduler could actually start."""

    def pending(self, reason, age, partition="compute"):
        return {"id": "1", "partition": partition, "state": "PENDING", "reason": reason,
                "cpus": 1, "nodes": 1, "user": "alice", "account": "physics",
                "qos": "normal", "submit_time": FIXTURE_NOW - age,
                "pending_age": age, "elapsed": 0}

    def test_unschedulable_jobs_never_set_the_wait(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        for reason in ("Dependency", "DependencyNeverSatisfied", "JobHeldUser",
                       "JobHeldAdmin", "BeginTime", "Reservation"):
            summary = collector.summarise_jobs([self.pending(reason, 30 * 86400)], None)
            self.assertEqual(summary["oldest_pending_age"], 0, reason)
            self.assertEqual(summary["mean_pending_age"], 0, reason)

    def test_schedulable_jobs_still_set_the_wait(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        for reason in ("Resources", "Priority", "QOSMaxCpuPerUserLimit",
                       "AssocGrpCpuLimit", "Licenses", "PartitionNodeLimit"):
            summary = collector.summarise_jobs([self.pending(reason, 3600)], None)
            self.assertEqual(summary["oldest_pending_age"], 3600, reason)

    def test_a_queue_of_dependencies_has_nothing_ready_to_run(self):
        """The backfill scheduler has nothing to do with such a queue."""
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        blocked = [self.pending(reason, 600) for reason in
                   ("Dependency", "DependencyNeverSatisfied", "JobHeldAdmin",
                    "BeginTime", "Reservation")]
        summary = collector.summarise_jobs(blocked, None)
        self.assertEqual(summary["pending"], 5)
        self.assertEqual(summary["pending_schedulable"], 0)

    def test_runnable_jobs_are_counted_as_ready(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        queue = [self.pending("Resources", 600), self.pending("Priority", 600),
                 self.pending("QOSMaxCpuPerUserLimit", 600),
                 self.pending("Dependency", 600)]
        summary = collector.summarise_jobs(queue, None)
        self.assertEqual(summary["pending"], 4)
        self.assertEqual(summary["pending_schedulable"], 3)

    def test_schedulable_and_unschedulable_always_add_up(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        queue = [self.pending(reason, 600) for reason in
                 ("Resources", "Priority", "Dependency", "JobHeldUser", "Reservation",
                  "Licenses", "AssocGrpCpuLimit", "SomethingNew")]
        summary = collector.summarise_jobs(queue, None)
        unschedulable = sum(summary["pending_" + bucket]
                            for bucket in sz.UNSCHEDULABLE_REASON_BUCKETS)
        self.assertEqual(summary["pending_schedulable"] + unschedulable,
                         summary["pending"])

    def test_a_held_job_does_not_hide_a_real_wait(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        summary = collector.summarise_jobs(
            [self.pending("Dependency", 30 * 86400), self.pending("Resources", 7200)], None)
        self.assertEqual(summary["oldest_pending_age"], 7200)
        self.assertEqual(summary["mean_pending_age"], 7200)
        # The job itself is still counted in the queue and in its reason bucket.
        self.assertEqual(summary["pending"], 2)
        self.assertEqual(summary["pending_dependency"], 1)

    def test_partition_wait_excludes_unschedulable_jobs(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        partitions = [{"name": "compute", "state": "UP", "state_code": 1, "default": 1,
                       "hidden": 0, "max_time": "", "max_time_seconds": 0,
                       "priority_tier": 0, "total_nodes_configured": 0,
                       "total_cpus_configured": 0}]
        merged = collector.merge_partitions(
            partitions, [],
            [self.pending("Dependency", 30 * 86400), self.pending("Resources", 900)])
        self.assertEqual(merged[0]["oldest_pending_age"], 900)
        self.assertEqual(merged[0]["jobs_pending"], 2)

    def test_partition_wait_is_zero_when_everything_is_blocked(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        partitions = [{"name": "compute", "state": "UP", "state_code": 1, "default": 1,
                       "hidden": 0, "max_time": "", "max_time_seconds": 0,
                       "priority_tier": 0, "total_nodes_configured": 0,
                       "total_cpus_configured": 0}]
        merged = collector.merge_partitions(
            partitions, [], [self.pending("Dependency", 30 * 86400)])
        self.assertEqual(merged[0]["oldest_pending_age"], 0)


class ReasonAnnotationTest(unittest.TestCase):
    def test_parses_user_and_timestamp(self):
        text, user, when = sz.parse_reason_annotation(
            "hardware maintenance scheduled [root@2024-05-03T02:15:00]")
        self.assertEqual(text, "hardware maintenance scheduled")
        self.assertEqual(user, "root")
        self.assertEqual(when, sz.parse_slurm_datetime("2024-05-03T02:15:00"))

    def test_reason_without_annotation(self):
        self.assertEqual(sz.parse_reason_annotation("just a reason"),
                         ("just a reason", "", None))

    def test_empty_reason(self):
        self.assertEqual(sz.parse_reason_annotation(""), ("", "", None))

    def test_unavailable_age_is_zero_for_usable_nodes(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        collector.now = FIXTURE_NOW
        nodes = dict((node["name"], node) for node in collector.collect_nodes())
        # n005 is drained with a reason set 2024-05-03T02:15:00.
        self.assertEqual(nodes["n005"]["reason_user"], "root")
        self.assertEqual(nodes["n005"]["reason_text"], "hardware maintenance scheduled")
        self.assertEqual(nodes["n005"]["unavailable_age"],
                         FIXTURE_NOW - sz.parse_slurm_datetime("2024-05-03T02:15:00"))
        # n001 is healthy, so it reports no outage even though time has passed.
        self.assertEqual(nodes["n001"]["unavailable_age"], 0)

    def test_cluster_reports_the_longest_outage(self):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        collector.now = FIXTURE_NOW
        nodes = collector.collect_nodes()
        summary, _, _, _ = collector.summarise_nodes(nodes)
        self.assertEqual(summary["longest_unavailable_age"],
                         max(node["unavailable_age"] for node in nodes))
        self.assertGreater(summary["longest_unavailable_age"], 0)


class LicenseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.licenses = dict((entry["name"], entry) for entry in
                            sz.SlurmCollector(bin_dir=FAKEBIN).collect_licenses())

    def test_parses_every_license(self):
        self.assertEqual(sorted(self.licenses), ["abaqus", "ansys", "matlab"])

    def test_usage(self):
        self.assertEqual(self.licenses["ansys"]["total"], 100)
        self.assertEqual(self.licenses["ansys"]["used"], 30)
        self.assertEqual(self.licenses["ansys"]["free"], 70)
        self.assertAlmostEqual(self.licenses["ansys"]["utilization"], 30.0)

    def test_exhausted_license(self):
        self.assertEqual(self.licenses["matlab"]["free"], 0)
        self.assertAlmostEqual(self.licenses["matlab"]["utilization"], 100.0)

    def test_remote_and_reserved(self):
        self.assertEqual(self.licenses["abaqus"]["remote"], 1)
        self.assertEqual(self.licenses["abaqus"]["reserved"], 2)

    def test_cluster_without_licenses(self):
        empty = tempfile.mkdtemp(prefix="slurm-nolicense")
        try:
            self.assertEqual(sz.SlurmCollector(bin_dir=empty).collect_licenses(), [])
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class ReservationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        collector.now = FIXTURE_NOW
        cls.reservations, cls.summary = collector.collect_reservations()
        cls.by_name = dict((entry["name"], entry) for entry in cls.reservations)

    def test_discovers_every_reservation(self):
        self.assertEqual(sorted(self.by_name), ["maint_may", "training_june"])

    def test_summary_still_matches_the_list(self):
        self.assertEqual(self.summary["reservations_total"], len(self.reservations))
        self.assertEqual(self.summary["reservations_active"], 1)
        self.assertEqual(self.summary["reservations_nodes"], 2)

    def test_active_maintenance_reservation(self):
        entry = self.by_name["maint_may"]
        self.assertEqual(entry["active"], 1)
        self.assertEqual(entry["maintenance"], 1)
        self.assertEqual(entry["nodes"], 2)
        self.assertEqual(entry["cores"], 64)
        self.assertEqual(entry["partition"], "compute")
        self.assertEqual(entry["duration"], 12 * 3600)
        self.assertEqual(entry["remaining"], 10 * 3600)  # ends at 22:00, now is 12:00
        self.assertEqual(entry["starts_in"], 0)

    def test_future_reservation(self):
        entry = self.by_name["training_june"]
        self.assertEqual(entry["active"], 0)
        self.assertEqual(entry["maintenance"], 0)
        self.assertGreater(entry["starts_in"], 0)
        self.assertEqual(entry["users"], "")       # "(null)" is normalised away
        self.assertEqual(entry["accounts"], "training")


class AccountingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        collector = sz.SlurmCollector(bin_dir=FAKEBIN)
        collector.now = FIXTURE_NOW
        cls.stats = collector.collect_accounting(3600)

    def test_job_counts_by_ending(self):
        self.assertEqual(self.stats["jobs_total"], 8)
        self.assertEqual(self.stats["jobs_completed"], 2)
        self.assertEqual(self.stats["jobs_failed"], 1)
        self.assertEqual(self.stats["jobs_cancelled"], 1)
        self.assertEqual(self.stats["jobs_timeout"], 1)
        self.assertEqual(self.stats["jobs_node_fail"], 1)
        self.assertEqual(self.stats["jobs_out_of_memory"], 1)
        self.assertEqual(self.stats["jobs_preempted"], 1)
        self.assertEqual(self.stats["jobs_other"], 0)

    def test_rates(self):
        self.assertAlmostEqual(self.stats["success_rate"], 25.0)
        # Cancellations are a user action and are not counted as failures.
        self.assertAlmostEqual(self.stats["failure_rate"], 50.0)

    def test_wait_and_runtime(self):
        # The cancelled job never started, so it is excluded from wait stats.
        self.assertEqual(self.stats["wait_mean"], 120)
        self.assertEqual(self.stats["wait_max"], 300)
        self.assertEqual(self.stats["elapsed_mean"], 1132)
        self.assertEqual(self.stats["elapsed_max"], 3600)

    def test_cpu_hours(self):
        self.assertAlmostEqual(self.stats["cpu_hours"], 102.533, places=3)

    def test_window_is_reported(self):
        self.assertEqual(self.stats["window"], 3600)

    def test_document_shape(self):
        document = sz.SlurmCollector(bin_dir=FAKEBIN).collect_accounting_document(3600)
        self.assertEqual(sorted(document), ["accounting", "meta"])
        self.assertEqual(document["meta"]["error_count"], 0)


class SliceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = sz.SlurmCollector(bin_dir=FAKEBIN).collect()

    def test_cluster_mode_omits_node_array(self):
        payload = sz.slice_document(self.document, "cluster")
        self.assertNotIn("nodes", payload)
        self.assertIn("partitions", payload)
        self.assertIn("nodes_summary", payload)

    def test_nodes_mode_is_node_array_only(self):
        payload = sz.slice_document(self.document, "nodes")
        self.assertEqual(sorted(payload), ["meta", "nodes"])
        self.assertEqual(len(payload["nodes"]), 9)

    def test_unknown_mode(self):
        self.assertRaises(ValueError, sz.slice_document, self.document, "bogus")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="slurm-zabbix-test")
        self.cache = os.path.join(self.directory, "cache.json")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def read_cache(self):
        with open(self.cache) as handle:
            return json.load(handle)

    def write_cache(self, document):
        with open(self.cache, "w") as handle:
            json.dump(document, handle)

    def run_collector(self, *extra):
        argv = [sys.executable, COLLECTOR, "--slurm-bin-dir", FAKEBIN,
                "--cache-file", self.cache] + list(extra)
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True)
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def test_cli_writes_and_reuses_the_cache(self):
        code, stdout, stderr = self.run_collector("--mode", "cluster")
        self.assertEqual(code, 0, stderr)
        first = json.loads(stdout)
        self.assertEqual(first["meta"]["cached"], 0)
        self.assertTrue(os.path.exists(self.cache))

        code, stdout, _ = self.run_collector("--mode", "nodes")
        self.assertEqual(code, 0)
        second = json.loads(stdout)
        self.assertEqual(second["meta"]["cached"], 1)
        self.assertEqual(second["meta"]["timestamp"], first["meta"]["timestamp"])
        self.assertEqual(len(second["nodes"]), 9)

    def test_expired_cache_is_refreshed(self):
        self.run_collector("--mode", "cluster")
        stale = self.read_cache()
        stale["meta"]["timestamp"] = int(time.time()) - 3600
        self.write_cache(stale)

        code, stdout, _ = self.run_collector("--mode", "cluster", "--cache-ttl", "60")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["meta"]["cached"], 0)

    def test_cache_only_without_cache_returns_error_document(self):
        code, stdout, stderr = self.run_collector("--mode", "cluster", "--cache-only")
        self.assertEqual(code, 0, stderr)
        document = json.loads(stdout)
        self.assertEqual(document["meta"]["error_count"], 1)
        self.assertIn("missing", document["meta"]["errors"])
        # The document must stay well formed so dependent items keep working.
        self.assertEqual(document["cluster"]["slurmctld_up"], 0)

    def test_cache_only_reports_age(self):
        self.run_collector("--mode", "cluster")
        aged = self.read_cache()
        aged["meta"]["timestamp"] = int(time.time()) - 300
        self.write_cache(aged)

        code, stdout, _ = self.run_collector("--mode", "cluster", "--cache-only")
        self.assertEqual(code, 0)
        document = json.loads(stdout)
        self.assertEqual(document["meta"]["cached"], 1)
        self.assertGreaterEqual(document["meta"]["age"], 300)

    def test_refresh_updates_cache_quietly(self):
        code, stdout, stderr = self.run_collector("--refresh")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.strip(), "")
        self.assertTrue(os.path.exists(self.cache))
        self.assertEqual(self.read_cache()["cluster"]["name"], "hpc-prod")

    def test_refresh_stays_quiet_with_an_explicit_mode(self):
        """The systemd timer refreshes the accounting cache the same way."""
        code, stdout, stderr = self.run_collector("--refresh", "--mode", "accounting")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.strip(), "")
        self.assertTrue(os.path.exists(sz.accounting_cache_file(self.cache)))

    def test_refresh_collects_even_when_the_cache_is_fresh(self):
        self.run_collector("--mode", "cluster")
        first = self.read_cache()["meta"]["timestamp"]
        time.sleep(1.1)
        self.run_collector("--refresh")
        self.assertGreater(self.read_cache()["meta"]["timestamp"], first)

    def test_output_is_a_single_line(self):
        code, stdout, _ = self.run_collector("--mode", "nodes")
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.strip().splitlines()), 1)


class CollectionLockTest(unittest.TestCase):
    """Concurrent agent checks must not each sweep Slurm for the same data."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="slurm-zabbix-lock")
        self.cache = os.path.join(self.directory, "cache.json")
        self.counter = os.path.join(self.directory, "invocations.log")
        # A stub layer that records every Slurm command and is slow enough for
        # a second process to arrive while the first is still collecting.
        self.slowbin = os.path.join(self.directory, "bin")
        os.makedirs(self.slowbin)
        for command in ("scontrol", "squeue", "sdiag", "sacctmgr", "sacct"):
            path = os.path.join(self.slowbin, command)
            with open(path, "w") as handle:
                # The fakebin command already records the invocation through
                # $SLURM_TEST_COUNTER; this layer only makes it slow enough for
                # a second process to arrive while the first is collecting.
                handle.write("#!/bin/sh\nsleep 0.4\nexec %s \"$@\"\n"
                             % os.path.join(FAKEBIN, command))
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def invocations(self, command):
        if not os.path.exists(self.counter):
            return 0
        with open(self.counter) as handle:
            return sum(1 for line in handle if line.startswith(command + " "))

    def start(self, mode):
        environment = os.environ.copy()
        environment["SLURM_TEST_COUNTER"] = self.counter
        return subprocess.Popen(
            [sys.executable, COLLECTOR, "--mode", mode, "--slurm-bin-dir", self.slowbin,
             "--cache-file", self.cache, "--lock-timeout", "60"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=environment)

    def test_concurrent_invocations_collect_once(self):
        first, second = self.start("cluster"), self.start("nodes")
        first_out, first_err = first.communicate()
        second_out, second_err = second.communicate()

        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)

        # sdiag runs exactly once per collection.
        self.assertEqual(self.invocations("sdiag"), 1,
                         "expected a single collection, got %d" % self.invocations("sdiag"))

        # Both callers still get a complete, identical document.
        cluster = json.loads(first_out)
        nodes = json.loads(second_out)
        self.assertEqual(cluster["meta"]["timestamp"], nodes["meta"]["timestamp"])
        self.assertEqual(len(nodes["nodes"]), 9)
        self.assertEqual(cluster["nodes_summary"]["total"], 9)
        self.assertEqual(cluster["meta"]["error_count"], 0)

    def test_the_waiting_caller_reads_the_fresh_cache(self):
        first, second = self.start("cluster"), self.start("cluster")
        first_document = json.loads(first.communicate()[0])
        second_document = json.loads(second.communicate()[0])
        # One collected, the other served the cache the first one wrote.
        self.assertEqual(sorted([first_document["meta"]["cached"],
                                 second_document["meta"]["cached"]]), [0, 1])

    def test_lock_failure_does_not_prevent_collection(self):
        """With the lock unavailable the collector still returns data."""
        lock = sz.CollectionLock(os.path.join(self.directory, "busy.lock"), timeout=0)
        self.assertTrue(lock.acquire())
        try:
            blocked = sz.CollectionLock(os.path.join(self.directory, "busy.lock"), timeout=0)
            self.assertFalse(blocked.acquire())
            blocked.release()
        finally:
            lock.release()

        # And once released the lock is free again.
        again = sz.CollectionLock(os.path.join(self.directory, "busy.lock"), timeout=0)
        self.assertTrue(again.acquire())
        again.release()


class AccountingCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="slurm-zabbix-acct")
        self.cache = os.path.join(self.directory, "cache.json")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def run_collector(self, *extra):
        argv = [sys.executable, COLLECTOR, "--slurm-bin-dir", FAKEBIN,
                "--cache-file", self.cache] + list(extra)
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True)
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def test_accounting_uses_its_own_cache_file(self):
        code, stdout, stderr = self.run_collector("--mode", "accounting")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["accounting"]["jobs_total"], 8)
        self.assertTrue(os.path.exists(sz.accounting_cache_file(self.cache)))
        # The cluster cache is untouched: the two run on different schedules.
        self.assertFalse(os.path.exists(self.cache))

    def test_accounting_does_not_disturb_the_cluster_cache(self):
        self.run_collector("--mode", "cluster")
        self.run_collector("--mode", "accounting")
        code, stdout, _ = self.run_collector("--mode", "cluster")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["nodes_summary"]["total"], 9)

    def test_accounting_is_cached(self):
        self.run_collector("--mode", "accounting")
        code, stdout, _ = self.run_collector("--mode", "accounting")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["meta"]["cached"], 1)


class FailureHandlingTest(unittest.TestCase):
    def test_missing_slurm_commands_produce_an_error_document(self):
        empty = tempfile.mkdtemp(prefix="slurm-zabbix-empty")
        try:
            collector = sz.SlurmCollector(bin_dir=empty)
            document = collector.collect()
        finally:
            shutil.rmtree(empty, ignore_errors=True)

        self.assertGreater(document["meta"]["error_count"], 0)
        self.assertEqual(document["cluster"]["slurmctld_up"], 0)
        self.assertEqual(document["nodes_summary"]["total"], 0)
        self.assertEqual(document["partitions"], [])
        json.dumps(document)

    def test_strict_mode_exit_code(self):
        empty = tempfile.mkdtemp(prefix="slurm-zabbix-empty")
        cache = os.path.join(empty, "cache.json")
        try:
            argv = [sys.executable, COLLECTOR, "--slurm-bin-dir", empty,
                    "--cache-file", cache, "--strict", "--mode", "cluster"]
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       universal_newlines=True)
            stdout, _ = process.communicate()
            self.assertEqual(process.returncode, 1)
            json.loads(stdout)  # still valid JSON
        finally:
            shutil.rmtree(empty, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
