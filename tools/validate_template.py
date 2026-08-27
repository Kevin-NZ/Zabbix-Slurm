#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the generated Zabbix template.

Zabbix only reports the first error it hits during an import, and some mistakes
(a JSONPath that never matches, a widget referencing a graph that was renamed)
import cleanly and simply produce no data.  This script checks the export
up front:

* structure: UUIDs, unique keys, value types, dependent items pointing at an
  existing master item;
* references: trigger expressions, trigger dependencies, graph items and
  dashboard widgets may only reference items that exist;
* macros: every {$MACRO} used has a default in the template, every {#MACRO}
  used by a prototype is produced by its discovery rule;
* dashboards: widgets stay inside the 72x64 grid and do not overlap;
* data: every JSONPath is resolved against a real document produced by the
  collector, so a path that can never match is reported as an error.

Usage:  python3 tools/validate_template.py [template.xml]
"""

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_TEMPLATE = os.path.join(ROOT, "templates", "slurm_cluster_7.0.xml")
COLLECTOR = os.path.join(ROOT, "bin", "slurm_zabbix.py")
FAKEBIN = os.path.join(ROOT, "tests", "fakebin")

DASHBOARD_COLUMNS = 72
DASHBOARD_ROWS = 64

# Zabbix accepts version 4 UUIDs only, and rejects the whole import with
# "UUIDv4 is expected" otherwise: 32 lowercase hex digits, '4' in the version
# position and 8, 9, a or b in the variant position.
UUID_RE = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._-]+(\[.*\])?$")
# An item reference only ever appears as the first argument of a trigger
# function, i.e. "func(/host/key...)".  Anchoring on the function call keeps
# arithmetic such as "100*last(/host/a)/(last(/host/b)+1)" from being misread.
ITEM_REFERENCE_RE = re.compile(r"[a-z_]+\(\s*/([^/,)]+)/((?:[^,)\s]|\[[^\]]*\])+)")
USER_MACRO_RE = re.compile(r"\{\$([A-Z0-9._]+)(?::[^}]*)?\}")
LLD_MACRO_RE = re.compile(r"\{#[A-Z0-9_]+\}")

VALID_PRIORITIES = ("NOT_CLASSIFIED", "INFO", "WARNING", "AVERAGE", "HIGH", "DISASTER")

# Field contract per widget type, taken from the official Zabbix 7.0 templates.
# Object references are indexed ("itemid.0"), and widgets that can broadcast to
# other widgets carry a "reference" that is unique within the dashboard.  Getting
# any of this wrong imports without complaint and renders an empty widget.
WIDGET_CONTRACTS = {
    "item": {"required": (("ITEM", "itemid.0"),), "reference": False},
    "graph": {"required": (("GRAPH", "graphid.0"),), "reference": True},
    "graphprototype": {"required": (("GRAPH_PROTOTYPE", "graphid.0"),
                                    ("INTEGER", "columns")), "reference": True},
}

# Unindexed spellings that used to work in older Zabbix releases.
LEGACY_FIELD_NAMES = ("itemid", "graphid", "itemid_prototype", "graphid_prototype")

# String functions operate on a value, not on an item reference: the value has
# to be produced by a history function first, as in length(last(/host/key)).
# Writing length(/host/key) fails Zabbix's expression validation on import.
VALUE_ONLY_FUNCTIONS = (
    "length", "left", "right", "mid", "trim", "ltrim", "rtrim", "concat",
    "insert", "replace", "repeat", "ascii", "bytelength", "bitlength",
)
VALID_VALUE_TYPES = ("FLOAT", "CHAR", "LOG", "UNSIGNED", "TEXT")

# JSONPath subset used by the template: object navigation, one equality filter
# on an array, and .first().
PATH_TOKEN_RE = re.compile(r"""
      \[\?\(@\.(?P<fkey>[A-Za-z_]\w*)==(?P<quote>['"])(?P<fval>[^'"]*)(?P=quote)\)\]
    | \.first\(\)
    | \.(?P<key>[A-Za-z_][\w]*)
""", re.VERBOSE)


def text_of(element, tag, default=""):
    child = element.find(tag)
    if child is None or child.text is None:
        return default
    return child.text


def resolve_path(document, path):
    """Evaluate the JSONPath subset used by the template.

    Returns the list of matching values, which is empty when the path does not
    match anything.
    """
    if not path.startswith("$"):
        raise ValueError("path does not start with $: %s" % path)

    current = [document]
    position = 1
    while position < len(path):
        match = PATH_TOKEN_RE.match(path, position)
        if match is None:
            raise ValueError("cannot parse %r at offset %d" % (path, position))
        position = match.end()

        if match.group("key"):
            key = match.group("key")
            current = [entry[key] for entry in current
                       if isinstance(entry, dict) and key in entry]
        elif match.group("fkey"):
            key, wanted = match.group("fkey"), match.group("fval")
            selected = []
            for entry in current:
                if isinstance(entry, list):
                    selected.extend(child for child in entry
                                    if isinstance(child, dict) and child.get(key) == wanted)
            current = selected
        else:  # .first()
            current = current[:1]
    return current


class Validator(object):
    def __init__(self, path, document):
        self.path = path
        self.document = document
        self.errors = []
        self.warnings = []
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        self.template = self.root.find("./templates/template")
        self.template_name = text_of(self.template, "template")

        self.items = {}          # key -> element
        self.prototypes = {}     # key -> (element, rule key)
        self.rules = {}          # rule key -> element
        self.valuemaps = set()
        self.macros = set()
        self.graphs = set()
        self.graph_prototypes = {}   # name -> rule key
        self.uuids = {}
        self.triggers = {}       # (name, expression)

        # Sample entity names for resolving prototype paths against real data.
        self.samples = dict(
            ("{#%s}" % macro, [entry["name"] for entry in document.get(key, [])])
            for macro, key in (("PARTITION", "partitions"), ("QOS", "qos"),
                               ("NODE", "nodes"), ("LICENSE", "licenses"),
                               ("RESERVATION", "reservations")))

    # -- helpers ------------------------------------------------------------

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def check_uuid(self, element, label):
        uuid = text_of(element, "uuid")
        if not UUID_RE.match(uuid):
            reason = "not a version 4 UUID" if re.match(r"^[0-9a-f]{32}$", uuid) \
                else "not 32 lowercase hex digits"
            self.error("%s: invalid uuid %r (%s); Zabbix rejects the import with "
                       "'UUIDv4 is expected'" % (label, uuid, reason))
        elif uuid in self.uuids:
            self.error("%s: uuid collides with %s" % (label, self.uuids[uuid]))
        else:
            self.uuids[uuid] = label

    # -- collection ---------------------------------------------------------

    def collect(self):
        for element in self.root.findall("./templates/template/valuemaps/valuemap"):
            self.check_uuid(element, "valuemap %s" % text_of(element, "name"))
            self.valuemaps.add(text_of(element, "name"))

        for element in self.root.findall("./templates/template/macros/macro"):
            self.macros.add(text_of(element, "macro"))

        for element in self.root.findall("./templates/template/items/item"):
            key = text_of(element, "key")
            self.check_uuid(element, "item %s" % key)
            if key in self.items:
                self.error("item %s: duplicate key" % key)
            self.items[key] = element

        for rule in self.root.findall("./templates/template/discovery_rules/discovery_rule"):
            rule_key = text_of(rule, "key")
            self.check_uuid(rule, "discovery rule %s" % rule_key)
            self.rules[rule_key] = rule
            for element in rule.findall("./item_prototypes/item_prototype"):
                key = text_of(element, "key")
                self.check_uuid(element, "item prototype %s" % key)
                if key in self.prototypes or key in self.items:
                    self.error("item prototype %s: duplicate key" % key)
                self.prototypes[key] = (element, rule_key)
            for element in rule.findall("./graph_prototypes/graph_prototype"):
                name = text_of(element, "name")
                self.check_uuid(element, "graph prototype %s" % name)
                self.graph_prototypes[name] = rule_key

        for element in self.root.findall("./graphs/graph"):
            name = text_of(element, "name")
            self.check_uuid(element, "graph %s" % name)
            self.graphs.add(name)

        for element in self.root.findall("./triggers/trigger"):
            name = text_of(element, "name")
            self.check_uuid(element, "trigger %s" % name)
            self.triggers[(name, text_of(element, "expression"))] = element

        for rule in self.root.findall("./templates/template/discovery_rules/discovery_rule"):
            for element in rule.findall("./trigger_prototypes/trigger_prototype"):
                name = text_of(element, "name")
                self.check_uuid(element, "trigger prototype %s" % name)
                self.triggers[(name, text_of(element, "expression"))] = element

        for element in self.root.findall("./templates/template/dashboards/dashboard"):
            self.check_uuid(element, "dashboard %s" % text_of(element, "name"))

    # -- checks -------------------------------------------------------------

    def check_export(self):
        version = text_of(self.root, "version")
        if version != "7.0":
            self.error("export version is %r, expected 7.0" % version)
        if self.root.find("./template_groups/template_group") is None:
            self.error("export has no template group")
        if self.template is None:
            self.error("export has no template")

    def check_items(self):
        all_items = [(key, element, None) for key, element in self.items.items()]
        all_items += [(key, element, rule) for key, (element, rule) in self.prototypes.items()]

        for key, element, rule_key in all_items:
            label = "item %s" % key
            if not KEY_RE.match(key.replace("{#", "").replace("}", "")):
                self.error("%s: invalid key syntax" % label)

            value_type = text_of(element, "value_type")
            if value_type not in VALID_VALUE_TYPES:
                self.error("%s: invalid value type %r" % (label, value_type))
            if value_type in ("CHAR", "TEXT", "LOG") and text_of(element, "trends") != "0":
                self.error("%s: text item must not keep trends" % label)

            valuemap = element.find("./valuemap/name")
            if valuemap is not None and valuemap.text not in self.valuemaps:
                self.error("%s: unknown value map %r" % (label, valuemap.text))
            if valuemap is not None and value_type not in ("UNSIGNED", "FLOAT", "CHAR"):
                self.error("%s: value map on a %s item" % (label, value_type))

            master = element.find("./master_item/key")
            item_type = text_of(element, "type")
            if item_type == "DEPENDENT":
                if master is None:
                    self.error("%s: dependent item without master item" % label)
                elif master.text not in self.items:
                    self.error("%s: master item %r does not exist" % (label, master.text))
                if element.find("delay") is not None:
                    self.error("%s: dependent item must not define a delay" % label)
            else:
                if master is not None:
                    self.error("%s: non dependent item with a master item" % label)
                if element.find("delay") is None:
                    self.error("%s: polled item without a delay" % label)

            # LLD macros used by a prototype must be produced by its rule.
            if rule_key is not None:
                produced = set(path.text for path in
                               self.rules[rule_key].findall("./lld_macro_paths/lld_macro_path/lld_macro"))
                used = set(LLD_MACRO_RE.findall(ET.tostring(element, encoding="unicode")))
                for macro in used - produced:
                    self.error("%s: uses %s which %s does not discover"
                               % (label, macro, rule_key))

    def check_preprocessing(self):
        for key, element, in list(self.items.items()) + \
                [(key, value[0]) for key, value in self.prototypes.items()]:
            steps = element.findall("./preprocessing/step")
            if text_of(element, "type") != "DEPENDENT":
                continue
            if not steps:
                self.error("item %s: dependent item without preprocessing" % key)
                continue
            if text_of(steps[0], "type") != "JSONPATH":
                self.error("item %s: first preprocessing step is not a JSONPath" % key)

    def check_paths(self):
        """Resolve every JSONPath against a document produced by the collector."""
        for key, element in self.items.items():
            step = element.find("./preprocessing/step[type='JSONPATH']")
            if step is None:
                continue
            path = text_of(step, "parameters/parameter")
            self._resolve_or_report("item %s" % key, path, self.document)

        for rule_key, rule in self.rules.items():
            step = rule.find("./preprocessing/step[type='JSONPATH']")
            path = text_of(step, "parameters/parameter") if step is not None else ""
            matches = self._resolve_or_report("discovery rule %s" % rule_key, path,
                                              self.document)
            if matches and not isinstance(matches[0], list):
                self.error("discovery rule %s: %s does not select an array"
                           % (rule_key, path))

            # Every LLD macro path must exist on the discovered entities.
            entities = matches[0] if matches else []
            for macro_path in rule.findall("./lld_macro_paths/lld_macro_path"):
                macro = text_of(macro_path, "lld_macro")
                path = text_of(macro_path, "path")
                if not any(resolve_path(entity, path) for entity in entities):
                    self.error("discovery rule %s: %s -> %s never matches"
                               % (rule_key, macro, path))

        for key, (element, rule_key) in self.prototypes.items():
            step = element.find("./preprocessing/step[type='JSONPATH']")
            if step is None:
                continue
            template_path = text_of(step, "parameters/parameter")
            macros = [macro for macro in LLD_MACRO_RE.findall(template_path)
                      if macro in self.samples]
            if not macros:
                self._resolve_or_report("item prototype %s" % key, template_path,
                                        self.document)
                continue

            # The path has to resolve for at least one discovered entity; a
            # field that is missing everywhere is a typo.
            resolved = False
            for macro in macros:
                for sample in self.samples[macro]:
                    path = template_path.replace(macro, sample)
                    try:
                        if resolve_path(self.document, path):
                            resolved = True
                            break
                    except ValueError as exc:
                        self.error("item prototype %s: %s" % (key, exc))
                        resolved = True
                        break
                if resolved:
                    break
            if not resolved:
                self.error("item prototype %s: %s never matches any %s"
                           % (key, template_path, ", ".join(macros)))

    def _resolve_or_report(self, label, path, document):
        if not path:
            self.error("%s: empty JSONPath" % label)
            return []
        try:
            matches = resolve_path(document, path)
        except ValueError as exc:
            self.error("%s: %s" % (label, exc))
            return []
        if not matches:
            self.error("%s: JSONPath %s does not match the collector output" % (label, path))
        return matches

    def check_triggers(self):
        known = set(self.items) | set(self.prototypes)
        for (name, expression), element in self.triggers.items():
            label = "trigger %s" % name

            priority = text_of(element, "priority")
            if priority not in VALID_PRIORITIES:
                self.error("%s: invalid priority %r" % (label, priority))

            for function in VALUE_ONLY_FUNCTIONS:
                if re.search(r"\b%s\(\s*/" % function, expression):
                    self.error("%s: %s() takes a value, not an item reference; write "
                               "%s(last(/host/key)) instead"
                               % (label, function, function))

            references = ITEM_REFERENCE_RE.findall(expression)
            if not references:
                self.error("%s: expression references no item: %s" % (label, expression))
            for host, key in references:
                if host != self.template_name:
                    self.error("%s: expression references host %r" % (label, host))
                if key not in known:
                    self.error("%s: expression references unknown item %r" % (label, key))

            # {ITEM.LASTVALUEn} in operational data must have a matching item.
            for index in re.findall(r"\{ITEM\.LASTVALUE(\d+)\}", text_of(element, "opdata")):
                if int(index) > len(references):
                    self.error("%s: opdata uses {ITEM.LASTVALUE%s} but the expression has "
                               "only %d item references" % (label, index, len(references)))

            for dependency in element.findall("./dependencies/dependency"):
                target = (text_of(dependency, "name"), text_of(dependency, "expression"))
                if target not in self.triggers:
                    self.error("%s: depends on unknown trigger %r" % (label, target[0]))
                if target == (name, expression):
                    self.error("%s: depends on itself" % label)

    def check_graphs(self):
        known = set(self.items)
        for element in self.root.findall("./graphs/graph"):
            name = text_of(element, "name")
            for reference in element.findall("./graph_items/graph_item/item"):
                key = text_of(reference, "key")
                if text_of(reference, "host") != self.template_name:
                    self.error("graph %s: references another host" % name)
                if key not in known:
                    self.error("graph %s: references unknown item %r" % (name, key))

        for rule_key, rule in self.rules.items():
            rule_prototypes = set(key for key, value in self.prototypes.items()
                                  if value[1] == rule_key)
            for element in rule.findall("./graph_prototypes/graph_prototype"):
                name = text_of(element, "name")
                keys = [text_of(reference, "key")
                        for reference in element.findall("./graph_items/graph_item/item")]
                for key in keys:
                    if key not in rule_prototypes and key not in self.items:
                        self.error("graph prototype %s: references unknown item %r"
                                   % (name, key))
                if not any(key in rule_prototypes for key in keys):
                    self.error("graph prototype %s: contains no item prototype" % name)

    def check_dashboards(self):
        for dashboard in self.root.findall("./templates/template/dashboards/dashboard"):
            dashboard_name = text_of(dashboard, "name")
            self._check_widget_references(dashboard, dashboard_name)
            for page in dashboard.findall("./pages/page"):
                page_name = text_of(page, "name")
                label = "dashboard %s / %s" % (dashboard_name, page_name)
                rectangles = []
                for widget in page.findall("./widgets/widget"):
                    widget_name = text_of(widget, "name")
                    x = int(text_of(widget, "x", "0"))
                    y = int(text_of(widget, "y", "0"))
                    width = int(text_of(widget, "width", "1"))
                    height = int(text_of(widget, "height", "1"))

                    if width < 1 or height < 1:
                        self.error("%s: widget %s has a zero size" % (label, widget_name))
                    if x + width > DASHBOARD_COLUMNS:
                        self.error("%s: widget %s exceeds the %d column grid"
                                   % (label, widget_name, DASHBOARD_COLUMNS))
                    if y + height > DASHBOARD_ROWS:
                        self.error("%s: widget %s exceeds the %d row grid"
                                   % (label, widget_name, DASHBOARD_ROWS))

                    for other_name, other in rectangles:
                        if (x < other[0] + other[2] and other[0] < x + width and
                                y < other[1] + other[3] and other[1] < y + height):
                            self.error("%s: widgets %s and %s overlap"
                                       % (label, other_name, widget_name))
                    rectangles.append((widget_name, (x, y, width, height)))

                    self._check_widget_fields(label, widget_name, widget)

    def _check_widget_references(self, dashboard, dashboard_name):
        """Widget references have to be unique within their dashboard."""
        seen = {}
        for widget in dashboard.findall("./pages/page/widgets/widget"):
            for field in widget.findall("./fields/field"):
                if text_of(field, "name") != "reference":
                    continue
                value = text_of(field, "value")
                name = text_of(widget, "name")
                if not re.match(r"^[A-Z]{5}$", value):
                    self.error("dashboard %s: widget %s has an invalid reference %r"
                               % (dashboard_name, name, value))
                if value in seen:
                    self.error("dashboard %s: widgets %s and %s share the reference %r"
                               % (dashboard_name, seen[value], name, value))
                seen[value] = name

    def _check_widget_contract(self, label, widget_name, widget):
        """Check the widget carries the fields Zabbix 7.0 expects for its type."""
        contract = WIDGET_CONTRACTS.get(text_of(widget, "type"))
        if contract is None:
            return
        present = set((text_of(field, "type"), text_of(field, "name"))
                      for field in widget.findall("./fields/field"))
        names = set(name for _, name in present)

        for required in contract["required"]:
            if required not in present:
                self.error("%s: widget %s is missing the %s field %r"
                           % (label, widget_name, required[0], required[1]))
        if contract["reference"] and "reference" not in names:
            self.error("%s: widget %s has no reference field" % (label, widget_name))
        if not contract["reference"] and "reference" in names:
            self.error("%s: widget %s must not have a reference field"
                       % (label, widget_name))
        for legacy in LEGACY_FIELD_NAMES:
            if legacy in names:
                self.error("%s: widget %s uses the unindexed field name %r; Zabbix 7.0 "
                           "expects %r" % (label, widget_name, legacy, legacy + ".0"))

    def _check_widget_fields(self, label, widget_name, widget):
        self._check_widget_contract(label, widget_name, widget)
        for field in widget.findall("./fields/field"):
            field_type = text_of(field, "type")
            if field_type == "ITEM":
                key = text_of(field, "value/key")
                if key not in self.items:
                    self.error("%s: widget %s references unknown item %r"
                               % (label, widget_name, key))
            elif field_type == "GRAPH":
                name = text_of(field, "value/name")
                if name not in self.graphs:
                    self.error("%s: widget %s references unknown graph %r"
                               % (label, widget_name, name))
            elif field_type == "GRAPH_PROTOTYPE":
                name = text_of(field, "value/name")
                if name not in self.graph_prototypes:
                    self.error("%s: widget %s references unknown graph prototype %r"
                               % (label, widget_name, name))
            elif field_type == "INTEGER":
                value = text_of(field, "value")
                if not re.match(r"^-?\d+$", value):
                    self.error("%s: widget %s has a non integer field %r"
                               % (label, widget_name, text_of(field, "name")))

    def check_macros(self):
        used = set()
        for element in self.root.iter():
            if element.text:
                used.update("{$%s}" % name for name in USER_MACRO_RE.findall(element.text))
        for macro in sorted(used - self.macros):
            self.error("macro %s is used but has no default in the template" % macro)
        for macro in sorted(self.macros - used):
            self.warn("macro %s is defined but never used" % macro)

    def run(self):
        self.check_export()
        self.collect()
        self.check_items()
        self.check_preprocessing()
        self.check_paths()
        self.check_triggers()
        self.check_graphs()
        self.check_dashboards()
        self.check_macros()
        return self.errors, self.warnings

    def summary(self):
        return {
            "items": len(self.items),
            "item prototypes": len(self.prototypes),
            "discovery rules": len(self.rules),
            "triggers": len(self.root.findall("./triggers/trigger")),
            "trigger prototypes": len(self.root.findall(
                "./templates/template/discovery_rules/discovery_rule/"
                "trigger_prototypes/trigger_prototype")),
            "graphs": len(self.graphs),
            "graph prototypes": len(self.graph_prototypes),
            "value maps": len(self.valuemaps),
            "macros": len(self.macros),
            "dashboard widgets": len(self.root.findall(
                "./templates/template/dashboards/dashboard/pages/page/widgets/widget")),
        }


def collect(mode):
    output = subprocess.check_output(
        [sys.executable, COLLECTOR, "--mode", mode, "--slurm-bin-dir", FAKEBIN, "--no-cache"],
        universal_newlines=True)
    return json.loads(output)


def sample_document():
    """Collect a document from the recorded Slurm output in tests/fixtures.

    Accounting is collected through its own master item and merged in here, so
    that every JSONPath in the template can be resolved against one document.
    """
    document = collect("all")
    document["accounting"] = collect("accounting").get("accounting", {})
    return document


def validate(path=DEFAULT_TEMPLATE, document=None):
    validator = Validator(path, document if document is not None else sample_document())
    errors, warnings = validator.run()
    return validator, errors, warnings


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    path = argv[0] if argv else DEFAULT_TEMPLATE

    validator, errors, warnings = validate(path)

    for key, value in sorted(validator.summary().items()):
        print("%-20s %d" % (key, value))
    print("")

    for warning in warnings:
        print("WARNING: %s" % warning)
    for error in errors:
        print("ERROR:   %s" % error)

    if errors:
        print("\n%d error(s) found in %s" % (len(errors), path))
        return 1
    print("%s is valid" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
