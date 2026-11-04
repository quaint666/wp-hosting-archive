#!/usr/bin/env python3
"""Offline tests that the control app's VM (aopenpanel) is declared, addressed, firewalled, named and certificated the same way in every place that has to say so.

The same fact lives in several files, each a layer of its own: the VM in the AOpen's list and the inventory; ssh from the VM to dellwp in the hub's rules, the AOpen's forward rules, the Dell's forward rules and
dellwp's own input rules; the control node's HTTPS to the page in the AOpen's forward rules and the VM's own input rules; the name in the services registry; the certificate in the HP's renewal hook and the key
setup that lets the hook in; the monitoring in Prometheus. A layer that is missing makes the path fail without a message, or leaves it open wider than meant, so this reads every file as plain data (inventory,
group variables and host variables resolved with Jinja, the plays as YAML) and checks that they agree. It proves the files; it does not prove that a firewall accepted them, that the certificate reached the VM or
that anything answers: the plays and the rehearsal do. Run: python3 -I tests/test_wp_panel_network.py"""
import ipaddress
import re
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    return yaml.safe_load((ROOT / path).read_text())


def hosts_of(node, out=None):
    """Every host of the inventory with its variables, whatever the group nesting."""
    out = {} if out is None else out
    for name, host in (node.get("hosts") or {}).items():
        out.setdefault(name, {}).update(host or {})
    for child in (node.get("children") or {}).values():
        hosts_of(child or {}, out)
    for key, value in node.items():
        if isinstance(value, dict) and key not in ("hosts", "children", "vars"):
            hosts_of(value, out)
    return out


INVENTORY = load("inventory.yml")
HOSTS = hosts_of(INVENTORY)
CTX = {"hostvars": HOSTS}
for f in ("network", "ports", "wireguard", "domain", "proxmox", "services"):
    CTX.update(load("group_vars/all/%s.yml" % f))
CTX.update(load("group_vars/proxmox_guests/vars.yml"))
for host in ("aopen", "delloptiplex3040", "dellwp"):
    CTX.setdefault("hostvars_vars", {})[host] = load("host_vars/%s/vars.yml" % host)
HOSTS["aopen"].update(CTX["hostvars_vars"]["aopen"])
HOSTS["delloptiplex3040"].update(CTX["hostvars_vars"]["delloptiplex3040"])
HOSTS["dellwp"].update(CTX["hostvars_vars"]["dellwp"])
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined)


def resolve(value, ctx=None):
    ctx = dict(CTX, **(ctx or {}))
    if isinstance(value, str):
        for _ in range(6):
            if "{{" not in value:
                break
            value = ENV.from_string(value).render(**ctx)
        return value
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


HP_IP = resolve(HOSTS["hp705g4"]["overlay_ip"])
PANEL_IP = HOSTS["aopenpanel"]["overlay_ip"]
WP_IP = HOSTS["dellwp"]["overlay_ip"]
CONTROL_IP = resolve("{{ wg_control_node_overlay_ip }}")
PHONE_IP = resolve("{{ wg_phone_overlay_ip }}")
AOPEN = HOSTS["aopen"]
DELL = HOSTS["delloptiplex3040"]


def vm(name):
    return next(v for v in AOPEN["proxmox_vms"] if v["name"] == name)


class Declared(unittest.TestCase):
    def test_the_vm_is_declared_as_agreed(self):
        v = resolve(vm("aopenpanel"), {"proxmox_vm_bridges": AOPEN["proxmox_vm_bridges"]})
        self.assertEqual((v["vmid"], v["cores"], v["memory"], v["disk"]), (304, 2, 4096, 35))
        self.assertEqual(v["address"], "%s/29" % PANEL_IP)
        self.assertEqual(v["gateway"], "10.8.0.70")
        self.assertEqual(v["tags"], ["panel", "watch"], "the VM also runs the watcher")
        self.assertEqual(v["startup"].split(",")[0], "order=4", "after the drill (2)")

    def test_its_address_is_inside_the_bridge_subnet_unique_and_not_the_free_one(self):
        net = ipaddress.ip_network(resolve(AOPEN["proxmox_vm_bridges"][0]["subnet"]))
        block = ipaddress.ip_network(resolve(AOPEN["proxmox_vm_block"]))
        self.assertIn(ipaddress.ip_address(PANEL_IP), net)
        self.assertIn(ipaddress.ip_address(PANEL_IP), block)
        self.assertEqual(PANEL_IP, "10.8.0.68")
        overlay = [h["overlay_ip"] for h in HOSTS.values() if h.get("overlay_ip")]
        self.assertEqual(overlay.count(PANEL_IP), 1)
        self.assertNotIn("10.8.0.69", overlay, ".69 stays free")
        self.assertNotEqual(PANEL_IP, resolve(AOPEN["proxmox_vm_bridges"][0]["address"]).split("/")[0])

    def test_the_vm_id_is_unique_and_in_the_aopens_range(self):
        ids = [v["vmid"] for v in AOPEN["proxmox_vms"]] + [v["vmid"] for v in DELL["proxmox_vms"]]
        self.assertEqual(ids.count(304), 1)
        self.assertTrue(300 <= 304 < 380, "the AOpen's VMs are 300 to 379; 380 to 398 are the drill's spare range, 399 the template")
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_memory_declared_on_the_aopen_leaves_the_host_two_gib(self):
        """12288 MB of 15 GiB (15360): re-accepted 2026-11-04 when the production fallback (1536 MB, aopenfallback) was added to the 10752 MB left after the watcher moved into the control app's VM (4096 MB; the drill's spare VM is included). A VM added later must reopen this decision, not slip under it."""
        total = sum(v["memory"] for v in AOPEN["proxmox_vms"]) + AOPEN["proxmox_drill_memory_mb"]
        self.assertEqual(total, 12288)
        self.assertLessEqual(total, 15 * 1024 - 2048)

    def test_the_vm_is_in_the_group_the_base_plays_target(self):
        group = INVENTORY["aopen_vms"]["hosts"]
        self.assertIn("aopenpanel", group)
        self.assertEqual(group["aopenpanel"]["ansible_host"], "{{ overlay_ip }}")
        for play in ("vm_base", "ssh_hardening", "nftables", "node_exporter", "alloy", "ntfy_notify", "apt_pending_check", "disk_check", "nftables_check"):
            self.assertEqual(load("aopenvms/%s_aopenvms.yml" % play)[0]["hosts"], "aopen_vms", play)

    def test_docker_is_installed_and_pruned_on_it(self):
        for play in ("docker_setup", "docker_prune"):
            self.assertIn("aopenpanel", load("aopenvms/%s_aopenvms.yml" % play)[0]["hosts"].split(","), play)


class Ssh(unittest.TestCase):
    """The app's ssh to dellwp, in the four places that must agree."""

    def test_the_hub_lets_only_the_panel_vm_reach_dellwp_on_ssh_and_before_the_block(self):
        rules = load("opnsense/wireguard_hub_rules.yml")[0]["vars"]
        ctx = {k: v for k, v in rules.items() if k.startswith("hub_") and k != "hub_vm_rules"}
        ctx = {k: resolve(v, {"hostvars": HOSTS}) for k, v in ctx.items()}
        listed = [resolve(r, dict(ctx, hostvars=HOSTS)) for r in rules["hub_vm_rules"]]
        mine = [r for r in listed if r["src"] == PANEL_IP + "/32"]
        # 59 is the watcher on this VM probing the HP over https; 60 is the app's ssh to dellwp. Nothing else starts from this address.
        hp_ip = resolve(HOSTS["hp705g4"]["overlay_ip"])
        pings = [(r["seq"], r["dst"], r["proto"], r.get("port", "")) for r in mine if r["proto"] == "ICMP"]
        self.assertEqual([r[3] for r in pings], ["", "", ""], "Uptime Kuma's ping monitors: the Dell, the ASUS and centermc92, ICMP only")
        self.assertEqual(sorted(r[0] for r in pings), [61, 62, 69])
        self.assertEqual([(r["seq"], r["dst"], r["proto"], r["port"]) for r in mine if r["proto"] != "ICMP"], [(59, hp_ip + "/32", "TCP", "443"), (60, WP_IP + "/32", "TCP", "22"), (70, resolve(HOSTS["asus"]["overlay_ip"]) + "/32", "TCP", "22")])
        self.assertEqual(len({r["seq"] for r in listed}), len(listed), "sequence numbers are unique")
        block = next(r for r in listed if r.get("action") == "block" and r["src"] == resolve(AOPEN["proxmox_vm_block"]))
        self.assertLess(60, block["seq"])
        self.assertTrue(all(r["dst"] != WP_IP + "/32" or r["src"] == PANEL_IP + "/32" for r in listed if r["seq"] > 50), "nothing else of the AOpen reaches dellwp")

    def test_the_aopens_forward_rules_let_the_vm_bridge_start_ssh_to_dellwp(self):
        rules = resolve(AOPEN["proxmox_vm_to_overlay"]["vmbr1"])
        self.assertIn({"dest": WP_IP, "proto": "tcp", "ports": [22]}, rules)

    def test_the_dells_forward_rules_let_the_panel_vm_reach_dellwp_on_its_own_bridge_only(self):
        wp = next(v for v in DELL["proxmox_vms"] if v["name"] == "dellwp")
        self.assertEqual(wp["bridge"], "vmbr2")
        entries = [e for e in resolve(DELL["proxmox_overlay_to_vm"]) if e["src"] == PANEL_IP]
        self.assertEqual(entries, [{"src": PANEL_IP, "proto": "tcp", "ports": [22], "bridges": ["vmbr2"]}])

    def test_dellwp_itself_names_the_panel_vm_for_port_22(self):
        extra = resolve(load("host_vars/dellwp/vars.yml")["vm_nft_extra_tcp"])
        self.assertEqual([(e["src"], e["ports"]) for e in extra], [(PANEL_IP, [22])])
        self.assertTrue(extra[0]["comment"])
        template = (ROOT / "templates/nftables/_vm_input.j2").read_text()
        self.assertIn("vm_nft_extra_tcp", template)

    def test_the_vm_may_not_reach_dellwp_on_anything_but_22_at_any_layer(self):
        for rules in (resolve(AOPEN["proxmox_vm_to_overlay"]["vmbr1"]),):
            for r in rules:
                if r["dest"] == WP_IP:
                    self.assertEqual(r["ports"], [22])
        for e in resolve(DELL["proxmox_overlay_to_vm"]):
            if e["src"] == PANEL_IP:
                self.assertEqual(e["ports"], [22])


class Https(unittest.TestCase):
    """The page: reachable from the control node and from the phone on 443, and from nobody else."""

    def test_the_aopens_forward_rules_let_the_control_node_reach_443_on_the_vm_bridge(self):
        entries = [e for e in resolve(AOPEN["proxmox_overlay_to_vm"]) if e["src"] == CONTROL_IP and "vmbr1" in e["bridges"]]
        self.assertEqual(len(entries), 1)
        self.assertIn(443, entries[0]["ports"])
        self.assertIn(22, entries[0]["ports"])
        self.assertEqual(entries[0]["bridges"], ["vmbr1"])

    def test_the_aopens_forward_rules_let_the_phone_reach_443_and_nothing_else(self):
        entries = [e for e in resolve(AOPEN["proxmox_overlay_to_vm"]) if e["src"] == PHONE_IP]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ports"], [443])
        self.assertEqual(entries[0]["bridges"], ["vmbr1"])

    def test_no_wider_source_reaches_443_on_the_aopens_vms(self):
        for e in resolve(AOPEN["proxmox_overlay_to_vm"]):
            if 443 in e["ports"]:
                self.assertIn(e["src"], (CONTROL_IP, PHONE_IP, HP_IP), "only the control node, the phone and the HP (its heartbeat push to Uptime Kuma, which shares this listener)")
                self.assertEqual(e["bridges"], ["vmbr1"], "never on the fallback's bridge")

    def test_the_vm_lets_in_443_from_the_control_node_the_phone_and_the_hp_and_dns_from_the_overlay(self):
        vars_ = load("host_vars/aopenpanel/vars.yml")
        extra = resolve(vars_["vm_nft_extra_tcp"])
        self.assertEqual([(e["src"], e["ports"]) for e in extra], [(CONTROL_IP, [443]), (PHONE_IP, [443]), (HP_IP, [443]), ("10.8.0.0/24", [53])])
        self.assertEqual([(e["src"], e["ports"]) for e in resolve(vars_["vm_nft_extra_udp"])], [("10.8.0.0/24", [53])])
        self.assertEqual(CONTROL_IP, "10.8.0.8")
        self.assertEqual(PHONE_IP, "10.8.0.4")

    def test_the_inside_layer_is_the_same_as_the_group_default_for_ssh_and_scraping(self):
        guests = load("group_vars/proxmox_guests/vars.yml")
        self.assertIn("wg_control_node_overlay_ip", guests["vm_nft_ssh_sources"][1])


class Name(unittest.TestCase):
    def test_the_name_is_in_the_registry_and_points_at_the_vm(self):
        s = resolve(load("group_vars/all/services.yml")["services"], {"hostvars": HOSTS})
        self.assertEqual(s["wp_panel"], {"host_ip": PANEL_IP, "lab_domain": "wp-panel.lab.example.com"})
        domains = [v["lab_domain"] for v in s.values()]
        self.assertEqual(len(domains), len(set(domains)), "every name once")
        self.assertTrue(s["wp_panel"]["lab_domain"].endswith("." + resolve("{{ lab_base_domain }}")))

    def test_the_name_has_no_lan_address_because_the_vm_is_only_on_the_overlay(self):
        raw = load("group_vars/all/services.yml")["services"]["wp_panel"]
        self.assertNotIn("lan_ip", raw)


class Certificate(unittest.TestCase):
    def test_the_renewal_hook_pushes_to_the_vm(self):
        play = load("hp705g4/certbot_timer_setup_hp705g4.yml")[0]
        hosts = resolve(play["vars"]["cert_remote_hosts"], {"hostvars": HOSTS})
        self.assertIn(PANEL_IP, hosts)
        self.assertEqual(len(hosts), len(set(hosts)))

    def test_the_hooks_key_is_authorized_on_the_vm_and_the_vms_host_key_is_trusted_by_the_hp(self):
        plays = load("hp705g4/cert_orchestration_key_setup_hp705g4.yml")
        auth = next(p for p in plays if p["hosts"] == "aopenpanel")
        modules = [k for t in auth["tasks"] for k in t if k.startswith("ansible.")]
        self.assertIn("ansible.posix.authorized_key", modules)
        self.assertEqual(next(t for t in auth["tasks"] if "ansible.posix.authorized_key" in t)["ansible.posix.authorized_key"]["user"], "admin")
        trust = next(p for p in plays if p["hosts"] == "hp705g4" and p["become"] is True and "known_hosts" in str(p))
        task = next(t for t in trust["tasks"] if "aopenpanel" in str(t))["ansible.builtin.lineinfile"]
        self.assertEqual((task["path"], task["mode"], task["create"]), ("/root/.ssh/known_hosts", "0600", True), "root's known_hosts: the renewal hook runs as root")
        self.assertIn("panel_hostkey_slurp", task["line"])
        self.assertIn("hostvars['aopenpanel']['overlay_ip']", task["line"])
        self.assertLess(plays.index(auth), plays.index(trust), "the host key is read before it is trusted")

    def test_the_first_deploy_play_puts_the_files_where_the_hook_and_nginx_expect_them_without_leaking_the_key(self):
        play = load("aopenvms/deploy_le_cert_aopenpanel.yml")[0]
        self.assertEqual((play["hosts"], play["become"]), ("aopenpanel", True))
        self.assertNotIn("handlers", play)
        self.assertFalse([t for t in play["tasks"] if "notify" in t], "nginx is not on the VM yet: nothing to reload")
        by = {t["name"]: t for t in play["tasks"]}
        key_read, key_put = by["Read privkey.pem from the HP"], by["Deploy privkey.pem"]
        self.assertTrue(key_read["no_log"] and key_put["no_log"] and key_put["diff"] is False)
        self.assertEqual(key_put["ansible.builtin.copy"]["mode"], "0640")
        self.assertEqual(by["Deploy fullchain.pem"]["ansible.builtin.copy"]["mode"], "0644")
        for t in (key_put, by["Deploy fullchain.pem"], by["Ensure the certificate directory exists"]):
            self.assertEqual(list(t.values())[1]["owner"], "admin", "the hook rsyncs as admin")
        self.assertEqual(key_read["delegate_to"], "hp705g4")
        self.assertEqual(play["vars"]["le_local_dir"], "{{ le_cert_dir }}")
        self.assertEqual(resolve("{{ le_cert_dir }}"), "/etc/ssl/le/lab.example.com")
        hook = (ROOT / "hp705g4/certbot_timer_setup_hp705g4.yml").read_text()
        self.assertIn('DEST_DIR="/etc/ssl/le/{{ le_cert_name }}"', hook, "the hook's destination is the directory this play makes")


class Monitoring(unittest.TestCase):
    def test_prometheus_watches_the_vm(self):
        text = (ROOT / "hp705g4/prometheus_hp705g4.yml").read_text()
        self.assertRegex(text, r"prometheus_vm_hosts: \[[^\]]*aopenpanel[^\]]*\]")
        self.assertIn("hostvars['aopenpanel']['overlay_ip']", text)
        self.assertEqual(len(re.findall(r"host: aopenpanel\b", text)), 1)


class Fallback(unittest.TestCase):
    """The production fallback VM (aopenfallback): its own bridge, and the same four destinations at the forward rules and at the hub, nothing else, and no way in on 443 or 53."""

    def test_the_vm_is_declared_on_its_own_bridge_inside_the_block(self):
        v = resolve(vm("aopenfallback"), {"proxmox_vm_bridges": AOPEN["proxmox_vm_bridges"]})
        self.assertEqual((v["vmid"], v["cores"], v["memory"], v["disk"], v["bridge"]), (305, 2, 1536, 20, "vmbr2"))
        self.assertEqual((v["address"], v["gateway"]), ("10.8.0.73/29", "10.8.0.78"))
        bridge = next(b for b in AOPEN["proxmox_vm_bridges"] if b["name"] == "vmbr2")
        self.assertEqual(resolve(bridge["subnet"]), "10.8.0.72/29")
        self.assertTrue(ipaddress.ip_network(resolve(bridge["subnet"])).subnet_of(ipaddress.ip_network(resolve(AOPEN["proxmox_vm_block"]))))
        self.assertFalse(ipaddress.ip_network("10.8.0.72/29").overlaps(ipaddress.ip_network("10.8.0.64/29")))

    def test_the_forward_rules_and_the_hub_name_the_same_destinations(self):
        fwd = resolve(AOPEN["proxmox_vm_to_overlay"]["vmbr2"])
        fwd_set = {(d, e["proto"].upper(), str(p)) for e in fwd for d in [e["dest"]] for p in e["ports"]}
        rules = load("opnsense/wireguard_hub_rules.yml")[0]["vars"]
        ctx = {k: resolve(v, {"hostvars": HOSTS}) for k, v in rules.items() if k.startswith("hub_") and k != "hub_vm_rules"}
        listed = [resolve(r, dict(ctx, hostvars=HOSTS)) for r in rules["hub_vm_rules"]]
        mine = [r for r in listed if r["src"] == "10.8.0.72/29"]
        hub_set = {(r["dst"].split("/")[0], r["proto"], r["port"]) for r in mine}
        self.assertEqual(fwd_set, hub_set)
        self.assertEqual(sorted(r["seq"] for r in mine), [65, 66, 67, 68])
        self.assertEqual(len({r["seq"] for r in listed}), len(listed))

    def test_nothing_from_the_overlay_reaches_the_fallback_except_ssh_and_the_scrape(self):
        for e in resolve(AOPEN["proxmox_overlay_to_vm"]):
            if "vmbr2" in e["bridges"]:
                self.assertIn(e["src"], (HP_IP, CONTROL_IP))
                self.assertEqual({str(x) for x in e["ports"]} - {"22", "9100"}, set(), "only ssh and node_exporter")

    def test_prometheus_watches_it(self):
        text = (ROOT / "hp705g4/prometheus_hp705g4.yml").read_text()
        self.assertRegex(text, r"prometheus_vm_hosts: \[[^\]]*aopenfallback[^\]]*\]")
        self.assertEqual(len(re.findall(r"host: aopenfallback\b", text)), 1)


class PrivateBackups(unittest.TestCase):
    """The backups of ntfy, Uptime Kuma and AdGuard (aopenvms/backup_private_ship_aopenvms.yml): the services, the destinations, the firewalls and the watchers must name the same things."""

    @classmethod
    def setUpClass(cls):
        cls.panel = load("host_vars/aopenpanel/vars.yml")
        cls.pbs = load("host_vars/aopenpbs/vars.yml")
        cls.drill = load("host_vars/aopendrill/vars.yml")
        cls.backup = load("group_vars/all/backup.yml")["backup_dirs"]
        cls.ship = load("aopenvms/backup_private_ship_aopenvms.yml")

    def dirs(self):
        return [b["dir"] for b in self.panel["private_backups"]]

    def test_each_service_is_one_of_the_shared_backup_directories_and_has_its_own_key_name(self):
        names = [b["name"] for b in self.panel["private_backups"]]
        self.assertEqual(sorted(names), ["adguard", "kuma", "ntfy"])
        self.assertEqual(len(set(names)), 3)
        for d in self.dirs():
            self.assertRegex(d, r"^\{\{ backup_dirs\.[a-z_]+ \}\}$")
            self.assertIn(d.split("backup_dirs.")[1].split(" ")[0], self.backup)

    def test_the_destinations_are_one_at_the_apartment_and_one_at_home_and_the_ship_play_authorizes_exactly_them(self):
        self.assertEqual(self.panel["private_backup_destinations"], ["aopenpbs", "asus"])
        authorize = next(p for p in self.ship if p["name"].startswith("Authorize"))
        self.assertEqual(sorted(authorize["hosts"].split(",")), sorted(self.panel["private_backup_destinations"]))
        self.assertEqual(HOSTS["asus"].get("wg_site", "home"), "home", "the ASUS is the home copy")

    def test_a_key_can_only_run_rrsync_on_its_own_directory_from_the_vm(self):
        authorize = next(p for p in self.ship if p["name"].startswith("Authorize"))
        task = next(t for t in authorize["tasks"] if "ansible.posix.authorized_key" in t)
        options = task["ansible.posix.authorized_key"]["key_options"]
        for needle in ("restrict", 'from="{{ hostvars[\'aopenpanel\'][\'overlay_ip\'] }}"', 'command="/usr/bin/rrsync {{ item.dir }}"'):
            self.assertIn(needle, options)

    def test_aopenpbs_lets_in_ssh_from_the_vm_and_the_drill_and_from_nobody_else_new(self):
        extra = resolve(self.pbs["vm_nft_extra_tcp"], {"hostvars": HOSTS})
        ssh = sorted(e["src"] for e in extra if 22 in e["ports"])
        self.assertEqual(ssh, sorted([resolve(HOSTS["aopenpanel"]["overlay_ip"]), resolve(HOSTS["aopendrill"]["overlay_ip"])]))

    def test_the_age_checks_watch_the_same_three_services_where_they_are_made_and_where_they_land(self):
        for host in (self.panel, self.pbs):
            dirs = sorted(c["path"] for c in host["backup_age_checks"])
            self.assertEqual(dirs, sorted(self.dirs()))
            for c in host["backup_age_checks"]:
                self.assertEqual((c["kind"], c["max_hours"]), ("newest_file", 36))
        self.assertEqual(self.pbs["backup_age_requires_mount"], "{{ pbs_check_mount_unit }}")

    def test_the_drill_reads_the_two_copies_and_nothing_of_centermc92_for_them(self):
        by = {}
        for s in self.drill["drill_sources"]:
            by.setdefault(s["service"], []).append(s["host"])
        for service in ("ntfy", "kuma", "adguard"):
            self.assertEqual(sorted(by[service]), ["aopenpbs", "asus"], service)
        expect = resolve(self.drill["drill_services"]["kuma"]["expect"], {"hostvars": {"aopenpanel": self.panel}})
        self.assertIn(self.panel["kuma_data_dir"].lstrip("/") + "/", expect, "the drill looks for the data directory where the backup script puts it in the tarball")
        self.assertIn("kuma_data_dir[1:]", (ROOT / "aopenvms/backup_kuma_to_local.yml").read_text())
        keys = load("aopenvms/service_drill_keys_aopenvms.yml")
        sources_play = next(p for p in keys if p["name"].startswith("Authorize the drill"))
        self.assertIn("aopenpbs", sources_play["hosts"].split(","))


class RuleComments(unittest.TestCase):
    def test_no_firewall_comment_is_longer_than_nftables_allows(self):
        """nft refuses a rule whose comment is over 128 characters ("comment too long"; seen on aopenpbs 2026-11-04), after the play has already rendered it: the VMs' extra rules and the hosts' forward rules."""
        checked = 0
        for path in sorted((ROOT / "host_vars").glob("*/vars.yml")):
            data = yaml.safe_load(path.read_text()) or {}
            for key in ("vm_nft_extra_tcp", "vm_nft_extra_udp"):
                for rule in data.get(key, []):
                    checked += 1
                    self.assertLessEqual(len(resolve(rule["comment"], {"hostvars": HOSTS})), 128, "%s %s: %s" % (path, key, rule["comment"]))
        self.assertGreater(checked, 5)


if __name__ == "__main__":
    unittest.main()
