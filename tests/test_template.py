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
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE = os.path.join(ROOT, "templates", "slurm_cluster_7.0.xml")
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
            subprocess.check_call([sys.executable, BUILDER, "-o", generated],
                                  stderr=subprocess.DEVNULL)
            with open(generated) as handle:
                self.assertEqual(handle.read(), self.xml,
                                 "templates/slurm_cluster_7.0.xml is out of date, "
                                 "run: python3 tools/build_template.py")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_uuids_are_stable_across_builds(self):
        directory = tempfile.mkdtemp(prefix="slurm-template-build")
        try:
            first = os.path.join(directory, "first.xml")
            second = os.path.join(directory, "second.xml")
            for output in (first, second):
                subprocess.check_call([sys.executable, BUILDER, "-o", output],
                                      stderr=subprocess.DEVNULL)
            with open(first) as first_handle, open(second) as second_handle:
                self.assertEqual(first_handle.read(), second_handle.read())
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_expected_content(self):
        validator = vt.Validator(TEMPLATE, self.document)
        validator.run()
        summary = validator.summary()
        self.assertGreater(summary["items"], 90)
        self.assertEqual(summary["discovery rules"], 3)
        self.assertGreater(summary["triggers"], 15)
        self.assertGreater(summary["trigger prototypes"], 10)
        self.assertGreater(summary["graphs"], 10)
        self.assertGreater(summary["dashboard widgets"], 20)

        self.assertEqual(sorted(validator.rules), [
            "slurm.nodes.discovery", "slurm.partitions.discovery", "slurm.qos.discovery"])
        for key in ("slurm.cluster", "slurm.nodes"):
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
        self.assertEqual(masters, {"slurm.cluster", "slurm.nodes"})
        self.assertGreater(dependent, 100)

    def test_no_agent_check_besides_the_two_master_items(self):
        """The whole template must cost exactly two agent checks per interval."""
        tree = ET.parse(TEMPLATE)
        polled = [item for item in tree.findall(".//items/item")
                  if item.find("./master_item") is None]
        keys = sorted(item.find("key").text for item in polled)
        self.assertEqual(keys, ["slurm.cluster", "slurm.nodes"])


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
