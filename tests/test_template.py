#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the generated Zabbix template.

Besides asserting that the shipped template validates, these tests inject
faults into the XML and assert that the validator reports them.  A validator
that cannot fail is worthless, and the checks it performs are the only thing
standing between a typo and a template that imports cleanly but collects no
data.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(ROOT, "templates", "slurm_cluster_7.0.xml")
GPU_TEMPLATE = os.path.join(ROOT, "templates", "slurm_gpu_node_7.0.xml")
BUILDER = os.path.join(ROOT, "tools", "build_template.py")

sys.path.insert(0, os.path.join(ROOT, "tools"))

import validate_template as vt  # noqa: E402


class TemplateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = vt.sample_document()
        with open(TEMPLATE) as handle:
            cls.xml = handle.read()

    def validate_xml(self, xml):
        directory = tempfile.mkdtemp(prefix="slurm-template-test")
        try:
            path = os.path.join(directory, "template.xml")
            with open(path, "w") as handle:
                handle.write(xml)
            validator = vt.Validator(path, self.document)
            return validator.run()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def find(self, tree, xpath, predicate=None):
        """Locate exactly one element, so a fault injection can never no-op."""
        matches = [element for element in tree.findall(xpath)
                   if predicate is None or predicate(element)]
        self.assertEqual(len(matches), 1,
                         "expected one match for %s, found %d" % (xpath, len(matches)))
        return matches[0]

    def mutate(self, mutation):
        """Apply a mutation to the template tree and return the validation result."""
        tree = ET.parse(TEMPLATE)
        mutation(tree)
        return self.validate_xml(ET.tostring(tree.getroot(), encoding="unicode"))


class ShippedTemplateTest(TemplateTestCase):
    def test_template_is_valid(self):
        errors, _ = self.validate_xml(self.xml)
        self.assertEqual(errors, [])

    def test_template_is_up_to_date_with_the_builder(self):
        """The committed XML must match what the builder produces."""
        directory = tempfile.mkdtemp(prefix="slurm-template-build")
        try:
            generated = os.path.join(directory, "template.xml")
            generated_gpu = os.path.join(directory, "gpu.xml")
            subprocess.check_call([sys.executable, BUILDER, "-o", generated,
                                   "--gpu-output", generated_gpu],
                                  stderr=subprocess.DEVNULL)
            for built, committed in ((generated, TEMPLATE), (generated_gpu, GPU_TEMPLATE)):
                with open(built) as handle, open(committed) as original:
                    self.assertEqual(handle.read(), original.read(),
                                     "%s is out of date, run: "
                                     "python3 tools/build_template.py" % committed)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_uuids_are_stable_across_builds(self):
        directory = tempfile.mkdtemp(prefix="slurm-template-build")
        try:
            first = os.path.join(directory, "first.xml")
            second = os.path.join(directory, "second.xml")
            for output in (first, second):
                subprocess.check_call(
                    [sys.executable, BUILDER, "-o", output,
                     "--gpu-output", output + ".gpu"], stderr=subprocess.DEVNULL)
            with open(first) as first_handle, open(second) as second_handle:
                self.assertEqual(first_handle.read(), second_handle.read())
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_every_uuid_is_a_version_4_uuid(self):
        """Zabbix refuses the whole import with 'UUIDv4 is expected' otherwise."""
        tree = ET.parse(TEMPLATE)
        values = [element.text for element in tree.iter("uuid")]
        self.assertGreater(len(values), 200)
        self.assertEqual(len(values), len(set(values)), "uuids must be unique")
        for value in values:
            self.assertRegex(value, r"^[0-9a-f]{32}$")
            parsed = uuid.UUID(value)
            self.assertEqual(parsed.version, 4, "%s is a v%s uuid" % (value, parsed.version))
            self.assertEqual((parsed.int >> 62) & 0x3, 0x2,
                             "%s has the wrong variant bits" % value)

    def test_dashboard_does_not_auto_start_the_slideshow(self):
        """Six pages rotating away under the reader is not helpful."""
        tree = ET.parse(TEMPLATE)
        dashboards = tree.findall(".//dashboards/dashboard")
        self.assertTrue(dashboards)
        for dashboard in dashboards:
            auto_start = dashboard.find("auto_start")
            self.assertIsNotNone(auto_start, dashboard.find("name").text)
            self.assertEqual(auto_start.text, "NO")

    def test_widget_fields_follow_the_zabbix_7_contract(self):
        """Zabbix 7.0 indexes object fields and wants a reference on graphs.

        Getting this wrong does not fail the import, it just renders empty
        widgets, so it is checked against the shipped file directly.
        """
        tree = ET.parse(TEMPLATE)
        seen_references = set()
        widgets = tree.findall(".//widgets/widget")
        self.assertGreater(len(widgets), 20)
        for widget in widgets:
            kind = widget.find("type").text
            name = widget.find("name").text
            fields = dict((field.find("name").text, field.find("type").text)
                          for field in widget.findall("./fields/field"))

            for legacy in ("itemid", "graphid"):
                self.assertNotIn(legacy, fields,
                                 "%s: %r must be indexed as %r" % (name, legacy, legacy + ".0"))
            if kind == "item":
                self.assertEqual(fields.get("itemid.0"), "ITEM", name)
                self.assertNotIn("reference", fields, name)
            elif kind == "graph":
                self.assertEqual(fields.get("graphid.0"), "GRAPH", name)
                self.assertIn("reference", fields, name)
            elif kind == "graphprototype":
                self.assertEqual(fields.get("graphid.0"), "GRAPH_PROTOTYPE", name)
                self.assertIn("columns", fields, name)
                self.assertIn("reference", fields, name)
            else:
                self.fail("unexpected widget type %r" % kind)

            for field in widget.findall("./fields/field"):
                if field.find("name").text == "reference":
                    value = field.find("value").text
                    self.assertRegex(value, r"^[A-Z]{5}$")
                    self.assertNotIn(value, seen_references, "duplicate reference " + value)
                    seen_references.add(value)

    def test_string_functions_never_take_an_item_reference(self):
        """length(/host/key) does not parse; it has to be length(last(/host/key))."""
        tree = ET.parse(TEMPLATE)
        expressions = [element.text for element in tree.iter("expression")]
        self.assertGreater(len(expressions), 30)
        for expression in expressions:
            for function in vt.VALUE_ONLY_FUNCTIONS:
                self.assertNotRegex(expression, r"\b%s\(\s*/" % function)
        self.assertTrue(any("length(last(/" in expression for expression in expressions),
                        "expected at least one length(last(...)) expression")

    def test_idle_cluster_triggers_are_guarded_by_runnable_work(self):
        """The backfill scheduler only runs when something can actually be run.

        Guarding on the raw pending count is not enough: a queue made up of
        dependency-blocked jobs gives the backfill scheduler nothing to do, so
        the age of its last cycle grows while the cluster is perfectly healthy.
        """
        tree = ET.parse(TEMPLATE)
        trigger = self.find(
            tree, "./triggers/trigger",
            lambda element: element.find("name").text.startswith(
                "Slurm: Backfill scheduler has not run"))
        expression = trigger.find("expression").text
        self.assertIn("slurm.backfill.last_cycle_age", expression)
        self.assertRegex(expression,
                         r"min\(/[^)]*slurm\.jobs\.pending\.schedulable,[^)]*\)>0")
        # The raw pending count would include jobs that can never be backfilled.
        self.assertNotRegex(expression, r"slurm\.jobs\.pending,")

    def test_blocked_jobs_trigger_ignores_legitimately_waiting_jobs(self):
        """Dependencies, holds, licences and reservations are not "blocked"."""
        tree = ET.parse(TEMPLATE)
        trigger = self.find(
            tree, "./triggers/trigger",
            lambda element: element.find("name").text.startswith(
                "Slurm: Jobs are blocked"))
        expression = trigger.find("expression").text
        self.assertIn("slurm.jobs.pending.limited", expression)
        # The raw pending count would include dependency-blocked jobs.
        self.assertNotRegex(expression, r"slurm\.jobs\.pending,")

    def test_uses_the_standard_template_group_uuid(self):
        """Templates/Applications already exists in every Zabbix installation."""
        tree = ET.parse(TEMPLATE)
        group = tree.find("./template_groups/template_group")
        self.assertEqual(group.find("name").text, "Templates/Applications")
        self.assertEqual(group.find("uuid").text, "a571c0d144b14fd4a87a9d9b2aa9fcd6")

    def test_expected_content(self):
        validator = vt.Validator(TEMPLATE, self.document)
        validator.run()
        summary = validator.summary()
        self.assertGreater(summary["items"], 90)
        self.assertEqual(summary["discovery rules"], 5)
        self.assertGreater(summary["triggers"], 15)
        self.assertGreater(summary["trigger prototypes"], 10)
        self.assertGreater(summary["graphs"], 10)
        self.assertGreater(summary["dashboard widgets"], 20)

        for key in ("slurm.cluster", "slurm.nodes", "slurm.accounting"):
            self.assertIn(key, validator.items)

    def test_every_dependent_item_hangs_off_a_master_item(self):
        tree = ET.parse(TEMPLATE)
        masters = set()
        dependent = 0
        for item in tree.findall(".//item") + tree.findall(".//item_prototype"):
            master = item.find("./master_item/key")
            if master is not None:
                masters.add(master.text)
                dependent += 1
        self.assertEqual(masters, {"slurm.cluster", "slurm.nodes", "slurm.accounting"})
        self.assertGreater(dependent, 100)

    def test_the_only_agent_checks_are_the_master_items(self):
        tree = ET.parse(TEMPLATE)
        polled = [item for item in tree.findall(".//items/item")
                  if item.find("./master_item") is None]
        keys = sorted(item.find("key").text for item in polled)
        self.assertEqual(keys, ["slurm.accounting", "slurm.cluster", "slurm.nodes"])

    def test_accounting_is_disabled_by_default(self):
        """sacct is expensive, so the default cost stays at two agent checks."""
        tree = ET.parse(TEMPLATE)
        enabled = []
        for item in tree.findall(".//items/item"):
            if item.find("./master_item") is not None:
                continue
            status = item.find("status")
            if status is None or status.text != "DISABLED":
                enabled.append(item.find("key").text)
        self.assertEqual(sorted(enabled), ["slurm.cluster", "slurm.nodes"])

        # The dependent items stay enabled, so switching the master item on is
        # all it takes to turn the feature on.
        accounting = [item for item in tree.findall(".//items/item")
                      if (item.find("./master_item") is not None and
                          item.find("./master_item/key").text == "slurm.accounting")]
        self.assertGreater(len(accounting), 10)
        for item in accounting:
            self.assertIsNone(item.find("status"), item.find("key").text)

    def test_discovers_every_slurm_object(self):
        tree = ET.parse(TEMPLATE)
        rules = sorted(rule.find("key").text
                       for rule in tree.findall(".//discovery_rule"))
        self.assertEqual(rules, [
            "slurm.licenses.discovery",
            "slurm.nodes.discovery",
            "slurm.partitions.discovery",
            "slurm.qos.discovery",
            "slurm.reservations.discovery",
        ])

    def test_reservations_are_cleaned_up_quickly(self):
        """Reservations come and go; lost ones should not linger for a week."""
        tree = ET.parse(TEMPLATE)
        rule = self.find(tree, ".//discovery_rule",
                         lambda element: element.find("key").text == "slurm.reservations.discovery")
        self.assertEqual(rule.find("lifetime").text, "1d")


class GpuTemplateTest(TemplateTestCase):
    """The GPU node template, linked to compute nodes rather than the cluster."""

    @classmethod
    def setUpClass(cls):
        super(GpuTemplateTest, cls).setUpClass()
        cls.tree = ET.parse(GPU_TEMPLATE)

    def test_is_valid(self):
        with open(GPU_TEMPLATE) as handle:
            errors, _ = self.validate_xml(handle.read())
        self.assertEqual(errors, [])

    def test_is_a_separate_template(self):
        template = self.tree.find("./templates/template")
        self.assertEqual(template.find("template").text,
                         "Slurm GPU node by Zabbix agent")
        # It must not collide with the cluster template's items.
        cluster = set(item.find("key").text
                      for item in ET.parse(TEMPLATE).findall(".//items/item"))
        gpu = set(item.find("key").text for item in self.tree.findall(".//items/item"))
        self.assertEqual(cluster & gpu, set())

    def test_one_agent_check(self):
        polled = [item for item in self.tree.findall(".//items/item")
                  if item.find("./master_item") is None]
        self.assertEqual([item.find("key").text for item in polled], ["slurm.gpu"])

    def test_reports_allocation_and_utilisation(self):
        keys = set(item.find("key").text for item in self.tree.findall(".//items/item"))
        for expected in ("slurm.gpu.allocated", "slurm.gpu.utilization.mean",
                         "slurm.gpu.allocated_idle", "slurm.gpu.busy",
                         "slurm.gpu.allocation"):
            self.assertIn(expected, keys)

    def test_the_graph_compares_the_two(self):
        graph = self.find(self.tree, "./graphs/graph",
                          lambda element: "Allocation against utilisation"
                          in element.find("name").text)
        keys = [item.find("key").text
                for item in graph.findall("./graph_items/graph_item/item")]
        self.assertIn("slurm.gpu.allocated", keys)
        self.assertIn("slurm.gpu.utilization.mean", keys)
        self.assertIn("slurm.gpu.allocated_idle", keys)

    def test_alerts_on_allocated_but_idle(self):
        trigger = self.find(self.tree, "./triggers/trigger",
                            lambda element: "allocated but idle"
                            in element.find("name").text)
        self.assertIn("slurm.gpu.allocated_idle", trigger.find("expression").text)
        self.assertIn("{$SLURM.GPU.IDLE.TIME}", trigger.find("expression").text)

    def test_discovery_filters_on_a_string_index(self):
        """A JSONPath filter comparing to '0' never matches a numeric 0."""
        rule = self.find(self.tree, ".//discovery_rule",
                         lambda element: element.find("key").text == "slurm.gpu.discovery")
        path = self.find(rule, "./lld_macro_paths/lld_macro_path",
                         lambda element: element.find("lld_macro").text == "{#GPU}")
        self.assertEqual(path.find("path").text, "$.id")
        prototype = self.find(
            self.tree, ".//item_prototype",
            lambda element: element.find("key").text ==
            "slurm.gpu.device.utilization[{#GPU}]")
        self.assertIn("@.id=='{#GPU}'",
                      prototype.find("./preprocessing/step/parameters/parameter").text)

    def test_expressions_reference_the_gpu_template(self):
        for trigger in (self.tree.findall("./triggers/trigger") +
                        self.tree.findall(".//trigger_prototype")):
            self.assertIn("/Slurm GPU node by Zabbix agent/",
                          trigger.find("expression").text)

    def test_uuids_do_not_collide_with_the_cluster_template(self):
        cluster = set(element.text for element in ET.parse(TEMPLATE).iter("uuid"))
        gpu = set(element.text for element in self.tree.iter("uuid"))
        # The shared template group is deliberately the same object.
        self.assertEqual(cluster & gpu, set(["a571c0d144b14fd4a87a9d9b2aa9fcd6"]))


class ValidatorDetectsFaultsTest(TemplateTestCase):
    """Each test breaks the template in one way and expects it to be caught."""

    def assert_detects(self, mutation, fragment):
        errors, _ = self.mutate(mutation)
        self.assertTrue(any(fragment in error for error in errors),
                        "expected an error containing %r, got: %s" % (fragment, errors))

    def item_by_key(self, tree, key):
        return self.find(tree, ".//items/item",
                         lambda element: element.find("key").text == key)

    def test_detects_a_jsonpath_that_never_matches(self):
        def mutation(tree):
            item = self.item_by_key(tree, "slurm.cpus.utilization")
            parameter = item.find("./preprocessing/step/parameters/parameter")
            parameter.text = "$.cpus.utilisation"
        self.assert_detects(mutation, "does not match the collector output")

    def test_detects_a_prototype_field_that_never_matches(self):
        def mutation(tree):
            prototype = self.find(
                tree, ".//item_prototype",
                lambda element: element.find("key").text == "slurm.node.cpu.load[{#NODE}]")
            parameter = prototype.find("./preprocessing/step/parameters/parameter")
            parameter.text = parameter.text.replace("cpu_load", "cpu_loud")
        self.assert_detects(mutation, "never matches")

    def test_detects_an_lld_macro_path_that_never_matches(self):
        def mutation(tree):
            rule = self.find(tree, ".//discovery_rule",
                             lambda element: element.find("key").text == "slurm.nodes.discovery")
            path = self.find(rule, "./lld_macro_paths/lld_macro_path",
                             lambda element: element.find("lld_macro").text == "{#NODE}")
            path.find("path").text = "$.nmae"
        self.assert_detects(mutation, "never matches")

    def test_detects_a_trigger_on_an_unknown_item(self):
        def mutation(tree):
            trigger = self.find(
                tree, "./triggers/trigger",
                lambda element: element.find("name").text.startswith("Slurm: Job backlog"))
            trigger.find("expression").text = trigger.find("expression").text.replace(
                "slurm.jobs.pending", "slurm.jobs.pendign")
        self.assert_detects(mutation, "unknown item")

    def test_detects_a_trigger_referencing_another_host(self):
        def mutation(tree):
            trigger = tree.find("./triggers/trigger")
            trigger.find("expression").text = "last(/Some other host/slurm.nodes.total)=0"
        self.assert_detects(mutation, "references host")

    def test_detects_opdata_referencing_a_missing_item(self):
        def mutation(tree):
            trigger = self.find(
                tree, "./triggers/trigger",
                lambda element: element.find("name").text == "Slurm: slurmctld is not responding")
            ET.SubElement(trigger, "opdata").text = "{ITEM.LASTVALUE4}"
        self.assert_detects(mutation, "only 1 item references")

    def test_detects_a_graph_on_an_unknown_item(self):
        def mutation(tree):
            graph = self.find(tree, "./graphs/graph",
                              lambda element: element.find("name").text == "Slurm: Nodes by state")
            graph.find("./graph_items/graph_item/item/key").text = "slurm.nodes.idl"
        self.assert_detects(mutation, "unknown item")

    def test_detects_a_widget_on_an_unknown_graph(self):
        def mutation(tree):
            field = self.find(
                tree, ".//widgets/widget/fields/field",
                lambda element: (element.find("type").text == "GRAPH" and
                                 element.find("value/name").text == "Slurm: Nodes by state"))
            field.find("value/name").text = "Slurm: Nodes by staet"
        self.assert_detects(mutation, "unknown graph")

    def test_detects_a_widget_on_an_unknown_item(self):
        def mutation(tree):
            field = self.find(
                tree, ".//widgets/widget/fields/field",
                lambda element: (element.find("type").text == "ITEM" and
                                 element.find("value/key").text == "slurm.nodes.available"))
            field.find("value/key").text = "slurm.nodes.availabl"
        self.assert_detects(mutation, "unknown item")

    def test_detects_a_string_function_on_an_item_reference(self):
        def mutation(tree):
            trigger = self.find(
                tree, "./triggers/trigger",
                lambda element: element.find("name").text == "Slurm: Version has changed")
            trigger.find("expression").text = (
                "length(/Slurm cluster by Zabbix agent/slurm.cluster.version)>0")
        self.assert_detects(mutation, "takes a value, not an item reference")

    def test_detects_an_unindexed_widget_field(self):
        def mutation(tree):
            field = self.find(
                tree, ".//widgets/widget/fields/field",
                lambda element: (element.find("type").text == "ITEM" and
                                 element.find("value/key").text == "slurm.nodes.available"))
            field.find("name").text = "itemid"
        self.assert_detects(mutation, "unindexed field name")

    def test_detects_a_graph_widget_without_a_reference(self):
        def mutation(tree):
            widget = self.find(tree, ".//widgets/widget",
                               lambda element: element.find("name").text == "Nodes by state")
            fields = widget.find("fields")
            for field in list(fields):
                if field.find("name").text == "reference":
                    fields.remove(field)
        self.assert_detects(mutation, "no reference field")

    def test_detects_duplicate_widget_references(self):
        def mutation(tree):
            fields = [field for field in tree.findall(".//widgets/widget/fields/field")
                      if field.find("name").text == "reference"]
            fields[1].find("value").text = fields[0].find("value").text
        self.assert_detects(mutation, "share the reference")

    def test_detects_a_missing_widget_object_field(self):
        def mutation(tree):
            widget = self.find(tree, ".//widgets/widget",
                               lambda element: element.find("name").text == "Nodes available")
            fields = widget.find("fields")
            for field in list(fields):
                if field.find("name").text == "itemid.0":
                    fields.remove(field)
        self.assert_detects(mutation, "missing the ITEM field")

    def test_detects_a_non_v4_uuid(self):
        def mutation(tree):
            element = tree.find(".//items/item/uuid")
            # A uuid5 value: right shape, wrong version.
            element.text = uuid.uuid5(uuid.NAMESPACE_DNS, "example").hex
        self.assert_detects(mutation, "UUIDv4 is expected")

    def test_detects_a_malformed_uuid(self):
        def mutation(tree):
            tree.find(".//items/item/uuid").text = "not-a-uuid"
        self.assert_detects(mutation, "invalid uuid")

    def test_detects_duplicate_uuids(self):
        def mutation(tree):
            uuids = tree.findall(".//items/item/uuid")
            uuids[1].text = uuids[0].text
        self.assert_detects(mutation, "uuid collides")

    def test_detects_an_undefined_macro(self):
        def mutation(tree):
            macro = self.find(tree, ".//macros/macro",
                              lambda element: element.find("macro").text == "{$SLURM.CPU.UTIL.HIGH}")
            macro.find("macro").text = "{$SLURM.CPU.UTIL.HUGE}"
        self.assert_detects(mutation, "has no default in the template")

    def test_detects_overlapping_widgets(self):
        def mutation(tree):
            widget = self.find(
                tree, ".//widgets/widget",
                lambda element: element.find("name").text == "Nodes down")
            widget.find("x").text = "6"
        self.assert_detects(mutation, "overlap")

    def test_detects_a_widget_outside_the_grid(self):
        def mutation(tree):
            widget = self.find(
                tree, ".//widgets/widget",
                lambda element: element.find("name").text == "Jobs pending")
            widget.find("x").text = "68"
        self.assert_detects(mutation, "exceeds")

    def test_detects_a_dependent_item_without_master(self):
        def mutation(tree):
            item = self.item_by_key(tree, "slurm.nodes.total")
            item.remove(item.find("master_item"))
        self.assert_detects(mutation, "without master item")

    def test_detects_a_dependent_item_on_a_missing_master(self):
        def mutation(tree):
            item = self.item_by_key(tree, "slurm.nodes.total")
            item.find("./master_item/key").text = "slurm.clustre"
        self.assert_detects(mutation, "does not exist")

    def test_detects_a_trigger_dependency_on_a_missing_trigger(self):
        def mutation(tree):
            trigger = self.find(
                tree, "./triggers/trigger",
                lambda element: element.find("name").text.startswith("Slurm: No data collected"))
            trigger.find("name").text = "Slurm: No data collected, renamed"
        self.assert_detects(mutation, "depends on unknown trigger")

    def test_detects_an_unknown_value_map(self):
        def mutation(tree):
            item = self.item_by_key(tree, "slurm.ctld.available")
            item.find("./valuemap/name").text = "Nonexistent map"
        self.assert_detects(mutation, "unknown value map")

    def test_detects_an_invalid_priority(self):
        def mutation(tree):
            tree.find("./triggers/trigger/priority").text = "CRITICAL"
        self.assert_detects(mutation, "invalid priority")

    def test_detects_a_prototype_using_an_undiscovered_macro(self):
        def mutation(tree):
            prototype = self.find(
                tree, ".//item_prototype",
                lambda element: element.find("key").text == "slurm.qos.priority[{#QOS}]")
            prototype.find("name").text = "QOS [{#QOS}] on [{#CLUSTER}]: Priority"
        self.assert_detects(mutation, "does not discover")


if __name__ == "__main__":
    unittest.main()
