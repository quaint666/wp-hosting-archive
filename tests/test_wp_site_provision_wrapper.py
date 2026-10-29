#!/usr/bin/env python3
"""Offline tests of templates/wp/wp_site_provision.py.j2, the wrapper that carries the repo's values to wp-site-provision, and of the variables it reads.

The wrapper is rendered with the real variables (vars/wp.yml, host_vars/dellwp/vars.yml, the VM's declared size in host_vars/delloptiplex3040/vars.yml) and its CONFIG is evaluated, then compared
with what the repo says: the defaults, the sites still declared, the fit limits, the paths and the platform values. `wp_provision_platform_vars` must be exactly the values the templates and the tool
read (scripts/wp_site_renderer.py needed_platform_vars), and the master secret and the provider key must be nowhere in the file. What this does not prove: Ansible's own rendering (its `lookup('vars', ...)`
and `tojson` are modelled here by Jinja's), or that python3 runs the installed file: scripts/wp_provision_rehearsal_dellwp.sh does that on the host. Run: python3 -I tests/test_wp_site_provision_wrapper.py"""
import ast
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import jinja2  # noqa: E402
import wp_site_provision  # noqa: E402
import wp_site_renderer as renderer  # noqa: E402
import test_wp_sites_render as play  # noqa: E402

ROOT = HERE.parent
SECRETS = ("stub-master-secret", "stub-resend-key")


def render():
    v = play.base_vars()
    v["wp_master_secret"], v["dellwp_wp_resend_key"] = SECRETS
    v["wp_sites"] = v["real_wp_sites"]                                    # the helper swaps in a fixture site; the wrapper must carry the real list
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROOT / "templates")), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.globals["lookup"] = lambda kind, name: v[name]
    ctx = dict(v, inventory_hostname="dellwp", hostvars={"delloptiplex3040": {"proxmox_vms": [play.dellwp_vm()]}})
    return v, e.get_template("wp/wp_site_provision.py.j2").render(**ctx)


def config(text):
    """CONFIG as the installed file would build it: everything but the last line (the call of main)."""
    body = "\n".join(l for l in text.splitlines() if not l.startswith("sys.exit("))
    ns = {"__name__": "wrapper"}
    sys.path.insert(0, str(ROOT / "scripts"))
    exec(compile(body, "wp-site-provision", "exec"), ns)
    return ns["CONFIG"]


class Wrapper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v, cls.text = render()
        cls.cfg = config(cls.text)

    def test_it_is_python_and_ends_by_calling_main(self):
        ast.parse(self.text)
        self.assertTrue(self.text.startswith("#!/usr/bin/python3 -I\n"))
        self.assertEqual(self.text.rstrip().splitlines()[-1], "sys.exit(wp_site_provision.main(CONFIG, sys.argv[1:]))")

    def test_the_values_are_the_repos(self):
        c, v = self.cfg, self.v
        self.assertEqual(c["defaults"], v["wp_site_defaults"], "booleans, numbers and lists survive")
        self.assertEqual(c["known"], v["real_wp_sites"])
        self.assertEqual(c["fit"], dict(reserved_names=v["wp_reserved_names"], vm_memory_mb=play.dellwp_vm()["memory"], data_disk_gb=play.dellwp_vm()["data_disk"], ram_reserve_mb=v["wp_vm_ram_reserve_mb"],
                                         data_reserve_mb=v["wp_data_reserve_mb"], backup_quota_offset=v["wp_backup_quota_offset"], scratch_quota_id=v["wp_scratch_quota_id"],
                                         archive_quota_id=v["wp_archive_quota_id"], intake_quota_id=v["wp_intake_quota_id"]))
        self.assertIn("vps-hub", c["fit"]["reserved_names"])
        self.assertEqual((c["data_dir"], c["tombstones_dir"]), (v["wp_data_mount"] + "/sites", v["wp_data_mount"] + "/tombstones"))
        self.assertEqual(c["template_dir"], v["wp_tools_lib"] + "/templates")
        self.assertEqual((c["master_secret_file"], c["resend_key_file"]), (v["wp_provision_secret_dir"] + "/master_secret", v["wp_provision_secret_dir"] + "/resend_key"))
        self.assertEqual((c["lock_dir"], c["command_timeout_s"], c["probe_tries"], c["probe_delay_s"]), (v["wp_provision_lock_dir"], v["wp_provision_command_timeout_s"], v["wp_provision_probe_tries"], v["wp_provision_probe_delay_s"]))
        self.assertEqual(c["platform"], {k: v[k] for k in v["wp_provision_platform_vars"]})
        self.assertEqual(set(c["tools"]), {"docker", "nginx", "systemctl", "lsattr", "chattr", "repquota", "setquota"})
        for path in c["tools"].values():
            self.assertTrue(path.startswith("/usr/"), path)

    def test_the_platform_list_is_exactly_what_the_code_reads(self):
        required, optional = renderer.needed_platform_vars(ROOT / "templates")
        self.assertEqual(sorted(self.v["wp_provision_platform_vars"]), required)
        self.assertEqual(len(set(self.v["wp_provision_platform_vars"])), len(self.v["wp_provision_platform_vars"]))
        self.assertEqual(optional, ["dellwp_wp_resend_key"])

    def test_no_secret_is_in_the_file(self):
        for secret in SECRETS:
            self.assertNotIn(secret, self.text)
        self.assertNotIn("wp_master_secret", self.cfg["platform"])
        self.assertTrue(all(not k.startswith("dellwp_") for k in self.cfg["platform"]), "vault variables are never carried")

    def test_the_tool_starts_with_it_and_can_plan_a_site(self):
        prov = wp_site_provision.Provisioner(self.cfg)
        self.assertEqual({k["name"] for k in prov.known}, {s["name"] for s in self.v["real_wp_sites"]})
        self.assertTrue(all(k["state"] in ("hold", "live", "stopped", "closed") for k in prov.known), "an entry without a state gets the default")
        for n in ("wp_site_defaults",):
            self.assertIn(n, self.v)

    def test_the_numbers_of_a_new_site_are_above_every_site_the_repo_declares(self):
        import wp_site_model as model
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            cfg = dict(self.cfg, data_dir=t + "/sites", tombstones_dir=t + "/tomb")
            prov = wp_site_provision.Provisioner(cfg)
            port, quota = model.next_numbers(model.everything(prov.registry, prov.known))
        self.assertEqual((port, quota), (max(s["port"] for s in self.v["real_wp_sites"]) + 1, max(s["quota_id"] for s in self.v["real_wp_sites"]) + 1))

    def test_the_repo_variables_the_wrapper_needs_exist(self):
        for name in ("wp_provision_secret_dir", "wp_provision_lock_dir", "wp_provision_command_timeout_s", "wp_provision_probe_tries", "wp_provision_probe_delay_s", "wp_provision_platform_vars",
                     "wp_data_reserve_mb", "wp_vm_ram_reserve_mb", "wp_scratch_quota_id", "wp_archive_quota_id", "wp_intake_quota_id"):
            self.assertIn(name, self.v, name)


if __name__ == "__main__":
    unittest.main()
