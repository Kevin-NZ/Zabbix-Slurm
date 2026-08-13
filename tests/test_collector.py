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

    def test_output_is_a_single_line(self):
        code, stdout, _ = self.run_collector("--mode", "nodes")
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.strip().splitlines()), 1)


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
