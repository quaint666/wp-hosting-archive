#!/usr/bin/env python3
"""Offline tests of the control app's play (aopenvms/wp_panel_aopenvms.yml) and its two templates (templates/wp_panel/).

The templates are rendered with the real variables (host_vars/aopenpanel/vars.yml, the inventory, the services registry) and checked against what the rest of the repo says about the same thing: the unit's flags
against the flags scripts/wp_panel_image_rehearsal.sh proved, its environment against what app/wp_panel/config.py reads, its uid and port against app/Dockerfile; the vhost against the certificate files the first-deploy
play writes and the address the VM firewall opens. The play is read as YAML: its early stops, the secrets written as root-managed files for the app's uid, the pinned ssh host key, the build that happens only when the image
is missing, the order of the check, the start and the reload. The secrets contract is run for real: a credentials file made by scripts/wp_panel_credentials.py is written the way the play writes it and read by the app's
own settings loader and by ssh-keygen. They do not prove Docker, nginx, systemd or Ansible themselves: scripts/wp_panel_rehearsal_aopenpanel.sh does, on the VM. Run: python3 -I tests/test_wp_panel_play.py"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "app"))
import test_wp_panel_network as net  # noqa: E402
import wp_panel_credentials as cred  # noqa: E402
from wp_panel import config  # noqa: E402

PLAY_PATH = ROOT / "aopenvms/wp_panel_aopenvms.yml"
PLAY = yaml.safe_load(PLAY_PATH.read_text())[0]
TASKS = PLAY["tasks"]
BY = {t["name"]: t for t in TASKS}
net.HOSTS["dellwp"].update(yaml.safe_load((ROOT / "host_vars/dellwp/vars.yml").read_text()))
PANEL_VARS = yaml.safe_load((ROOT / "host_vars/aopenpanel/vars.yml").read_text())
for _ in range(4):                      # variables refer to each other (panel_build_dir to panel_dir): resolve until they settle
    PANEL_VARS = {k: net.resolve(v, dict(PANEL_VARS, hostvars=net.HOSTS)) for k, v in PANEL_VARS.items()}
CONTEXT = dict(net.CTX, **PANEL_VARS, overlay_ip=net.HOSTS["aopenpanel"]["overlay_ip"], panel_image="wp-panel:0123456789ab", hostvars=net.HOSTS)
CONTEXT["le_cert_dir"] = net.resolve("{{ le_cert_dir }}")
DOCKERFILE = (ROOT / "app/Dockerfile").read_text()
STRICT = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)


def render(name):
    return STRICT.from_string((ROOT / "templates/wp_panel" / name).read_text()).render(**CONTEXT)


def task(part):
    return next(t for t in TASKS if part in t["name"])


class Variables(unittest.TestCase):
    def test_the_variables_are_the_documented_ones_and_hold_no_secret(self):
        raw = yaml.safe_load((ROOT / "host_vars/aopenpanel/vars.yml").read_text())
        for key in ("panel_dir", "panel_build_dir", "panel_secret_dir", "panel_uid", "panel_container", "panel_memory", "panel_bind", "panel_session_hours", "panel_domain", "panel_ctl_host", "panel_ctl_user", "panel_site_domain", "panel_upload_max_mb"):
            self.assertIn(key, raw)
        self.assertFalse([k for k in raw if k.startswith("aopenpanel_panel_")], "the secrets are in the vault, never in vars.yml")
        self.assertEqual(PANEL_VARS["panel_ctl_host"], net.HOSTS["dellwp"]["overlay_ip"])
        self.assertEqual(PANEL_VARS["panel_ctl_user"], "wpctl")
        self.assertEqual(PANEL_VARS["panel_domain"], "wp-panel.lab.example.com")

    def test_the_uid_and_the_port_are_the_images(self):
        self.assertEqual(PANEL_VARS["panel_uid"], int(re.search(r"^USER (\d+):\d+$", DOCKERFILE, re.M).group(1)))
        self.assertEqual(PANEL_VARS["panel_uid"], int(re.search(r"--uid (\d+)", DOCKERFILE).group(1)))
        host, port = PANEL_VARS["panel_bind"].rsplit(":", 1)
        self.assertEqual(host, "127.0.0.1", "loopback only: nginx is the only way in")
        self.assertIn("http://%s:%s/login" % (host, port), DOCKERFILE, "the health check asks the address the app is told to bind")

    def test_the_memory_is_a_size_docker_reads_and_the_hours_are_in_the_apps_range(self):
        self.assertRegex(PANEL_VARS["panel_memory"], r"^[1-9][0-9]*m$")
        self.assertTrue(1 <= int(PANEL_VARS["panel_session_hours"]) <= 24)


class Unit(unittest.TestCase):
    def setUp(self):
        self.text = render("panel.service.j2")
        self.exec = " ".join(re.search(r"^ExecStart=(.*?)^ExecStop", self.text, re.S | re.M).group(1).replace("\\\n", " ").split())

    def test_it_runs_the_image_the_play_built_with_the_flags_the_rehearsal_proved(self):
        rehearsal = re.search(r'sudo -n docker run (.*?) \$TAG sh -s', (ROOT / "scripts/wp_panel_image_rehearsal.sh").read_text()).group(1)
        for flag in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--tmpfs /tmp", "--tmpfs /dev/shm", "--tmpfs /run/panel:uid=10001,gid=10001,mode=700"):
            self.assertIn(flag, rehearsal, "the flag the unit must also have")
        norm = self.exec.replace("--cap-drop=ALL", "--cap-drop ALL").replace("--security-opt=no-new-privileges", "--security-opt no-new-privileges")
        for need in ("--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--tmpfs /tmp", "--tmpfs /dev/shm", "--tmpfs /run/panel:uid=10001,gid=10001,mode=700"):
            self.assertIn(need, norm)
        self.assertIn("--memory=512m --memory-swap=512m", self.exec, "memory equals swap, the repo's rule")
        self.assertTrue(self.exec.endswith("wp-panel:0123456789ab"), "the image is the last word: the tag the play built")

    def test_it_mounts_only_the_secret_directory_read_only_and_opens_nothing_else(self):
        self.assertEqual(re.findall(r"-v (\S+)", self.exec), ["/etc/wp-panel:/run/secrets:ro"])
        for bad in (" -p ", "--publish", "--privileged", "--cap-add", "docker.sock", "--pid", "--ipc", "--device", "--user root", "-u 0", "--volume", "--mount"):
            self.assertNotIn(bad, " %s " % self.exec, bad)
        self.assertIn("--net=host", self.exec, "like the watcher's containers; the app binds loopback, nginx is the only way in")

    def test_the_environment_is_what_the_app_reads_and_nothing_else(self):
        names = dict(re.findall(r"-e (PANEL_[A-Z_]+)=(\S+)", self.exec))
        read = set(re.findall(r'"(PANEL_[A-Z_]+)"', (ROOT / "app/wp_panel/config.py").read_text())) | {"PANEL_BIND"}
        self.assertLessEqual(set(names), read, "an environment variable the app never reads")
        self.assertEqual(names["PANEL_CTL_HOST"], net.HOSTS["dellwp"]["overlay_ip"])
        self.assertEqual(names["PANEL_CTL_USER"], "wpctl")
        self.assertEqual(names["PANEL_SITE_DOMAIN"], net.HOSTS["dellwp"]["wp_domain"], "the sites' zone is dellwp's own variable, not a copy")
        self.assertEqual(names["PANEL_SECRET_DIR"], "/run/secrets")
        self.assertEqual(names["PANEL_STATE_DIR"], "/run/panel")
        self.assertEqual(names["PANEL_BIND"], PANEL_VARS["panel_bind"])
        self.assertEqual(names["PANEL_SESSION_HOURS"], "3")
        self.assertEqual(len(re.findall(r"-e ", self.exec)), len(names))

    def test_it_restarts_and_removes_a_stale_container_and_stops_it_cleanly(self):
        self.assertIn("ExecStartPre=-/usr/bin/docker rm -fv wp-panel", self.text)
        self.assertIn("ExecStop=/usr/bin/docker stop -t 20 wp-panel", self.text)
        self.assertIn("Restart=always", self.text)
        self.assertIn("Requires=docker.service", self.text)
        self.assertNotIn("docker restart", "\n".join(l for l in self.text.splitlines() if not l.lstrip().startswith("#")), "the comment says never to; the unit must not")

    def test_no_secret_value_reaches_the_unit(self):
        for word in ("PASSWORD", "SECRET=", "TOKEN", "KEY=", "BEGIN"):
            self.assertNotIn(word, self.text.replace("PANEL_SECRET_DIR", ""), word)


class Vhost(unittest.TestCase):
    def setUp(self):
        self.text = render("panel_nginx.conf.j2")
        self.code = "\n".join(l for l in self.text.splitlines() if not l.lstrip().startswith("#"))

    def test_it_listens_on_the_overlay_address_alone_on_443_and_refuses_every_other_name(self):
        ip = net.HOSTS["aopenpanel"]["overlay_ip"]
        listens = re.findall(r"^\s*listen (\S+)(.*);", self.code, re.M)
        self.assertEqual(sorted(a for a, _ in listens), [ip + ":443", ip + ":443"])
        self.assertNotRegex(self.code, r"listen\s+(\[::\]|0\.0\.0\.0|\*|80|443;)")
        self.assertIn("default_server;\n    ssl_reject_handshake on;", self.code)
        self.assertEqual(re.findall(r"server_name (\S+);", self.code), ["wp-panel.lab.example.com"])

    def test_it_uses_the_certificate_files_the_first_deploy_play_writes(self):
        base = net.resolve("{{ le_cert_dir }}")
        self.assertIn("ssl_certificate %s/fullchain.pem;" % base, self.code)
        self.assertIn("ssl_certificate_key %s/privkey.pem;" % base, self.code)
        deploy = yaml.safe_load((ROOT / "aopenvms/deploy_le_cert_aopenpanel.yml").read_text())[0]
        self.assertEqual(deploy["vars"]["le_local_dir"], "{{ le_cert_dir }}")
        names = {t["ansible.builtin.copy"]["dest"] for t in deploy["tasks"] if "ansible.builtin.copy" in t}
        self.assertEqual(names, {"{{ le_local_dir }}/fullchain.pem", "{{ le_local_dir }}/privkey.pem"})

    def test_tls_is_modern_and_the_response_says_so(self):
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", self.code)
        self.assertNotRegex(self.code, r"TLSv1(\.0|\.1)?[ ;]|SSLv")
        self.assertIn("ssl_session_tickets off;", self.code)
        self.assertIn('add_header Strict-Transport-Security "max-age=31536000" always;', self.code)
        # inside both server blocks, never at the top level: the distribution's nginx.conf may set it in http{} and a second one there is "directive is duplicate" (seen on the VM, 2026-10-29)
        self.assertEqual(self.code.count("    server_tokens off;\n"), 2)
        self.assertNotRegex(self.code, r"(?m)^server_tokens")

    def locations(self):
        """location header -> its block, in file order (the blocks hold no braces inside)."""
        return re.findall(r"location (?:(~) )?(\S+) \{\n(.*?)\n    \}", self.code, re.S)

    def test_it_proxies_everything_to_the_app_on_loopback_and_says_the_request_was_https(self):
        locs = self.locations()
        self.assertEqual([(kind, path) for kind, path, _ in locs], [("~", "^/sites/[a-z0-9-]+/(install|password|security)$"), ("~", "^/import/files/[A-Za-z0-9._-]+$"), ("", "/")],
                         "the slow pages, the upload, the catch-all; a new location is a decision of its batch")
        self.assertEqual(re.findall(r"proxy_pass (\S+);", self.code), ["http://127.0.0.1:8000"] * 3)
        for kind, path, block in locs:
            self.assertIn("proxy_set_header X-Forwarded-Proto https;", block, path)
            self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", block, "the client's address, not a list a client can extend: " + path)
            self.assertIn("proxy_set_header Host $host;", block, path)
            self.assertIn("proxy_http_version 1.1;", block, path)
        self.assertNotIn("$proxy_add_x_forwarded_for", self.code)

    def test_install_password_and_the_vulnerability_check_wait_longer_than_the_tool_may_run(self):
        slow = dict((path, block) for _, path, block in self.locations())["^/sites/[a-z0-9-]+/(install|password|security)$"]
        self.assertIn("proxy_read_timeout 700s;", slow, "longer than wp-ctl's 600 s for install")
        self.assertNotIn("proxy_request_buffering", slow)

    def test_the_body_limit_is_small_everywhere_but_the_upload_which_is_the_intake_quota_unbuffered(self):
        upload = dict((path, block) for _, path, block in self.locations())["^/import/files/[A-Za-z0-9._-]+$"]
        self.assertEqual(re.findall(r"client_max_body_size (\S+);", self.code), ["1m", "%sm" % net.HOSTS["dellwp"]["wp_intake_quota_mb"]])
        self.assertNotIn("client_max_body_size", self.code.split("location ~")[0].split("server_name")[1].split("client_max_body_size 1m;")[1], "the small limit is the server's; only the upload sets its own")
        self.assertIn("client_max_body_size %sm;" % net.HOSTS["dellwp"]["wp_intake_quota_mb"], upload)
        self.assertIn("proxy_request_buffering off;", upload, "nginx keeps no copy of the file: the app sees the bytes as they arrive")
        self.assertEqual(self.code.count("proxy_request_buffering"), 1, "only the upload")
        self.assertIn("limit_except PUT { deny all; }", upload, "the upload location takes PUT and nothing else")
        self.assertIn("client_body_timeout 300s;", upload, "a body that stalls is dropped")
        for t in ("proxy_read_timeout", "proxy_send_timeout"):
            m = re.search(r"%s (\d+)s;" % t, upload)
            self.assertTrue(m and int(m.group(1)) > 14400, t + ": at least as long as wp-ctl lets import-receive run (4 hours)")

    def test_the_apps_upload_limit_is_the_intake_quota_the_nginx_limit_is_made_from(self):
        ctl_py = (ROOT / "app/wp_panel/ctl.py").read_text()
        self.assertIn("UPLOAD_MAX = 3072 * 1024 * 1024", ctl_py)
        self.assertEqual(int(net.HOSTS["dellwp"]["wp_intake_quota_mb"]), 3072)

    def test_nginx_accepts_it_when_nginx_is_here(self):
        if shutil.which("nginx") is None:
            self.skipTest("nginx is not installed here; the play's own `nginx -t` and the rehearsal check it on the VM")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "privkey.pem"), "-out", str(d / "fullchain.pem"), "-days", "2", "-subj", "/CN=x"], check=True, capture_output=True)
            conf = self.text.replace(net.resolve("{{ le_cert_dir }}"), str(d)).replace(net.HOSTS["aopenpanel"]["overlay_ip"] + ":443", "127.0.0.1:18443")
            (d / "nginx.conf").write_text("pid %s/n.pid; error_log %s/e.log; events {} http { access_log off; %s }\n" % (d, d, conf))
            r = subprocess.run(["nginx", "-t", "-c", str(d / "nginx.conf"), "-p", str(d)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class Play(unittest.TestCase):
    def test_the_play_targets_the_vm_and_stops_early_before_it_changes_anything(self):
        self.assertEqual((PLAY["hosts"], PLAY["become"]), ("aopenpanel", True))
        names = [t["name"] for t in TASKS]
        first_change = names.index("Install nginx")
        stops = [i for i, t in enumerate(TASKS) if "ansible.builtin.assert" in t]
        self.assertEqual(len(stops), 2)
        self.assertTrue(all(i < first_change for i in stops), "both stops come before the first change")
        self.assertLess(names.index("Read dellwp's ssh host key from dellwp itself (it is pinned, never trusted on first use)"), first_change)

    def test_the_secret_stop_hides_values_and_asks_for_every_form_the_app_reads(self):
        t = task("a secret is missing")
        self.assertTrue(t["no_log"])
        that = " ".join(t["ansible.builtin.assert"]["that"])
        for name in cred.KEYS:
            self.assertIn(name + " is defined", that)
        self.assertIn("scrypt|pbkdf2", that)
        self.assertIn("[A-Z2-7]{16,128}", that)
        self.assertIn("BEGIN OPENSSH PRIVATE KEY", that)
        self.assertIn("not shown", t["ansible.builtin.assert"]["fail_msg"])

    def test_the_variables_the_play_reads_are_the_ones_the_credentials_helper_writes(self):
        text = PLAY_PATH.read_text()
        used = set(re.findall(r"aopenpanel_panel_[a-z_]+", text))
        self.assertEqual(used, set(cred.KEYS))

    def test_every_secret_is_a_private_file_of_the_apps_uid_that_never_reaches_the_log_or_a_diff(self):
        t = task("Write the app's secrets")
        copy = t["ansible.builtin.copy"]
        self.assertEqual((copy["owner"], copy["group"], copy["mode"]), ("{{ panel_uid }}", "{{ panel_uid }}", "0400"))
        self.assertTrue(t["no_log"] and t["diff"] is False)
        self.assertEqual(t["notify"], "Restart the control app")
        self.assertEqual([i["name"] for i in t["loop"]], ["session_key", "password_hash", "totp_secret", "panel_ssh_key"])
        settings = (ROOT / "app/wp_panel/config.py").read_text()
        for name in ("session_key", "password_hash", "totp_secret", "panel_ssh_key"):
            self.assertIn('"%s"' % name, settings, "the app reads a file called %s" % name)
        self.assertEqual(copy["content"], "{{ item.value | trim }}\n")
        for name in cred.KEYS:
            self.assertEqual(len([i for i in t["loop"] if name in i["value"]]), 1)

    def test_dellwps_host_key_is_read_from_dellwp_and_pinned_for_the_app(self):
        read = task("Read dellwp's ssh host key")
        self.assertEqual(read["delegate_to"], "dellwp")
        self.assertEqual(read["ansible.builtin.slurp"]["src"], "/etc/ssh/ssh_host_ed25519_key.pub")
        pin = task("Pin dellwp's host key")["ansible.builtin.copy"]
        self.assertEqual(pin["dest"], "{{ panel_secret_dir }}/known_hosts")
        self.assertEqual(pin["mode"], "0400")
        self.assertEqual(pin["content"], "{{ panel_ctl_host }} {{ panel_dellwp_hostkey.content | b64decode | trim }}\n")
        env = jinja2.Environment(keep_trailing_newline=True)       # Ansible keeps the trailing newline of a templated string
        env.filters["b64decode"] = lambda s: __import__("base64").b64decode(s).decode()
        line = env.from_string(pin["content"]).render(panel_ctl_host="10.8.0.41", panel_dellwp_hostkey={"content": __import__("base64").b64encode(b"ssh-ed25519 AAAAC3Nza host\n").decode()})
        self.assertEqual(line, "10.8.0.41 ssh-ed25519 AAAAC3Nza host\n", "a known_hosts line: address, type, key")
        self.assertIn("/known_hosts", (ROOT / "app/wp_panel/config.py").read_text().replace('"known_hosts"', "/known_hosts"))

    def test_the_image_is_built_only_when_it_is_missing_and_named_by_the_files_it_was_built_from(self):
        build = task("Build the image")
        self.assertIn("panel_image_found.rc == 1", build["when"])
        self.assertTrue(build["changed_when"] is True)
        self.assertEqual(build["notify"], "Restart the control app")
        self.assertIn("docker build --pull=false -t {{ panel_image }} {{ panel_build_dir }}", build["ansible.builtin.command"])
        h = task("Hash the build context")["ansible.builtin.shell"]["cmd"]
        for part in ("find . -type f -print0", "LC_ALL=C sort -z", "sha256sum", "cut -c1-12", "set -o pipefail"):
            self.assertIn(part, h)
        self.assertEqual(task("Name the image")["ansible.builtin.set_fact"]["panel_image"], "wp-panel:{{ panel_tree_hash.stdout | default('unbuilt') }}")
        inspect = task("Look for that image")
        self.assertEqual(inspect["failed_when"], "panel_image_found.rc not in [0, 1]")
        self.assertIs(inspect["changed_when"], False)

    def test_the_build_context_is_app_itself_deleting_what_is_gone_and_leaving_the_caches_out(self):
        s = task("Put app/ on the VM")
        sync = s["ansible.posix.synchronize"]
        self.assertTrue(sync["src"].endswith("/../app/") and sync["dest"] == "{{ panel_build_dir }}/")
        self.assertIs(sync["delete"], True)
        self.assertIs(s["become"], False, "rsync writes as admin, who owns the directory")
        self.assertEqual(sorted(sync["rsync_opts"]), ["--exclude=*.pyc", "--exclude=__pycache__"])

    def test_the_templates_the_play_uses_exist(self):
        for t in TASKS:
            body = t.get("ansible.builtin.template")
            if body:
                src = body["src"].replace("{{ panel_template_dir }}", "templates/wp_panel")
                self.assertTrue((ROOT / src).is_file(), src)
        self.assertEqual(PLAY["vars"]["panel_template_dir"], "{{ playbook_dir }}/../templates/wp_panel")

    def test_nginx_is_checked_before_it_is_reloaded_and_everything_is_checked_through_it_at_the_end(self):
        names = [t["name"] for t in TASKS]
        i_check = names.index("Let nginx check its whole configuration before anything is reloaded")
        i_start = names.index("Start the app and nginx, now and at boot")
        i_flush = names.index("Apply queued restarts and reloads now")
        i_wait = names.index("Wait for the app to listen on loopback")
        i_ask = names.index("Ask for the login page through nginx, with the certificate checked against the name")
        self.assertLess(names.index("Write the vhost"), i_check)
        self.assertLess(i_check, i_start)
        self.assertLess(i_start, i_flush)
        self.assertLess(i_flush, i_wait)
        self.assertLess(i_wait, i_ask)
        check = TASKS[i_check]
        self.assertEqual(check["ansible.builtin.command"], "nginx -t", "the whole configuration is tested, not a file")
        self.assertIs(check["changed_when"], False)
        uri = TASKS[i_ask]["ansible.builtin.uri"]
        self.assertIs(uri["validate_certs"], True)
        self.assertEqual(uri["url"], "https://{{ panel_domain }}/login")
        self.assertEqual(TASKS[i_wait]["ansible.builtin.wait_for"]["port"], 8000)
        self.assertEqual(TASKS[i_start]["loop"], ["wp-panel", "nginx"])
        self.assertIs(TASKS[i_start]["ansible.builtin.systemd"]["enabled"], True)

    def test_every_notify_names_a_handler_and_the_handlers_restart_and_reload(self):
        handlers = {h["name"]: h for h in PLAY["handlers"]}
        notified = {t["notify"] for t in TASKS if "notify" in t}
        self.assertEqual(notified, set(handlers))
        self.assertEqual(handlers["Restart the control app"]["ansible.builtin.systemd"]["state"], "restarted")
        self.assertEqual(handlers["Reload nginx"]["ansible.builtin.systemd"]["state"], "reloaded")

    def test_nginxs_default_site_goes_so_nothing_listens_on_port_80(self):
        t = task("Drop nginx's default site")
        self.assertEqual((t["ansible.builtin.file"]["path"], t["ansible.builtin.file"]["state"]), ("/etc/nginx/sites-enabled/default", "absent"))

    def test_nothing_ignores_errors_runs_a_shell_it_does_not_need_or_prints_a_secret(self):
        text = PLAY_PATH.read_text()
        self.assertNotIn("ignore_errors", text)
        self.assertEqual(len(re.findall(r"ansible\.builtin\.shell", text)), 1, "only the hash, which needs a pipe")
        for t in TASKS:
            if "aopenpanel_panel_" in str(t) and t["name"] != "Stop if a secret is missing or not in the form the app reads":
                self.assertTrue(t.get("no_log"), t["name"])
            self.assertNotIn("debug", [k for k in t if k.endswith("debug")])


class SecretsContract(unittest.TestCase):
    """The credentials helper's file, written the way the play writes it, read by the app's settings loader and by ssh-keygen."""

    def test_files_made_by_the_helper_and_written_like_the_play_load_and_the_key_is_a_key(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "plain.yml"
            pw = "a long passphrase for the contract test"
            rc = cred.main(["make", str(out)], getpw=lambda p, it=iter([pw, pw]): next(it), out=lambda *a: None)
            self.assertEqual(rc, 0)
            values = cred.read_values(str(out))
            secrets = d / "secrets"
            secrets.mkdir()
            by_name = dict(zip(("session_key", "password_hash", "totp_secret", "panel_ssh_key"), cred.KEYS))
            for name, var in by_name.items():
                (secrets / name).write_text(values[var].strip() + "\n")           # content: "{{ item.value | trim }}\n"
                (secrets / name).chmod(0o400)
            s = config.load({"PANEL_CTL_HOST": "10.8.0.41", "PANEL_SITE_DOMAIN": "example.test", "PANEL_SECRET_DIR": str(secrets)})
            self.assertEqual(s.password_hash, values["aopenpanel_panel_password_hash"])
            self.assertEqual(len(s.session_key), len(values["aopenpanel_panel_session_key"]) + 1)
            self.assertTrue(cred.password_ok(s.password_hash, pw))
            pub = subprocess.run(["ssh-keygen", "-y", "-f", str(secrets / "panel_ssh_key")], capture_output=True, text=True)
            self.assertEqual(pub.returncode, 0, "ssh reads the key as the play writes it: " + pub.stderr)
            self.assertEqual(pub.stdout.split()[:2], Path(str(out) + ".pub").read_text().split()[:2])


if __name__ == "__main__":
    unittest.main()
