#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the deployment files.

These are static checks on install.sh and the shipped agent configuration.
They exist because the packaging mistakes they guard against fail silently:
UserParameters in a directory the agent does not include load nowhere, and
every item comes back as "Unsupported item key" with nothing in the logs.
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

AGENT2_INCLUDE_DIR = "/etc/zabbix/zabbix_agent2.d/plugins.d"
AGENTD_INCLUDE_DIR = "/etc/zabbix/zabbix_agentd.d"

MASTER_KEYS = ("slurm.cluster", "slurm.nodes")


def read(*parts):
    with open(os.path.join(ROOT, *parts)) as handle:
        return handle.read()


class AgentConfigTest(unittest.TestCase):
    def setUp(self):
        self.conf = read("agent", "slurm.conf")

    def test_documents_the_agent2_plugins_directory(self):
        self.assertIn(AGENT2_INCLUDE_DIR + "/slurm.conf", self.conf)

    def test_documents_the_agent1_directory(self):
        self.assertIn(AGENTD_INCLUDE_DIR + "/slurm.conf", self.conf)

    def test_defines_both_master_item_keys(self):
        defined = re.findall(r"^UserParameter=([^,]+),", self.conf, re.MULTILINE)
        self.assertEqual(sorted(defined), sorted(MASTER_KEYS))

    def test_documents_the_optional_accounting_key(self):
        """Accounting ships commented out: it is opt-in on both sides."""
        self.assertIn("# UserParameter=slurm.accounting,", self.conf)
        self.assertNotIn("\nUserParameter=slurm.accounting,", self.conf)

    def test_active_user_parameters_share_one_cache(self):
        lines = [line for line in self.conf.splitlines()
                 if line.startswith("UserParameter=")]
        caches = set(re.search(r"--cache-file (\S+)", line).group(1) for line in lines)
        self.assertEqual(len(caches), 1, "both keys must read the same cache file")

    def test_every_user_parameter_selects_a_mode(self):
        for line in self.conf.splitlines():
            if line.startswith("UserParameter=") or line.startswith("# UserParameter="):
                self.assertRegex(line, r"--mode (cluster|nodes|accounting)\b")


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.script = read("install.sh")

    def test_installs_agent2_config_into_plugins_d(self):
        self.assertIn("AGENT_DIR=%s" % AGENT2_INCLUDE_DIR, self.script)

    def test_never_targets_the_bare_agent2_directory(self):
        """zabbix_agent2.d/*.conf is not included by the packaged agent 2."""
        targets = re.findall(r"AGENT_DIR=(\S+)", self.script)
        self.assertNotIn("/etc/zabbix/zabbix_agent2.d", targets)
        for candidate in re.findall(r"for candidate in (.+?); do", self.script, re.DOTALL):
            self.assertNotIn("zabbix_agent2.d", candidate)

    def test_warns_when_the_agent_does_not_include_the_directory(self):
        self.assertIn("has no Include covering", self.script)

    def test_creates_the_directory_when_missing(self):
        self.assertIn('mkdir -p "$AGENT_DIR"', self.script)

    def test_writes_both_master_item_keys_in_both_modes(self):
        written = re.findall(r"^UserParameter=([^,]+),", self.script, re.MULTILINE)
        # Both keys for the direct mode, both again for --timer, and the
        # accounting key once behind --accounting.
        self.assertEqual(sorted(written),
                         sorted(list(MASTER_KEYS) * 2 + ["slurm.accounting"]))
        self.assertIn("--cache-only", self.script)
        self.assertIn("--cache-ttl", self.script)

    def test_accounting_is_guarded_by_its_flag(self):
        self.assertIn("--accounting) USE_ACCOUNTING=1", self.script)
        # The accounting UserParameter is only written inside the guard.
        guard = re.search(r'if \[ "\$USE_ACCOUNTING" -eq 1 \]; then(.+?)\nfi\n',
                          self.script, re.DOTALL)
        self.assertIsNotNone(guard, "accounting block not found")
        self.assertIn("UserParameter=slurm.accounting", guard.group(1))


class DocumentationTest(unittest.TestCase):
    def test_readme_documents_the_agent2_path(self):
        self.assertIn(AGENT2_INCLUDE_DIR, read("README.md"))

    def test_systemd_unit_matches_the_installed_collector_path(self):
        unit = read("systemd", "zabbix-slurm-collector.service")
        self.assertIn("/usr/local/bin/slurm_zabbix.py", unit)
        self.assertIn("--refresh", unit)
        # The unit, the installer and the shipped UserParameters must agree on
        # the cache file, or the timer refreshes a cache nobody reads.
        cache = re.search(r"--cache-file (\S+)", unit).group(1)
        self.assertIn(cache, read("agent", "slurm.conf"))
        # install.sh composes the path from CACHE_DIR, so compare the parts.
        script = read("install.sh")
        self.assertIn("CACHE_DIR=%s" % os.path.dirname(cache), script)
        self.assertIn('CACHE_FILE="$CACHE_DIR/%s"' % os.path.basename(cache), script)


if __name__ == "__main__":
    unittest.main()
