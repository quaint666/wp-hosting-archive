#!/usr/bin/env python3
"""Render one WordPress site's files on dellwp from the templates the Ansible play also uses: .env, wp-config.php, php.ini, fpm-pool.conf, compose.yml, the systemd unit and the nginx vhost.
The templates (templates/wp/site_*.j2 and templates/compose_stack.service.j2) stay the single source; this module is the Jinja environment of the play without Ansible: the same trim and newline
settings, and the two Ansible-only filters the site templates use (hash, difference), written to give the same text. The platform values (images, uids, paths, limits) come from the caller
as one dict, the master secret from a root-only file; the site's secrets derive from the master secret exactly as the Ansible tasks this replaced derived them (tasks/wp_site_one_common.yml, gone; git has it), so a site made either way has the same passwords.

It returns the files with their owner, group and mode and writes nothing: the provisioning tool (a later batch) decides when and in which order they reach the disk.
Needs python3-jinja2 (3.1.6 on dellwp). Tests: tests/test_wp_site_renderer.py renders every site shape through this and through the play's offline test helper and demands identical text."""
import hashlib
import os
import stat
from pathlib import Path

import jinja2
import jinja2.meta

# The names a site's templates take from the site itself, not from the platform.
SITE_NAMES = frozenset({"wp_s", "wp_host", "wp_site_dir", "wp_secret_base", "wp_db_password"})
UNIT_PARAMS = frozenset({"compose_stack_description", "compose_stack_dir", "compose_stack_start_timeout_s", "compose_stack_stop_timeout_s", "compose_stack_reload_service"})
OPTIONAL = frozenset({"dellwp_wp_resend_key"})                                # read by site_wp-config.php.j2 only inside `mail_mode == 'send'`
# What the renderer and the provisioning tool read themselves (host name, paths, the unit's timeouts, the group of wp-config.php, the backup directory and its project id offset), beyond what the templates read.
OWN_NEEDS = frozenset({"wp_domain", "wp_data_mount", "wp_unit_dir", "wp_vhost_dir", "wp_stack_start_timeout_s", "wp_stack_stop_timeout_s", "wp_php_uid", "wp_backup_dir", "wp_backup_quota_offset"})
TEMPLATES = ("wp/site_env.j2", "wp/site_wp-config.php.j2", "wp/site_php.ini.j2", "wp/site_fpm_pool.conf.j2", "wp/site_compose.yml.j2", "wp/site_nginx.conf.j2", "compose_stack.service.j2")


class RenderError(Exception):
    """A template that cannot be rendered (a variable the platform dict lacks, a filter that does not exist). Nothing has been written."""


class SecretFileError(Exception):
    """The secret file is missing, a link, or readable by someone other than its owner."""


def hash_filter(value, algo="sha1"):
    """Ansible's `hash` filter: the hex digest of the text."""
    return hashlib.new(algo, str(value).encode()).hexdigest()


def difference(a, b):
    """Ansible's `difference`, in the order of `a`. (Ansible returns a set operation's result in any order; a template that depended on the order would be a bug, and the site templates sort.)"""
    return [x for x in a if x not in b]


def intersect(a, b):
    return [x for x in a if x in b]


def environment(template_dir):
    """The play's settings: trim_blocks and keep_trailing_newline are what Ansible's template module uses [inferred from the offline tests, proven on the host by the parity run of the next batch]."""
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir)), undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters.update(hash=hash_filter, difference=difference, intersect=intersect)
    return e


def needed_platform_vars(template_dir):
    """(required, optional): the platform values the renderer needs: what the site templates read that is neither the site's nor the unit's parameters, plus OWN_NEEDS. The platform dict must carry
    the first; the second only when it applies."""
    e = environment(template_dir)
    seen = set()
    for name in TEMPLATES:
        seen |= jinja2.meta.find_undeclared_variables(e.parse(Path(template_dir, name).read_text()))
    return sorted((seen - SITE_NAMES - UNIT_PARAMS - OPTIONAL) | OWN_NEEDS), sorted(seen & OPTIONAL)


def read_secret_file(path):
    """The text of a root-only file with its one trailing newline taken off. Refuses a link, anything but a regular file, another owner than the one running this, and any group or other access."""
    p = Path(path)
    if p.is_symlink():
        raise SecretFileError("%s is a link" % p)
    try:
        st = p.stat()
    except OSError as e:
        raise SecretFileError("%s: %s" % (p, e))
    if not stat.S_ISREG(st.st_mode):
        raise SecretFileError("%s is not a regular file" % p)
    if st.st_uid != os.geteuid():
        raise SecretFileError("%s belongs to uid %d, not to the user running this" % (p, st.st_uid))
    if st.st_mode & 0o077:
        raise SecretFileError("%s is readable by group or others (mode %o)" % (p, st.st_mode & 0o777))
    text = p.read_text()
    return text[:-1] if text.endswith("\n") else text


def secret_base(master, name, rev):
    return "%s|%s|%s" % (master, name, rev)


def db_password(base):
    return hash_filter(base + "|db", "sha256")


class Renderer:
    def __init__(self, template_dir, platform, master_secret, resend_key=None):
        self.env, self.platform, self.master, self.resend = environment(template_dir), dict(platform), master_secret, resend_key
        required, _ = needed_platform_vars(template_dir)
        missing = [k for k in required if k not in self.platform]
        if missing:
            raise RenderError("the platform values lack: %s" % ", ".join(missing))
        if not master_secret:
            raise RenderError("no master secret")

    def context(self, site):
        """The variables of one site's templates. `site` = the record over the defaults (every key a template reads must be there)."""
        base = secret_base(self.master, site["name"], site["secret_rev"])
        ctx = dict(self.platform)
        ctx.update(wp_s=site, wp_host="%s.%s" % (site["name"], self.platform["wp_domain"]), wp_site_dir="%s/sites/%s" % (self.platform["wp_data_mount"], site["name"]),
                   wp_secret_base=base, wp_db_password=db_password(base))
        if self.resend is not None:
            ctx["dellwp_wp_resend_key"] = self.resend
        elif site.get("mail_mode") == "send":
            raise RenderError("mail_mode is send but dellwp has no Resend key (dellwp_wp_resend_key): run scripts/wp_provider_key.sh resend, then dellwp/wp_sites_dellwp.yml, or set mail_mode back to log")
        return ctx

    def one(self, template, ctx):
        try:
            return self.env.get_template(template).render(**ctx)
        except (jinja2.UndefinedError, jinja2.TemplateError) as e:
            raise RenderError("%s: %s" % (template, e))

    def files(self, site):
        """The files of a site, in the order the play deploys them: a list of {path, content, owner, group, mode, secret}. owner and group are names or numbers; mode is an int."""
        ctx, p = self.context(site), self.platform
        d = ctx["wp_site_dir"]
        unit_ctx = dict(ctx, compose_stack_description="WordPress site %s" % site["name"], compose_stack_dir=d, compose_stack_start_timeout_s=p["wp_stack_start_timeout_s"],
                        compose_stack_stop_timeout_s=p["wp_stack_stop_timeout_s"], compose_stack_reload_service="php")
        plain = [("php.ini", "wp/site_php.ini.j2"), ("fpm-pool.conf", "wp/site_fpm_pool.conf.j2"), ("compose.yml", "wp/site_compose.yml.j2")]
        out = [
            dict(path=d + "/.env", content=self.one("wp/site_env.j2", ctx), owner="root", group="root", mode=0o600, secret=True),
            dict(path=d + "/wp-config.php", content=self.one("wp/site_wp-config.php.j2", ctx), owner="root", group=p["wp_php_uid"], mode=0o640, secret=True),
        ]
        out += [dict(path="%s/%s" % (d, dest), content=self.one(src, ctx), owner="root", group="root", mode=0o644, secret=False) for dest, src in plain]
        out += [
            dict(path="%s/wp-%s.service" % (p["wp_unit_dir"], site["name"]), content=self.one("compose_stack.service.j2", unit_ctx), owner="root", group="root", mode=0o644, secret=False),
            dict(path="%s/wp_site_%s.conf" % (p["wp_vhost_dir"], site["name"]), content=self.one("wp/site_nginx.conf.j2", ctx), owner="root", group="root", mode=0o644, secret=False),
        ]
        return out
