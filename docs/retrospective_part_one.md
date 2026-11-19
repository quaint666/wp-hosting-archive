# Retrospective, part one: WordPress hosting for friends, explained

*Archive edition, 2026-11-19. This is part one of a three-part retrospective written during the work (parts two and three compare the programming with a second project and describe the home lab under both; they are not included). It is longer and more explanatory than [`wordpress_hosting.md`](wordpress_hosting.md), and written for the operator ("you"). The questions addressed to the operator in section 1.2 were answered on 2026-11-19, the day the service was retired; the answers are added to that table next to the original assumptions.*

## 1.1 The service on one page

You run WordPress sites for a few friends. Each site is a small, sealed stack on a virtual machine called `dellwp`
in your home lab. The public internet reaches a site through a Cloudflare tunnel, so nothing at home accepts a
connection from the internet. You are the only operator. You create a site, you bring a friend's existing site in
from a file, you decide what code a friend's site may run, you watch the backups and the updates, and you delete a
site when a friend leaves. The friend gets a WordPress administrator login protected by two-factor authentication and
nothing else: no server access, no way to install plugins.

Since 2026-10-30 there are two ways to do all of that.

1. **The command line on `dellwp`**, through one program, `sudo wp-ctl <verb> key=value ...`. It is the older of the
   two and always works, even if everything else is broken.
2. **The panel**, a web page at `https://wp-panel.lab.example.com/`, reachable only over the WireGuard
   overlay and, of the machines on it, only from your ThinkPad. It logs you in with a password and a code from an
   authenticator app, and then does what `wp-ctl` can do, less three verbs it has no page for, by sending `wp-ctl` the same requests.

Everything about the service is written down as code in this repository: how the VM is set up (`dellwp/`, `tasks/`,
`templates/wp/`), how a site is built (`scripts/wp_site_provision.py`, the registry rules in
`scripts/wp_site_model.py`), the tools on the VM (`scripts/wp_site_*.py`, `templates/wp/*.sh.j2`), the dispatcher
(`scripts/wp_ctl.py`), the panel (`app/wp_panel/`), the tests that prove each of those do what they say (`tests/`),
and the rehearsal scripts that run a whole site's life against the real machines (`scripts/*_rehearsal_*.sh`).

**If this is new:** "a WordPress site" is a PHP program (WordPress) that keeps its content in a MariaDB database and
its uploads and plugins in a folder on disk. To serve a page, a web server (nginx) hands the request to a PHP process
(php-fpm), which asks the database what to show and builds the HTML. "A stack" here means those pieces for one site:
one php-fpm container with the WordPress files and one MariaDB container with that site's data. A "container" is a
process started from a packaged filesystem image with a ceiling on how much it can use and a limited view of the
machine; it is lighter than a virtual machine and weaker as a wall. A "virtual machine" is a whole pretend computer
running on a real one.

**For the expert:** the design is a deliberately small hosting platform with three properties you would not get by
default from a generic container host. It is *strict where a provider would be forgiving* (a hostile or damaged
`.wpress` cannot make WordPress execute attacker-chosen code before a human has approved it), *declarative where it can
be* (the platform is code in the repository; the tenants are records in a registry), and *auditable by construction*
(the program that holds the privilege writes the audit line, not the program that asked).

### Context only we know: how the service came to exist

The WordPress hosting started on 2026-10-23, straight after the Dell was rebuilt as a Proxmox host with two VMs: the
shop (`dellapps`, Bloomy's home) and `dellwp`. The first requirement you stated was not technical: *run it like a
hosting service, but you are not a professional hosting provider, the stakes are low, and you want a balance between
security and ease for the friends, "not like an enterprise"* `[stated]`. Friends could not be assumed to pay for
anything such as Cloudflare Access `[stated]`. That sentence explains more of the design than any diagram: it is why
the friends' login is an ordinary WordPress login with two-factor rather than a VPN, why importing is a command and
not a ticket system, and why the answer to "what if a friend asks for a plugin" is "ask you".

The service then grew in four visible layers, each proven before the next started:

1. **The site engine** (2026-10-23 and 24): Ansible plays that build a site from a list in `host_vars/dellwp/vars.yml`
   (`wp_sites`), with quotas, timers and nginx rules. Retired on 2026-10-29; its tombstone numbers live on as
   `wp_closed_floor`.
2. **The tools and the import** (2026-10-24 and 25): backup, recover, update, scan, the `.wpress` importer, phone
   approvals. All of it still in use, now called through the dispatcher.
3. **The dispatcher and the registry** (2026-10-28 and 29): `wp-ctl`, the site registry, `create`, `set`,
   `ensure-state`, `delete`; the old engine and `scripts/wp_sites.py` deleted.
4. **The panel** (2026-10-29 and 30): the control VM, the app, the pages, the upload.

That order matters for understanding the code: the panel is the *last* and *thinnest* layer, and it was allowed to
exist only because the layers beneath it were already correct without it.

## 1.2 Who the sites serve, what they are for, and why that decides everything

Almost every decision in this service follows from who the sites are for. A hosting design is not right or wrong in
the abstract; it is right or wrong for a tenant. So before any mechanism, here is what is known about the tenants,
what is only assumed, and what you still need to fill in.

### What is known `[stated]` or `[verified]`

- The tenants are **friends**. Not customers, not strangers. You described the relationship as a favour, not a
  product (section 2 of the older explainer: "This is a favour for friends `[stated]`, not a product").
- A friend's site arrives as a **`.wpress` file**, the format of the All-in-One WP Migration plugin (the file Local by
  Flywheel also writes). The practice file in `wpress/` is 290 MB `[verified]` and holds a WooCommerce shop and an
  Elementor-built front end; the first real scan found at least three CVEs in its WooCommerce 11.0.1, one rated 7.5,
  and correctly withheld the Approve button `[verified]`.
- The hosting is **small**: three sites at most on `dellwp` today (`host_vars/delloptiplex3040/vars.yml`:
  "the WordPress hosting is held to three sites at most") `[verified]`. A fourth would mean more RAM.
- **You are the only operator**, often away from the keyboard, approving things from a phone `[stated]`.
- The sites are **public** (reached through the Cloudflare tunnel on `<site>.example.com`) but a new site
  starts on `hold`, where only the VM itself can see it, and goes public only when you say so with the site's name.
- The friends are **trusted but fallible**: their laptops, passwords and plugin choices are as weak as anyone's. The
  design assumes a friend's site *will* be compromised one day and limits what that costs.

### What was assumed, and the operator's answers (2026-11-19)

The previous explainer never described the sites' *content*, and neither did any handoff. The table gives the working assumption written at the time and, added in the archive edition, the operator's answer `[stated: 2026-11-19]`.

| Question | Working assumption (at the time) | Answer (operator, 2026-11-19) | Why it matters |
|---|---|---|---|
| What do the friends' sites do? Portfolios, small shops, event pages, a club site? | A mix of brochure sites and one or two small WooCommerce shops, like the practice file `[inferred]` | School project sites and some small blogs | A shop means money, personal data and payment plugins: the compromise cost is higher and a backup gap hurts more. A brochure site is mostly about not being defaced. |
| Do any of them take payments or collect personal data on this server? | Payments go through a hosted gateway (the plugin redirects), no card data touches `dellwp` `[inferred]` | No. Any payments were sandbox payments with no real personal data | It decides whether the "compliance paperwork" gap in section 1.4 is a nuisance or a blocker. |
| How much traffic does a typical friend's site get? | A few hundred visits a week at most `[inferred]` | At most fewer than 10 visitors a day | It decides whether Cloudflare's free proxy is enough and whether 512 MB of PHP memory per site is generous. |
| Who edits content, and how often? | The friend, in WordPress admin, weekly to monthly `[inferred]` | The site's admin (the friend), almost never: set and forget | It decides how much data changes between backups (the nightly cadence is fine for weekly edits, thin for a daily blog). |
| What happens to a site when a friend loses interest? | It sits on `hold` or `stopped` and eventually gets deleted with the 14-day archive `[inferred]` | Closed after asking the friend beforehand | It is the reason `stopped` and `closed` exist as separate states. |
| Is a friend ever allowed to run custom code (a bespoke theme, a code snippet plugin)? | Only if you review it and install it by hand; never through the admin `[stated: "approve mode only, no auto"]` | No | It is the core of the "no plugin installs" decision. |
| What does the friend expect if the site is down? | An apologetic message to you, not a refund `[inferred]` | Not much | It decides that having no SLA is acceptable. |

> **Answered 2026-11-19 `[stated]`:** the sites were school projects and small blogs, with no real payments or personal data (any shop was a sandbox), fewer than ten visitors a day, content changes almost never, no custom code, and not much expected of the service. So the low-stakes reading of this section was right; the text below was written before these answers and is left as it was.

### Why "friends" is a stronger design input than "customers"

If the tenants were customers, you would need isolation that holds against a *hostile* tenant (a customer who wants
to read another's data), billing, an abuse process, an SLA, and legal terms. None of that is present, and that is not
negligence: it is the point. The real design questions for friends are different.

1. **Mistakes dominate malice.** A friend uploads a plugin with a known hole, reuses a password, or installs a
   "nulled" theme with a backdoor in it. The design does not need to stop a determined attacker among its tenants; it
   needs to stop an *accident* from spreading from one friend's site to another's, or to the shop that shares the
   hardware. That is why the isolation is a VM (away from the shop) plus a stack per site (away from each other), and
   why it is not a hardened multi-tenant kernel.
2. **Trust is high and so is the cost of being wrong.** A friend will not read a security notice. A friend will
   forgive downtime, but not lost photos or a defaced wedding page. So backups with proven restores outrank uptime.
3. **Your attention is the scarce resource.** You have a job, a course, a shop and a lab. Anything that needs your
   judgement while you are away must carry its own facts and be reversible; anything routine must not need you at all.
   That is the origin of phone approvals, of the nightly update with automatic rollback, and of the panel itself:
   fewer keystrokes, fewer mistakes, the same safety.

### Context only we know: "the stakes are low" does not mean "the standard is low"

You said the stakes are low, and in terms of money and users that is true. But look at how the work went. A fortnight
of effort produced a system whose backups are restored monthly by a drill, whose imports cannot run attacker code
before approval, whose control path is an allowlist behind a forced ssh key, and whose every destructive action asks
for a typed name. That is not an enterprise standard because an enterprise needs it. It is your standard, and the
reason for it is the second sentence of the previous section: for friends, the cost of an accident is a relationship,
not a refund. Keep that framing when you decide, later, how much of this to simplify. Removing a safeguard because
"the stakes are low" is a different decision from not building one because they are low, and the first one is harder
to take back.

## 1.3 Fundamentals: what has to be true for a WordPress page to appear

This section exists so that the later sections can name parts without explaining them. A newcomer can read it as a
tour; an expert can skim for the names we use.

### The request, from the visitor's browser to the database and back

A visitor opens `https://friend.example.com/about/`.

1. **DNS.** The name resolves, through Cloudflare, to a Cloudflare edge address. There is a wildcard record for the
   domain, so a new site name needs no DNS change (`terraform/cloudflare/main.tf` declares the wildcard and the
   tunnel's wildcard ingress `[verified]`).
2. **TLS at the edge.** Cloudflare terminates HTTPS with its own certificate for the name. Its free "Universal"
   certificate covers one label under the domain, which is why sites are `<name>.example.com` and not
   `<name>.hosting.example.com` `[from the older handoff]`.
3. **The tunnel.** Cloudflare passes the request down a tunnel that `cloudflared` on `dellwp` opened *outbound*.
   Nothing at home listens for the internet. The visitor's real address arrives in a header, which nginx trusts only
   when the connection really comes from the tunnel, so the login throttle can key on it.
4. **nginx on `dellwp`**, outside Docker, on a loopback port. One server block per site; it applies rate limits,
   serves the "this site is paused" page for a stopped site, and passes the request to that site's php-fpm.
5. **php-fpm in the site's container.** It runs WordPress's PHP with a hardened `php.ini`. The WordPress code is
   mounted read-only; only `uploads` is writable.
6. **MariaDB in the site's other container**, on a private network that contains only those two containers. The
   database is not published anywhere.
7. **Back out**, in reverse. Beyond what Cloudflare caches, no page cache is configured on the VM `[inferred]`, so a
   typical dynamic page is built each time.

**For the expert:** the nginx-outside-Docker choice trades isolation for operability. One nginx means one place for
rate limits and maintenance pages, and a stack is just two containers that restart as one systemd unit. The cost is
that a bug in nginx is a bug for every site, and that nginx's configuration is a shared resource every site play
touches. The alternative, a reverse proxy container per site or a proxy that discovers containers by label, was not
chosen because nothing in the tenant list needs it.

### Words that carry weight in the rest of the document

- **Hypervisor / VM.** Proxmox on the Dell runs two VMs. `dellwp`'s kernel is not the host's; a break-out from a
  container reaches the VM, not the hypervisor, and not the shop.
- **Docker Compose stack under systemd.** `systemctl restart wp-<site>` is the only way a stack changes state, so
  Ansible, a human and the panel cannot fight over container state. (A standing rule, repeated in several places: never
  `docker restart` a systemd-managed container.)
- **Project quota.** ext4 can limit a directory tree's disk use by "project". Each site has a quota for its files and a
  separate one for its backups, so a site that fills its disk cannot stop its own backup, and a hostile upload cannot
  eat the VM.
- **Registry.** The record of which sites exist: one JSON file per site under `/srv/wp/sites/<name>/site.json`. It
  replaced a list in `host_vars/dellwp/vars.yml`. Section 1.6 explains why.
- **Tombstone.** The record that a name once existed and was closed. A closed site's name can be used again; its
  port and its quota id never are.
- **Dispatcher.** `wp-ctl`: the one program allowed to start any change on `dellwp` on the panel's behalf.
- **Forced command.** An SSH feature: an `authorized_keys` line can say "whatever this key asks for, run this program
  instead". It is the load-bearing wall of the panel's security (section 1.7).
- **Job.** A change that takes more than a few seconds (a backup, a state change, an import). It runs as a
  transient systemd unit with a directory under `/var/lib/wp-ctl/jobs/<id>/` holding its request, its log and its exit
  code. The panel shows a job's page and refreshes it while the job runs.
- **Hold, live, stopped, closed.** The four states of a site (section 1.6).
- **.wpress.** The archive format of All-in-One WP Migration: a stream of file records (the database dump, `wp-content`
  files, a `package.json` describing the site). Section 1.8 explains why we do not trust one.

## 1.4 What a hosting provider does behind the curtain, and what we do instead

A paid WordPress host sells a long list of things that you only notice when they are missing. "Provider" below means a
managed host such as Kinsta, WP Engine or Cloudways, or a cPanel shared host; the statements about them are
`[from memory]` and typical, not exact. The older explainer's table is kept and updated for the panel and the
registry.

| Concern | A provider typically | We do | Why, for our tenants |
|---|---|---|---|
| Isolation between customers | one container or account per site; stronger on managed hosts, weak on cheap shared hosts | one VM for all WordPress; inside it a Docker stack per site with its own network, database container, project disk quota, memory and CPU limits | A break-in on one friend's site must not reach another's database or fill the disk. The VM keeps WordPress away from the shop that shares the Dell. |
| Who can change code | the customer, usually, through the admin (plugin and theme installs) | nobody through the web: `DISALLOW_FILE_MODS`, code read-only to PHP, plugins only from wordpress.org by slug, installed by you | The commonest way a WordPress site is taken over is a vulnerable or malicious plugin. If the admin cannot add code, a stolen friend password cannot add code either. |
| Updates | automatic, often with a visual check | a nightly job per site: snapshot, update, check the home page, the login form and PHP errors, roll back by itself on failure; major plugin updates held for you | You cannot be support staff. A silent bad update must undo itself. |
| Backups | daily, kept 14 to 30 days, a restore button | nightly snapshot (database dump plus hard-linked `wp-content`), 7 daily and 3 weekly kept, an hourly freshness check, a monthly drill that restores one site into a scratch copy and boots it | A backup that was never restored is a hope. The drill is the proof. |
| Restore | one click, by support | `wp-site-recover`: replaces database and files from a snapshot in place, with an undo, and proves WordPress boots before reopening | The undo is what makes trying it safe. |
| Malware and vulnerabilities | scanners, managed WAF, often virtual patching | every import scanned before you are asked (ClamAV in a pinned container, patterns, checksums against wordpress.org) and every site checked daily against the Wordfence vulnerability feed; Cloudflare's free WAF in front | Cheap, real, and honest about its limits (section 1.9). |
| Migration in | a plugin or a support ticket | a plan-run-report-approve pipeline with a defined trust model (section 1.8), now also a page | Migration is the riskiest moment: you are running a stranger's database. |
| Control panel | cPanel, a proprietary dashboard | `wp-ctl` plus the panel: 21 verbs, 18 of them on pages, a login with a code, typed-name confirmations, an audit file on the VM | A provider's panel runs as root on the server and has a very large attack surface. Ours can only say the 18 things its client allows. |
| Staging and previews | a button | none yet; the `hold` state is a private preview where only the VM can see the site | A friend's test site is the practice here. |
| Email | built in or an add-on | a must-use plugin that sends through Resend's HTTPS API from `<site>@mail.example.com`; log mode by default | The VM cannot send SMTP at all (a firewall rule), by design: nothing on it can spam. |
| Login security | brute-force limits, sometimes 2FA | an nginx rate limit, a second throttle in PHP, a generic login message, XML-RPC and user listing off, two-factor required for administrators | The login page is what the internet attacks. |
| Uptime and support | an SLA, on-call staff, a status page | none. You are on call, on a VM on a home network with a residential link | A favour for friends, not a product. Section 1.10 lists what that costs. |
| Scaling | elastic plans | none: a fixed RAM and disk budget, checked by the play before it builds anything | Three sites fit; the play refuses a list that does not. |
| Cost | monthly per site | electricity, your time, a free Cloudflare plan, a free Wordfence key, a Resend free plan | |

The pattern in that table is the thesis: **a provider spends money and staff on being forgiving; we spend code and
tests on being strict.** We cannot be forgiving at 3 a.m. when nobody is awake, so every action either has an undo,
or asks you first, or is impossible.

**For the expert:** this thesis has a corollary that took the whole project to see. Strictness has a price in *friction
for you* too, and friction is where mistakes come back in: an operator who must type a site name twice learns to type
it without reading it. The panel's typed-name confirmations are therefore used sparingly (delete, going live, replace,
reset) and not on every button; section 1.7 says exactly where, and why *stop* is still one click.

## 1.5 The architecture, from the hardware up

```
                          Cloudflare (TLS, free WAF, DNS wildcard)
                                   │  tunnel opened OUTBOUND by cloudflared on dellwp
                                   ▼
 ┌───────────────────────────── dellwp (VM 102, 10.8.0.41, 4 GB, 3 cores) ───────────────────────────┐
 │ nginx 127.0.0.1:8080 (public) and :8081 (probe, VM-local)        wp-ctl  ◀── ssh, forced command ──┼── aopenpanel
 │   one server block per site, rate limits, 503 pages                │       (user wpctl, from= the VM)  │   (VM 304, 10.8.0.68)
 │                       │                                            ▼                                   │
 │   ┌───────────── per site: Docker Compose under systemd (wp-<site>.service) ─────────────┐            │
 │   │ php-fpm container: code read-only, uploads writable, hardened php.ini, cap_drop ALL  │            │
 │   │ MariaDB container: own network, own user, not published                               │            │
 │   └───────────────────────────────────────────────────────────────────────────────────────┘            │
 │ /srv/wp  (a separate 40 GB virtual disk, ext4 project quotas)                                         │
 │   sites/<name>/{site.json, html/, db/, ...}  backups/<name>/  intake/  scratch/  archive/  tombstones/│
 └───────────────────────────────────────────────────────────────────────────────────────────────────────┘
        dellwp is a VM on the Dell OptiPlex 3040 running Proxmox VE; the Dell is a WireGuard spoke of the OPNsense box
```

The numbers in the picture are from the repository, not from memory: `dellwp` is VM 102 with `memory: 4096` and
`cores: 3` (`host_vars/delloptiplex3040/vars.yml`), a 25 GB system disk and a second disk mounted at `/srv/wp`;
`aopenpanel` is VM 304 with 2 GB and 2 cores (`host_vars/aopen/vars.yml`) `[verified]`.

What each layer buys, from the bottom:

- **Proxmox on the Dell, a VM for WordPress.** The wall between WordPress and the shop. A flaw in a plugin gives
  an attacker a shell inside a container; breaking out of the container reaches the VM's kernel; breaking out of the
  VM is a hypervisor escape, a different order of difficulty. The shop (`dellapps`, Bloomy) is the other VM on the
  same hardware and a neighbour at the kernel level only.
- **`/srv/wp`, a separate disk.** WordPress data is isolated from the operating system and from the shop. Proxmox can
  size and back up each disk on its own, and a full `/srv/wp` cannot stop the OS from booting.
- **Project quotas.** `tasks/wp_project_quota_common.yml` gives every site two quota projects, one for its files and one
  for its backups, plus projects for intake, scratch and archive. A runaway site fills its own ceiling.
- **A Compose stack per site under systemd.** Two containers (php-fpm and MariaDB), a private network, memory and
  swap limits that are *equal* (so a container cannot quietly use swap to exceed its memory ceiling), `pids_limit`, no
  capabilities, `no-new-privileges`, and a read-only root filesystem for PHP.
- **One nginx, outside Docker.** One place for the rate limits, the "this site is paused" page, and the nginx
  rules that stop PHP from running out of upload folders. It reaches each site's php-fpm on a loopback port
  (`127.0.0.1:<port>:9000`), and the port of a site is assigned once and never reused.
- **A tunnel, not a port.** `cloudflared` on `dellwp` connects outward to Cloudflare. At home no port forwards, no
  dynamic DNS, no certificate for the sites. (The apartment has no router access at all; the design would be
  impossible there any other way `[stated]`.)

### The two small ports, and why there are two

nginx listens on `127.0.0.1:8080` for the tunnel and on `127.0.0.1:8081` for the VM itself. The second is the
**probe port**: it ignores the maintenance flag. The update and recovery tools use it to test a site before they
reopen it. It is reachable only from the VM. This is a good example of a pattern that recurs in the whole system:
*give the tool a door that only the tool can use, instead of teaching the public door about the tool.*

### A site's compose file, with the parts that matter

From `templates/wp/site_compose.yml.j2` (excerpt, `[verified]`):

```yaml
name: wp-{{ wp_s.name }}
services:
  db:
    image: {{ wp_image_db }}
    user: "{{ wp_db_uid }}:{{ wp_db_uid }}"
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: {{ wp_s.db_mem_mb }}m
    memswap_limit: {{ wp_s.db_mem_mb }}m          # equal to mem_limit: no swap for this container
    pids_limit: {{ wp_s.pids_limit }}
    command:
      - --performance-schema=OFF                    # about 100 MB saved per site
      - --innodb-buffer-pool-size={{ wp_s.db_buffer_pool_mb }}M
      - --local-infile=0                            # LOAD DATA LOCAL is a classic exfiltration path
      - --skip-name-resolve
  php:
    read_only: true
    tmpfs: ["/tmp:size=64m,mode=1777"]
    ports: ["127.0.0.1:{{ wp_s.port }}:9000"]       # loopback only: nginx on this VM, nobody else
    volumes:
      - ./html:/var/www/html:ro                      # the code is read-only to PHP
      - ./html/wp-content/uploads:/var/www/html/wp-content/uploads   # the one writable place
      - ./wp-config.php:/var/www/html/wp-config.php:ro
      - {{ wp_mu_dir }}:{{ wp_mu_mount }}:ro         # our must-use plugins, outside the friend's wp-content
```

**If this is new:** read it as a list of *removed abilities*. The database container cannot gain privileges, cannot
use swap, cannot import local files, cannot start more than `pids_limit` processes. The PHP container cannot write
to its own code, so a hacked plugin cannot rewrite WordPress or add a backdoor file next to it; the only folder it can
write is `uploads`, and nginx refuses to execute PHP from there.

**For the expert:** the rule that matters most is the read-only code mount combined with `DISALLOW_FILE_MODS`. Most
WordPress compromises persist by writing a PHP file somewhere under `wp-content`. With code read-only and PHP execution
blocked in `uploads`, the persistence primitive is gone for a compromise that stays inside PHP. What remains is a
compromise of the database (an admin account, injected content) and anything a vulnerable plugin can do in memory.

### What a stopped, on-hold and live site look like to nginx

The state of a site is not a flag the application reads; it is the nginx configuration and the containers' presence.
From `templates/wp/site_nginx.conf.j2` (excerpt):

```nginx
{% if wp_s.state == 'stopped' %}
server {
    listen 127.0.0.1:{{ wp_nginx_listen_port }};
    server_name {{ wp_host }};
    add_header Retry-After 3600 always;
    return 503 "This site is paused.\n";           # nothing is passed to php, no file is served
}
{% else %}
server {
    ...
{% if wp_s.state == 'hold' %}
    allow 127.0.0.1;                                  # on hold: only a request from the VM itself gets in
    deny all;
{% endif %}
```

That is why `hold` is a safe starting state: the site exists and works for you, and the public sees nothing, with no
code path in WordPress that could forget to check.

## 1.6 A site's life: states, the registry, tombstones

### The four states and the rules for moving between them

| State | Containers | Public | How you get there | What it is for |
|---|---|---|---|---|
| `hold` | running | blocked at nginx (only the VM itself) | a new site starts here; `stopped` returns here; `live` can return here | create, import, test, show a friend before opening |
| `live` | running | open | from `hold` only, by typing the site's name | normal operation |
| `stopped` | down | a plain 503 "paused" page | from `hold` or `live`; a final backup is taken first | a friend away, a site being retired |
| `closed` | gone | gone | `delete` (the only way) | a tombstone is all that remains |

The rules are in one pure function, `plan_state`, in `scripts/wp_site_model.py` (`[verified]`):

```python
TRANSITIONS = {"hold": ("live", "stopped"), "live": ("hold", "stopped"),
               "stopped": ("hold",), "closed": ()}

def plan_state(current, target):
    if target not in ("hold", "live", "stopped"):
        raise Refuse("'%s' is not a state a site can be put in ..." % target)
    if current == target:
        return []
    if target not in TRANSITIONS[current]:
        raise Refuse("a %s site cannot go straight to %s: put it on %s first" % (...))
    return (["backup"] if target == "stopped" else []) + ["record", "apply"]
```

Three details are worth seeing. It is **pure**: it reads nothing and changes nothing, so every pair of states is
tested in a table. It returns a **plan** (`backup`, `record`, `apply`), so the caller knows the final backup comes
*before* the stack stops and that a failed backup stops the move. And **`stopped` to `live` is refused**: a site that
has been off comes back on `hold` first, because the thing you want to inspect after a pause is the site, not the
visitors.

### Context only we know: "going live is its own question"

Early in the design you decided that `live` is never a side effect: a new site starts on `hold`, and going live needs
the site's name typed. The old engine let `create` start a site live; the new tool deliberately does not (the
dispatcher's `create` handler says "always starts on hold"). Two lessons sit behind this. First, the old behaviour
was a default someone could forget. Second, a friend's site has real content only after an import and a look; any
moment before that, a public site is a liability. The typed name is the cheapest possible "are you sure" that cannot be
answered by muscle memory, because the answer is different for every site.

### The registry: a site is a file

Before 2026-10-29 a site was an entry in a list in `host_vars/dellwp/vars.yml` (`wp_sites`), and every change meant
editing the repository and re-running a play for the VM. That model has a hidden cost: the repository becomes the
database of tenants. A friend's site name, port and settings were mixed with the platform's code, and creating one
needed a commit.

The registry reverses it. The repository declares the *platform*: the VM, the dispatcher, the tools, the templates.
The *tenants* are records on the VM:

```json
{
  "created": "2026-10-29T15:43:49Z",
  "label": "test two",
  "name": "paneltest1",
  "note": "note",
  "php_mem_mb": 512,
  "port": 9114,
  "quota_id": 1014,
  "schema": 1,
  "state": "hold"
}
```

(`/srv/wp/sites/paneltest1/site.json`, the real record shape from your pasted `show` output `[verified]`.) The
rules for what a record may hold live in `scripts/wp_site_model.py`: a table `EDITABLE` says which keys `set` may change
and their ranges; `FIXED_KEYS` (`name`, `port`, `quota_id`, `state`, `created`) are never edited; `HIDDEN_KEYS` (CPU
count, PHP's function allowlist, buffer pool size) stay at the defaults and are changed only by editing the file by
hand. A record with a key nobody knows is an error, not a warning.

The write is atomic, because a half-written JSON file would make a site invisible:

```python
@staticmethod
def atomic_write(path, rec):
    text = json.dumps(rec, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".site-", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        fh.write(text); fh.flush(); os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)          # a crash leaves the old record or the new one, never half
```

**If this is new:** `os.replace` is the operating system's atomic rename: readers see either the old file or the new
one. The write-to-a-temp-file, flush, fsync, rename sequence is the standard way to update a small file safely.

**For the expert:** there is no database on purpose (design decision 5 in the control handoff: "Stateless app. No
database."). The cost of that choice is that you cannot query across sites cheaply, and consistency between records,
quotas and the real stacks is maintained by procedure (a lock per site, the order of writes, idempotent verbs) rather
than by transactions. The benefit is that every piece of state is a file you can read with `cat`, back up with `rsync`,
and restore with a Proxmox snapshot, and the control plane has no extra moving part that could be down when you need it.

### Tombstones: why a deleted name leaves a file behind

Deleting a site removes its containers, unit, nginx block and data after archiving a final snapshot for 14 days. What
stays is a tombstone: `/srv/wp/tombstones/<name>.<UTC stamp>.json` holding the closed record. Its job is to keep
three numbers from being handed to a new site by accident:

- the **port** (a site's php-fpm listens on `127.0.0.1:<port>`);
- the **quota id** (an ext4 project quota id, which also names the disk quota's accounting);
- the **backup id**, so a new site never inherits an old site's backup directory.

On 2026-10-29 a rule changed: a closed *name* may be reused (a friend leaves and comes back, a test site name is
recycled), but the numbers never are. The dispatcher's name check reads the tombstones too, so `check-name` for a name
whose last tombstone is dated and closed answers "allowed", while `create` allocates a fresh port and quota id from
the registry, skipping every number any record or tombstone ever used. The old engine's closed sites live on as
`wp_closed_floor` in `host_vars/dellwp/vars.yml`, ten entries of name, port and quota id, so the numbers they used stay
reserved even though their records never existed in the registry.

**Context only we know: the first delete.** The very first rehearsal of `delete` showed a bug found only on the real
VM: `wp-site-delete` removed a site's directory, including `site.json`, and wrote no tombstone. For a registry-only
site nothing would then have kept its numbers from being reused. The fix reversed the order: write the tombstone
first, remove the record second, and make `close` idempotent so a delete interrupted between the two halves finishes
when it is repeated. An unrepeated half-close is reported as a *broken registry* for every site until it is repaired,
which is exactly as loud as it should be.

## 1.7 The control panel, end to end

This is the section you asked to be long. It is organised as a walk: why the panel exists, how a request travels,
what each layer refuses, how the pages work, and what a compromise could do.

### 1.7.1 Why a panel at all, and why not just a panel

Running `sudo wp-ctl` by hand works and is safe. The panel exists because the work is *repetitive and error-prone by
hand*: typing site names into twenty commands, remembering which flag a command needs, watching a job's log in a second
terminal, remembering what you did last week. A page that lists sites and offers the right buttons turns recall into
recognition, and recognition is what a tired operator is good at.

But a panel is also the most dangerous kind of thing to add: a web application that can delete sites, reachable
from a browser. The design had to answer one question honestly: **what can the page do if it is wrong or taken over?**
The answer, built layer by layer, is: it can ask for the 18 things its own client allows (of the dispatcher's 21 verbs), one at a time, as itself.

### 1.7.2 The chain, with every refusal named

```
 browser (ThinkPad) ──HTTPS──▶ nginx on aopenpanel ──127.0.0.1:8000──▶ gunicorn ──▶ Flask app
                                                                                       │ ctl.py: its own allowlist
                                                                                       ▼
   ssh client in the same container  ──(key forced to wp-ctl, from= this VM, restrict)──▶ sshd on dellwp
                                                                                       │ sudo -n wp-ctl  (no arguments)
                                                                                       ▼
                                          dispatcher: verb table, typed arguments, audit line, lock per site
                                                                                       ▼
                                                      the tool (wp-site-*), as root, in a unit with limits
```

Seven doors, in order; each is a different party's check, and each is described so you can see what would be left
if the one before it failed.

1. **Network.** The page is reachable only on the WireGuard overlay (`10.8.0.68:443`) and only from the ThinkPad
   (`10.8.0.8`). Four layers must agree, all declared: the hub rule that lets the panel VM reach `dellwp` on tcp 22 only
   (sequence 60), the AOpen host's forward lists, the Dell host's forward list for `vmbr2`, and `dellwp`'s own
   nftables allowance. Section 3.4 explains why four.
2. **TLS and the vhost.** nginx has a default server that does `ssl_reject_handshake on`, so a handshake for any name
   but the panel's is refused before HTTP starts. TLS 1.2 and 1.3 only, HSTS, a 1 MB body limit everywhere except the
   one upload path.
3. **Login.** A password and a six-digit code (RFC 6238), compared in constant time; a wrong login of either kind is
   answered the same way; five wrong logins lock the whole login for 15 minutes, counted in a file under `flock` so all
   gunicorn workers share it; a code works once.
4. **Session and token.** A cookie that is `Secure`, `HttpOnly` and `SameSite=Strict`, three hours long, signed by a
   key only the container can read; every form carries a token tied to the session, checked before anything else.
5. **The panel's own allowlist.** `app/wp_panel/ctl.py` knows the 18 verbs the pages need and refuses a verb, an
   argument or a value the dispatcher would refuse, *before* anything is sent. If a page has a bug that tries to ask
   for a verb it should not, the request never leaves the container.
6. **ssh with a forced command.** The panel's key on `dellwp` is bound by `authorized_keys` to one program,
   from one address, with `restrict`:

   ```
   from="10.8.0.68",restrict,command="sudo -n /usr/local/sbin/wp-ctl" ssh-ed25519 AAAA... wp-panel@aopenpanel
   ```

   `from=` means a stolen key is useless from anywhere else. `restrict` turns off the terminal, forwarding and the
   agent. `command=` means whatever the client asks sshd to run, sshd runs `wp-ctl` instead, with the client's
   request on its standard input. And the sudoers entry allows `wp-ctl` with **no arguments**, so even a client that
   somehow smuggled `--run-job` or a flag would be refused by sudo itself.
7. **The dispatcher.** `wp-ctl` has its own table of verbs and typed arguments, checks every value again (the panel
   is not trusted), takes a lock per site, writes the audit line before it acts, and starts anything slow as a unit
   with limits.

**For the expert:** the layering follows one rule: *each layer assumes the layer before it is already compromised.*
The panel's allowlist (5) is not the security boundary; the dispatcher (7) is. The panel's allowlist exists so that a
*bug* in the panel cannot even attempt something the dispatcher would refuse, which keeps the audit file free of
noise and keeps the pages' behaviour explainable. A control that exists to make the other control's job easier is
worth having when it is cheap, and this one is a table.

### 1.7.3 The dispatcher: one table, typed arguments, five kinds of verb

`scripts/wp_ctl.py` is 839 lines. Its heart is a table. This is an excerpt of the real table (`[verified]`; the argument lists are shortened where marked):

```python
VERBS = {
    "list":         Verb("read",   {}, h_list),
    "show":         Verb("read",   {"site": SITE}, h_show),
    "check-name":   Verb("read",   {"name": Arg("name")}, h_check_name),
    "job-log":      Verb("read",   {"job": Arg("text", rx=JOB_RE),
                                    "offset": Arg("int", required=False, lo=0, hi=2**40, default=0)}, h_job_log),
    "delete":       Verb("job",    {"site": SITE,
                                    "confirm_name":  Arg("text", rx=NAME_RE, same="site"),
                                    "confirm_again": Arg("text", rx=NAME_RE, same="site")}, h_delete),
    "password":     Verb("secret", {"site": SITE, "user": Arg("text", rx=USER_NAME_RE)}, h_password, lock=True),
    "create":       Verb("job",    {"name": Arg("name"), "label": Arg("text", required=False, rx=LABEL_RE)}, h_create),
    "set":          Verb("job",    {"site": SITE, "key": Arg("enum", values=SET_KEYS),
                                    "value": Arg("text", rx=ANY_RE, limit=SET_VALUE_MAX)}, h_set),
    "ensure-state": Verb("job",    {"site": SITE, "state": Arg("enum", values=STATES),
                                    "confirm_name": Arg("text", rx=NAME_RE, same="site", when=("state", "live"))}, h_ensure_state),
    "import-receive": Verb("stream", {"file": Arg("text", rx=IMPORT_FILE_RE)}, h_import_receive),
    "import-run":   Verb("job",    {"site": SITE, "file": Arg("text", rx=IMPORT_FILE_RE), "plan": Arg("flag"),
                                    "replace": Arg("flag"),
                                    "confirm_name": Arg("text", rx=NAME_RE, same="site", needs="replace")}, h_import_run),
    ...
}
```

Read it as a contract. There are 21 verbs in five kinds:

| Kind | What it does | Examples | Why it is its own kind |
|---|---|---|---|
| `read` | answers from the dispatcher itself, not audited unless refused | `list`, `show`, `jobs`, `job-log`, `audit-tail`, `import-report` | cheap and harmless; auditing every refresh of a job page would drown the audit |
| `inline` | a quick tool, waited for | `import-discard` | short enough to answer in the same request |
| `secret` | like inline, but a password follows the request line | `password`, `install` | a password must never be an argument, a log line or an audit field |
| `stream` | the bytes after the request go to the tool | `import-receive` | a 3 GB file cannot be a JSON string |
| `job` | a slow tool, started as a unit; the reply is a job id | `create`, `set`, `ensure-state`, `delete`, `backup`, `import-run`, `import-approve`, `import-reset` | an HTTP request must not wait an hour |

An argument is typed: `site` must be an existing site (checked against the registry, not just a pattern), `text`
must match a pattern, `enum` must be one of a list, `int` must be in a range, `flag` must be a boolean. A few
arguments carry the confirmation logic in their definition: `same="site"` means "must equal the `site` argument"
(the typed name), `needs="replace"` means "required only when `replace` is true", and `when=("state", "live")` means
"required only when the state is `live`". So *going live without the name* is not a rule coded in a handler; it is a
property of the table, tested by a test that checks every confirmation argument of every verb.

The cleaning is written so that a mistake is a `Refuse`, and a `Refuse` is an audited, structured answer:

```python
def clean(self, ctl, key, value, coerce):
    ...
    if not isinstance(value, str):
        raise Refuse("bad_type", "%s must be text" % key)
    if len(value) > (self.limit or MAX_TEXT) or CONTROL_RE.search(value):
        raise Refuse("bad_value", "%s is too long or holds a control character" % key)
    ...
    elif self.kind == "site":
        if not NAME_RE.fullmatch(value) or value not in known_sites(ctl.cfg):
            raise Refuse("unknown_site", "there is no such site")
```

`coerce` is true only when the request came from the command line (everything is a string there) and false for the
JSON path, where a number must be a number and a flag a boolean. That one parameter is why the panel cannot get away
with sending `"true"` where `true` is meant.

### 1.7.4 The audit file: written by the callee, never the caller

The first design decision you accepted for the control plane was that **the dispatcher on `dellwp` owns the audit**,
not the app. A bug or a compromise of the panel cannot edit what the callee wrote. In code (shortened: the error handling around each step is left out):

```python
def audit(ctl, phase, rec, quiet=False):
    line = json.dumps({"ts": ctl.stamp(), "phase": phase, **{k: v for k, v in rec.items() if v is not None}},
                      sort_keys=True, separators=(",", ":"))
    fd = os.open(ctl.cfg["audit_file"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, (line + "\n").encode()); os.close(fd)
    ctl.run([ctl.cfg["logger"], "-t", "wp-ctl-audit", "--", line], ...)      # and the journal, which Alloy ships to Loki
    if not ok and not quiet:
        raise Refuse("audit_unavailable", "the audit file cannot be written, so nothing was done")
```

Four things deserve a name. The file is opened `O_APPEND` and, where the filesystem takes it, `chattr +a` makes it
append-only even for root's casual mistakes (it took on the VM `[verified]` 2026-10-28). Every line is also sent to
the journal, so Loki holds a second copy that `dellwp` cannot rewrite. A line is written **before** the action (the
phase `intent`) and again when the job starts and ends (`started`, `result`), so a crash between those shows as an
intent with no result, which is the signal that something needs looking at. And if the audit file cannot be written,
the request is **refused**, not run: no record, no action.

The real lines from your first click-through show the shape (`[verified]`, from your pasted `audit-tail`):

```json
{"args":{"key":"php_mem_mb","value":"512"},"caller":"panel","phase":"intent","req":"p7eafef11defabf0a2e8c741e7a2aed0dphp_mem_mb","site":"paneltest1","ts":"2026-10-29T15:48:02Z","verb":"set"}
{"caller":"panel","duration_ms":16,"job":"20261029T154802Z-5470a96e","ok":true,"phase":"started","req":"p7eaf...","site":"paneltest1","verb":"set"}
{"caller":"panel","duration_ms":615,"job":"20261029T154802Z-5470a96e","ok":true,"phase":"result","rc":0,"req":"p7eaf...","site":"paneltest1","verb":"set"}
```

`caller` says how the request arrived: `panel` for the ssh key, `cli:<user>` for a person on the VM. A password, a
file's content, a session cookie never appear, because they never reach the dispatcher's argument table in the first
place: the password is the second line of standard input, written to a file in a tmpfs and scrubbed from any output.

### 1.7.5 Jobs, the lock, and the request id

A job is a directory under `/var/lib/wp-ctl/jobs/<id>/`: `spec.json` (the request), `log`, `exit`. The unit that runs
it is the same program started again with `--run-job <id>`. The excerpt shows why it is robust:

```python
def run_job(ctl, argv):
    jd = ctl.state / "jobs" / argv[0]
    spec = json.loads((jd / "spec.json").read_text())
    if (jd / "exit").exists():
        return 0                                    # a job runs once, whatever restarts the unit
    lock = take_lock(ctl, spec["site"])
    ...
    rc = ctl.run(spec["argv"], stdin=DEVNULL, stdout=log, stderr=STDOUT, timeout=spec["timeout_s"], ...).returncode
    ...
    tmp = jd / "exit.tmp"; tmp.write_text("%d\n" % rc); os.replace(tmp, jd / "exit")     # atomic, like the registry
    audit(ctl, "result", dict(rec, ok=rc == 0, rc=rc, duration_ms=millis(t0)), quiet=True)
```

Three safety properties live in those lines. *Run once:* the presence of `exit` means the job is done, so restarting
the unit cannot run a `delete` twice. *One at a time per site:* `take_lock` is an exclusive `flock` on a per-site file;
a second verb on the same site is refused as `busy`. *A bounded run:* the unit has a memory cap, a task cap and a
runtime limit from `wp_ctl_limits`, and a timeout is recorded as exit 124 in the log, not as silence.

A job's state is derived, never stored in a way that can lie: `done` if `exit` exists, `running` while the clock is
inside the timeout (plus a grace), otherwise `lost`: a unit that died without writing an exit code. That third
state is the reason the Jobs page has a red "lost" badge.

**The request id.** Every action form carries a hidden `req` made when the page was drawn
(`"p" + secrets.token_hex(16)`, and for forms with several buttons the key or state appended). The dispatcher's
`start_job` is one step under a lock on the job table:

```python
if rec.get("req"):
    old, spec = find_job(ctl, rec["req"])
    if old:
        if (spec.get("verb"), spec.get("site"), spec.get("args")) != (verb_name, site, rec.get("args")):
            raise Refuse("req_reused", "the request id %s started another job (%s)" % (rec["req"], old))
        return {"ok": True, "job": old, "duplicate": True}
```

So a double click, a browser "resubmit", a retry after a network blip returns the same job. A reused id for a different
request is refused loudly. This is the one idea in the control plane that came from outside the homelab: you asked,
while designing batch 1, to compare the dispatcher with **AWS Lambda**, and the comparison produced two additions
(`[stated]` in the handoff): idempotent requests keyed by a request id, and a table of per-verb limits
(`wp_ctl_limits`: time, memory, tasks). Lambda's "invocation with a request id" and "per-function limits" are the
shape; the audit and the typed-name confirmations are ours.

### 1.7.6 The pages, and what each one asks of the dispatcher

| Page | Verbs it uses | Typed confirmation | Notes |
|---|---|---|---|
| Sites (`/sites`) | `list` | | a table with state badges; the first thing you see after login |
| New site (`/new-site`) | `create` | | name and optional label; always starts on `hold`; no WordPress yet |
| Site (`/sites/<name>`) | `show`; forms for `set`, `ensure-state`, `delete` | `live`: the name; `delete`: step one | settings are twelve small forms, one key each |
| Install WordPress | `install` | | title, administrator, email, password twice; refuses a site that already has WordPress |
| Set a password | `password` | | the password is the second line of input; never rendered, logged or audited |
| Delete | `delete` | the name, twice, on two pages | only a stopped site; the second page shows what will be removed |
| Import (`/import`) | `import-receive` (upload), `import-run`, `import-discard` | replace: the name | upload is a raw `PUT`; plan, run and replace in one form |
| Import report (`/sites/<n>/import`) | `import-report`, `import-approve`, `import-reset` | reset: the name | approve takes an optional reject list and an "accept the failures" box |
| Jobs / Job | `jobs`, `job-status`, `job-log` | | a running job reloads itself every 3 seconds |
| Audit | `audit-tail` | | the last lines of the audit file |

**Where typed names are and are not used, and why.** *Going live*, *delete*, *replace*, and *reset* ask for the
site's name. *Stopped* does not (it is one click, though it takes a final backup and gives visitors a 503); *hold*
does not; a setting does not. The rule of thumb: **ask when the action cannot be undone from the page, or when it
makes something public.** A stopped site is a button away from `hold`; a deleted site is an archive for 14 days and
then gone. Whether `stopped` should also ask is an open question in the backlog (section 1.12), not a decision.

**One flow in detail: delete.** The site page shows the Delete section only for a stopped site; otherwise it says "move
it to stopped first". Step one is a form that asks for the name. The page *checks it against the site's name itself*
and then renders a confirmation page that shows the label, the note, WordPress core version, disk use and last backup,
with a second field. Step two submits both entries; the client refuses unless both equal the site; the dispatcher
checks `same="site"` on both again; the tool prompts for the name and the dispatcher answers the prompt with the second
entry. Four checks of the same string in four places, each by a different party. It looks redundant and it is the
point: the panel's check keeps the page honest, the client's check keeps the request clean, the dispatcher's check is
the one that cannot be bypassed, and the tool's prompt is the one that protects a person typing at a terminal.

### 1.7.7 The login, the session and the security headers

The login is a password plus a TOTP code. Both are always checked, so the response and its time do not reveal which
half was wrong:

```python
def check_credentials(settings, password, code, throttle, clock):
    wait = throttle.locked_for()
    if wait:
        return "locked", wait
    pw = password if isinstance(password, str) and len(password) <= MAX_PASSWORD else ""
    pw_ok = check_password_hash(settings.password_hash, pw) and pw != ""
    step = totp_step(settings.totp_secret, code, clock())
    if pw_ok and step is not None and throttle.accept_step(step):
        return "ok", 0
    throttle.record_failure()
    return "wrong", 0
```

`totp_step` looks one step back and one forward for clock drift and compares **every** candidate in constant time
(no early exit on a match). `throttle.accept_step` refuses a step that was already used, so a code seen over a shoulder
cannot be replayed within its 30 seconds. The lockout state (recent failures, the lock, the last code step) is one
small file under `flock` in a tmpfs inside the container: a restart clears it, and an attacker cannot restart the
container.

The whole page set carries one set of headers, with one documented exception:

```python
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store", "Cross-Origin-Opener-Policy": "same-origin",
}
SCRIPT_PAGES = {"import_hub": "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; ..."}
```

`default-src 'none'` is the strictest starting point: nothing loads except the one stylesheet the policy names. Only
the import page may run a script, and only a file served by the app itself (`script-src 'self'`); a test asserts that
exactly one `<script src="/static/upload.js" defer>` tag exists and that no page has an inline script or style.

### 1.7.8 What a compromise of each part can do

| If this is compromised | It can | It cannot |
|---|---|---|
| The browser (the ThinkPad) | do anything the logged-in operator can, until the session ends | bypass the typed names (a script can type them; the audit still records every action as `panel`) |
| The panel container on `aopenpanel` | send the dispatcher any of the 18 verbs its client allows (and, by editing the client in memory, any of the 21) | get a shell on `dellwp`, read the vault, change the tools, read or edit the audit |
| The panel's ssh key (stolen from the VM) | be used from `10.8.0.68` only, to send the same requests | be used from elsewhere (`from=`), open a terminal, forward ports, run any other program (`command=`, `restrict`) |
| The dispatcher / `dellwp` | everything on `dellwp`, as root | reach the shop VM, the hypervisor or any other host (the hub allows `dellwp` nothing it should not) |
| The hub / the overlay | reach what the rules allow | the panel's session, which is encrypted end to end between the ThinkPad and the VM |

The honest summary is the sentence from the design: *a compromised container can send only allowlisted verbs; it cannot
get a shell and holds no vault password.* The one thing a compromised panel can do that is genuinely bad is use the
allowlisted verbs maliciously (delete a site, open a site to the public). The defences against that are the typed names
(a script has to know them, and the names are in the page it fetches, so this is a speed bump, not a wall), the
final backup before a stop, the 14-day archive before a delete, and the audit that records every one of those with
`caller: panel`.

**For the expert:** the weakest property is the one the design states openly: the panel is *semi-trusted*, and a
full takeover of it equals having the operator's session. The mitigation is the usual one for a single-operator tool:
keep the panel unreachable from anywhere but one machine, keep the session short, keep the lockout shared, keep the
actions reversible for as long as possible. A second factor *per destructive action* (a fresh TOTP for delete) would
raise the bar further; it is not built and is on the list of ideas in the backlog.

## 1.8 Import, backup and restore

These three belong together. Import is the riskiest thing the service does; backup is what makes it survivable; restore
is the proof that the backup was real. Each is described as a pipeline with the reason for each step, then what is
proven and what is not.

### 1.8.1 Why a `.wpress` is untrusted input, in one paragraph

A `.wpress` file contains a database dump, the `wp-content` tree, and a `package.json`. A provider's migration plugin
loads it as is. We cannot, because **the database is attacker-controlled input to code that will run.** WordPress
stores which plugins and which theme are active in the `options` table and then `include`s those files at every
request. A hostile (or merely damaged) database can name a file you did not intend, and a plugin you did not choose can
bring its own `mu-plugins` folder, whose files WordPress loads with no activation at all. Importing a stranger's site
is therefore *running a stranger's code*, and the whole pipeline is arranged so that **no line of code from the archive
runs until a human has read a report and said yes**.

**If this is new:** think of the file as a sealed box that says "this is a bookshop". You would not plug a box a
stranger handed you into your network before looking inside it with something that cannot be fooled by the box's
label. The importer is that "something": a reader that unpacks only what a policy allows, a plan that says what it
will do, a scan, and a pause.

### 1.8.2 The pipeline, step by step, with the reason for each

The tool is `scripts/wp_site_import.py` (1,308 lines), reached through the dispatcher's verbs `import-receive`,
`import-run`, `import-report`, `import-approve`, `import-reset` and `import-discard`. In the order they happen:

1. **Receive.** The file arrives as a stream into a root-only *intake* directory with its own quota (3 GB). The tool
   writes to a hidden `.recv-<name>` file and renames it into place only when the stream ended, never past the free
   space of the quota, never over a file that is already there. It prints one line: `received <sha256> <bytes> <name>`.
   The panel compares the count and hash with what it sent and refuses a mismatch (section 1.8.4).
2. **Plan** (`--plan`). The reader (`scripts/wp_wpress.py`, 299 lines) judges the archive *without writing*: a core newer
   than ours, a plugin that needs a newer PHP, an unsafe path (`..`, absolute, a link), a multisite, a truncated file
   are refusals. The plan lists what would be unpacked, reinstalled, and dropped, and why. Nothing on disk changes.
3. **Unpack with a policy**, not everything. Only the database, `package.json`, uploads, must-use plugins, languages,
   and an active plugin or theme that wordpress.org does *not* offer are unpacked, and the last two only into a work
   directory (`intake/work-<site>`). Dropped by design: the migration plugin itself, caches, LiteSpeed data,
   half-finished updates (`upgrade/`), the migration plugin's own backups, PHP files under `uploads`, hidden files.
4. **Load the SQL** with the migration plugin's table-prefix placeholder swapped for the site's, as the site's own
   database user. Never as root.
5. **Reset the options that name code** (`active_plugins`, `template`, `stylesheet`) to what `package.json` says, and
   drop the friend's cached *update state*. (The cached state was a real bug: an archive carried a record saying
   Elementor had an update waiting; WordPress then said "plugin already installed" and installed nothing, with exit code
   0. Found on the real VM, section 1.11.)
6. **Reinstall from wordpress.org**, at the archive's version when it is still offered. Anything not on wordpress.org
   is **held back** in the work directory; nothing of it has run.
7. **WP-CLI steps** with `--skip-themes` and, unless a step truly needs them, `--skip-plugins`: database upgrade,
   search-replace of the friend's old address in six forms, Elementor's URL replace and CSS flush, transients, rewrite
   rules, "discourage search engines".
8. **Scan.** Pattern hits (`eval`, `base64_decode`, shell calls), `verify-checksums` of core and plugins against
   wordpress.org, ClamAV in a pinned, network-less container, and the vulnerability feed.
9. **Report, first backup, notification.** The site stays on `hold`. A notification goes to your phone through the
   independent ntfy server, with an **Approve** button only if the import is clean (no failed step, checksums ok, no
   ClamAV find, no suspicious pattern) and a **Deny** button always.
10. **Your decision.** *Approve* copies the held code in (root-owned, read-only), deletes the intake file and the work
    directory. *Deny* or `import-reset` undoes the run from a manifest of what it wrote plus a snapshot of `wp-content`
    taken before the load.

The reason a manifest and a tree snapshot are used instead of "just delete the site and start over": **a name is spent
once** (its port and quota number are never reused), and a friend's real site is the one thing you must never lose
halfway. A failed import must be undoable in place.

### 1.8.3 Replace: the one operation that throws data away on purpose

A friend sends a newer file for a site that already has data. `--replace` takes a protected snapshot tagged
`pre-import` (never pruned while the import waits), wipes the database and `wp-content` in place, and imports. A deny
puts the old site back from that snapshot through `wp-site-recover`; an approve changes the tag so normal retention ages
it out. In the panel, *Replace* is a radio button with a text box beside it: you type the site's name, the client
refuses a replace without it, the dispatcher's argument has `needs="replace"` and `same="site"`, and the tool itself
asks again at its prompt. Nowhere else in the system is a name asked for so often, because nowhere else can one
mistake wipe a friend's live site.

### 1.8.4 The upload, as one flow, and why it is a raw `PUT`

The upload is where the most interesting engineering of the control app lives, and where the one real design reversal
happened.

**The constraint.** A `.wpress` can be 3 GB. The panel runs in a 512 MB container on a VM with 2 GB. The bytes must go
from your browser to `dellwp`'s intake **without ever being held** by nginx, by the app, or by the VM's disk. And the
transfer must be provably the same file at both ends.

**The first idea: an HTML form with `multipart/form-data`.** It works without JavaScript, which fit the policy of a
page with `default-src 'none'`. A streaming parser from Werkzeug would split the form's parts as the body arrives. I
stress-tested that parser before building on it: 95,839 random ways of splitting the same body into chunks, with
payloads full of the byte sequences that look like boundaries (`\r\n--`, partial boundaries, lone `\r`). **847 of them
came out corrupted** (a stray byte, a missing byte at a chunk edge), all in cases that a real file with a
chunk edge at the right place could reach. A corrupted `.wpress` is not a crash; it is a silently different file that
the importer might unpack. A parser I could not trust was not going on the path of a 3 GB file.

**The decision: a raw `PUT`.** The file *is* the request body. There is no multipart envelope to parse, so there is
nothing to get wrong, and `Content-Length` tells both ends how many bytes to expect. The price is a small script,
because an HTML form cannot send a raw body: one file, `app/wp_panel/static/upload.js`, which builds no HTML (every
message is set with `textContent`), uses `XMLHttpRequest` for the progress events, sends the session's token in an
`X-CSRF` header (a header a page on another origin cannot set), and puts the file itself as the body (`xhr.send(file)`).
The CSP exception for that one page is `script-src 'self'; connect-src 'self'`, and a test asserts the page has exactly
one script tag, with `src`, and no inline code.

**The path of the bytes**, each hop streaming:

```
 browser XHR  ──PUT /import/files/<name>──▶  nginx (proxy_request_buffering off, body limit = the intake quota)
        ──▶ gunicorn thread ──▶ upload() view: reads 64 KiB at a time from request.stream
        ──▶ Ctl.stream(): writes the request line, then each chunk, to the stdin of an ssh process
        ──▶ sshd on dellwp ──▶ sudo wp-ctl ──▶ h_import_receive ──▶ wp-site-import receive: .recv-<name> → rename
        ◀── one line: "received <sha256> <bytes> <name>"   (compared with what the panel counted and hashed on the way)
```

Memory and disk on the VM: none, apart from one 64 KiB chunk in flight per upload. That is a claim, and it was proven
three ways: a test that sends 300 MB through the real gunicorn configuration to a WSGI app that reads in 64 KiB pieces
and reports its own peak memory (it stays under 120 MB and every byte arrives); the offline test of the whole chain
with a stand-in `ssh`; and **your run on 2026-10-30**, a 300 MB generated file through the real nginx, the real app,
ssh and `wp-ctl`, checked by count and hash and removed again, with the container's memory peak and nginx's body
directory checked afterwards (`scripts/wp_panel_rehearsal_aopenpanel.sh --upload-mb 300` printed "All checks passed").

**The truncation problem, and the honest limit.** `wp-site-import receive` cannot tell a stream that was *cut off* from
a stream that *finished*: both end with end-of-input, and the file is renamed into place. So if your browser dies
half-way, `dellwp` may keep a short file that looks finished. The panel's mitigation is best effort: when its own
read of the body ends before `Content-Length`, it waits two seconds (to let `dellwp` finish the short file) and asks
the dispatcher to discard it; and the *importer's own check* (the reader refuses a truncated archive) is the second
line. The proper fix is in the backlog: an optional `bytes` argument on `import-receive` so the tool never renames a
file whose length differs. It is listed first in the backlog because it is the one known place where a failure could
produce a wrong state with no alarm.

### 1.8.5 Backups: what is taken, how often, how long it is kept

There are two independent layers, and the difference between them is the point.

| Layer | What | Where | How often | Kept |
|---|---|---|---|---|
| **Site snapshots** | each site's database dump plus its `wp-content`, hard-linked against the last snapshot | `/srv/wp/backups/<site>/` on `dellwp` (a quota of its own) | nightly (timer `wp-backup`) | 7 daily, 3 weekly; snapshots tagged `pre-update`, `pre-import`, `final` follow their own rules |
| **VM backups** | the whole `dellwp` VM, encrypted client-side | Proxmox Backup Server on the ASUS, replicated to the AOpen | nightly (04:00, job `dell-vms-nightly`) | by the PBS prune job (the VM job itself never prunes) |

A site snapshot protects against *the site's own mistakes* (a bad update, a bad import, a friend deleting a page): it
is fast, per site, and restorable in place with `wp-site-recover`. A VM backup protects against *the machine's*
disaster (the Dell dies, the disk corrupts): it brings back every site, the dispatcher, the registry, the timers, in
one piece. Neither replaces the other; a site snapshot lives on the same VM as the site and would die with it.

The site snapshot is written as a hidden folder and renamed into place only when the dump has passed `gzip -t`, has
its completion marker, and contains the `wp_options` table, so a half-written backup never looks like a backup. An
hourly check alerts if a site's newest backup is older than 30 hours. An *empty* site (created, waiting for an
import) has nothing to back up, and the jobs skip it quietly, because an alert every night for a site with no data
teaches you to ignore alerts; a site that once had real backups and lost its tables is the opposite case and still
alerts.

**Context only we know: `stopped` takes a backup of its own.** When a site is moved to `stopped`, the plan's first step
is a snapshot tagged `final`. In your own click-through on 2026-10-29 the job log of `paneltest1`'s stop shows it taking
the snapshot, stopping the stack, and the next `delete` archived *that* snapshot (`/srv/wp/archive/paneltest1-20261029T155231Z`,
kept until 2026-11-12, about 15 MB) `[verified]`. Stop-then-delete is therefore a two-snapshot trail, which is why the
delete page insists on a stopped site: the tool refuses a site that is not stopped, because there would be no final
snapshot to archive.

### 1.8.6 Restore, and why the drills matter more than the backups

A backup that was never restored is a hope. There are four different restore proofs in the system, and each tests a
different claim.

| Proof | Claim | What it does | How often |
|---|---|---|---|
| **Site restore drill** (`wp_restore_drill.sh.j2`) | "this site's snapshot restores and WordPress boots" | restores one random site with a good backup into a scratch copy with a throwaway database and boots it with WP-CLI | monthly, the 1st at 05:30 |
| **`wp-site-recover`** in real use | "I can put a site back after a bad update or a denied import" | replaces database and files from a snapshot in place, with an undo, and proves WordPress boots before opening | on demand and in every rehearsal |
| **VM restore test** (`scripts/proxmox_restore_test.sh`) | "the PBS backup of a VM restores and the VM runs" | restores a VM into a spare VM without a network card, checks the guest agent and the containers, destroys it | on demand; 161 s for the shop on 2026-10-23 `[verified]` |
| **AOpen VM restore drill** | "the *replica* restores, not just the original" | restores the newest replicated backup of `dellapps` (Tuesday) and `dellwp` (Wednesday) into a spare VM from the AOpen's PBS, boots with no network, checks through the guest agent, destroys it | weekly; `dellwp` took 546 to 573 seconds, `dellapps` 288 to 302 `[verified]` |

The drills have a rule about alarms: a drill that has not succeeded for 10 days alerts, and a weekly summary goes out
on Sunday. They are alerts about *absence of proof*, which is a different kind of alert from "something broke": the
system is allowed to be quiet when it is working, and loud when it has stopped proving that it works.

**For the expert:** the restored VM's units that fail *because it has no network* are listed per drill in
`expected_failed` (`cloud-final`, `cloud-init-main`, `systemd-networkd-wait-online`, and for `dellwp`
`systemd-quotacheck@srv-wp`, the last because the backup is crash-consistent and quotacheck cannot run on a filesystem
with the quota feature). Anything else failing fails the drill. This is the opposite of ignoring a failure: it is
*enumerating* the expected failures so that a new one is a signal.

### 1.8.7 Delete: the three-step safety, and the names that are never reused

Delete is `ensure-state stopped` (final snapshot) then `delete`, which archives that snapshot for 14 days under
`/srv/wp/archive/<name>-<stamp>`, writes the tombstone, and removes the containers, unit, timers, nginx block,
directory and backups. A daily job (`wp-archive-purge`) removes archives whose time is up. The panel's delete asks
for the name twice on two pages (section 1.7.6). What is *deliberately* absent is an "undelete": within the 14 days you
can restore from the archive by hand; after that, only a VM-level backup would have the site.

## 1.9 The security model, layer by layer

Security here is not one mechanism; it is the *order* in which things are refused. This section walks an attack from the
outside in, lists what each layer stops, and then lists what no layer stops. It extends the threats table of the older
explainer with the control plane.

### 1.9.1 The layers, outside in

| # | Layer | What it stops | What it does not stop |
|---|---|---|---|
| 1 | **Cloudflare** (free plan): TLS, WAF managed rules, DNS | commodity scanning, known exploit patterns, direct hits on a home IP (there is none to hit) | a targeted attack with a novel payload; anything that passes the WAF |
| 2 | **No inbound port at home**: the tunnel is outbound | port scans, direct attacks on the VM from the internet | an attack that arrives *through* the tunnel as ordinary requests |
| 3 | **nginx rules**: per-site server blocks, the login rate limit, 403 on `?author=N` and on PHP in `uploads`, 503 when stopped | brute-forcing `wp-login.php`, user enumeration, executing uploaded PHP | a slow, distributed guess of a weak password |
| 4 | **PHP-level throttle and generic login messages** (must-use plugin) | even a correct password past a failure limit; learning which users exist from the error text | a leaked password used once |
| 5 | **Two-factor, required for administrators** (the Two Factor plugin, pinned by version and zip SHA-256) | a stolen or guessed password alone | a stolen phone *and* password, or a stolen session cookie |
| 6 | **No file mods; code read-only; plugins only from wordpress.org by slug** | persisting by installing or editing code through the admin | a vulnerability in an installed plugin, exploited in memory |
| 7 | **Container hardening**: `cap_drop ALL`, `no-new-privileges`, read-only root for PHP, memory and PID limits, a network of two containers | a compromise growing beyond its container, fork bombs, memory exhaustion of the VM | a kernel exploit (the container shares the VM's kernel) |
| 8 | **Egress rules on the VM**: containers reach the overlay for DNS only; the VM cannot send SMTP | a hacked site sending spam from your address, or calling home to arbitrary hosts | data sent through a permitted channel (DNS tunnelling is theoretically possible) |
| 9 | **The VM boundary**: `dellwp` is not the shop; the hub allows it only what its rules list | a compromise reaching Bloomy, the HP, the hypervisor | a hypervisor escape |
| 10 | **Import policy**: untrusted archive, held code, option reset, skip-plugins, scans, human approval | a hostile `.wpress` running code before you decide | a *clean-looking* malicious plugin that you approve |
| 11 | **Phone approvals** with signed, one-time, report-bound tokens; a phone can approve only a clean import or deny | forged approvals; a lock-screen tap approving something unsafe | you approving a clean-looking bad import |
| 12 | **The control plane** (section 1.7): overlay-only page, login with code, forced ssh key, dispatcher allowlist, audit written by the callee | a stolen panel session doing anything beyond the 18 verbs; a compromised panel editing its own audit | the operator's own session used maliciously |

### 1.9.2 Walks: five attacks, followed through

**A friend's password is stolen (phished or reused).** Layer 3's rate limit and layer 4's throttle slow guessing;
layer 5 stops a login without the authenticator code. If the thief also has the phone or a session cookie, they are an
administrator of *that one site*. They cannot add code (layer 6), cannot write PHP to `uploads` and run it (layer 3,
403), cannot reach another site's database (layer 7's network), cannot send mail (layer 8). They can read and change
that site's content, and they can install nothing. The cost to you is a restore of that one site from a snapshot, which
you have drilled.

**A friend sends a poisoned `.wpress`.** It lands in a root-only intake with a quota (the file cannot fill the OS
disk). The reader refuses unsafe paths and truncations at the plan. The SQL loads as the site's own database user
into an empty database. The options that name code are reset before WordPress runs, and every early WP-CLI call skips
plugins and themes. Non-wordpress.org code is held. The scan runs. A known-vulnerable plugin removes the Approve
button on your phone. You read the report. If you approve, you approved that code; if you deny, the manifest undoes it.
The residual risk is the one in the "does not stop" column: a plugin that is clean to every scanner and still malicious.

**A plugin has a zero-day.** The vulnerability feed only knows *published* vulnerabilities, so this is not detected.
What limits the damage is layer 6 (no persistence by file write), layer 7 (the container), and layer 8 (no spam, no
arbitrary callbacks). The daily feed check will name the CVE after it is published; for a few days a site may be
exposed. Cloudflare's WAF may block the exploit pattern; do not count on it.

**Someone steals the panel's ssh key from `aopenpanel`.** `from="10.8.0.68"` means the key works only from that
address. An attacker on the panel VM could run `ssh` with it: they would get the dispatcher's JSON interface and
nothing else (`command=`, `restrict`, sudo with no arguments). They could delete a site if they also knew its name (the
name is in the same page they could read) and the 21-verb table is open to them. The audit would show `caller: panel`
for every action. The defence is detection and recovery, not prevention: the final snapshot, the 14-day archive, the
audit, and the fact that the *only* host that can reach the key's `from=` is the VM you control.

**The Dell dies.** The shop and every WordPress site go down. Cloudflare serves its error page. The plan is the
Proxmox VM backups on the ASUS and, if the ASUS is also gone, the replica on the AOpen; the AOpen's weekly restore
drill proves the replica boots, and takes about nine and a half minutes for `dellwp` `[verified]`. A rebuild of the
Dell is a Proxmox install plus `ansible-playbook` runs, then a VM restore; the time is a drill number, not a promise.
The honest line: no redundancy at home, one disk, one residential link, and a tested recovery.

### 1.9.3 What we do not defend against, on purpose

- A determined attacker with a **WordPress core zero-day** before the image updates. (The pinned image is updated by
  Renovate pull requests; you review and merge them.)
- **Losing both sites at once.** The Dell and the AOpen are in the apartment (`wg_site: apartment` in the inventory
  `[verified]`); the ASUS, which holds the primary PBS copy, is at home. A fire at one place leaves a copy at the other.
  Losing both buildings leaves only what is in Cloudflare R2, which holds the *services'* backups (Gitea, Nextcloud,
  Vaultwarden) but not the WordPress VM `[inferred: no WordPress leg to R2 is declared in this repository]`.
- **A deliberately hostile friend.** The design assumes a mistake, not an enemy, and says so.
- **Legal and compliance duties.** A site that stores other people's personal data and takes payments has obligations
  that this setup does not meet and does not claim to meet.
- **An attacker with control of your ThinkPad.** It is the one machine that can reach the panel and runs the
  Ansible runs; it is also the operator's own, so a compromise of it is a compromise of everything. Disk encryption, a
  locked screen and a password manager are the defence, and they are outside this repository.

### 1.9.4 Secrets, in one place

| Secret | Where it lives | Who can read it | Rotation |
|---|---|---|---|
| Each site's database password and WordPress salts | **derived** on `dellwp` from one master secret, the site name and a revision number (`secret_rev`) | root on `dellwp` | raising a site's `secret_rev` changes only that site's |
| The master secret | the Ansible vault (`dellwp_wp_master_secret`) and `dellwp` | you, `dellwp` | rarely; changing it changes every site's derived secrets |
| The panel's login | on `aopenpanel`: a scrypt hash, a TOTP secret, a session key, each a 0400 file owned by the app's uid | the container | re-run the credentials helper and the app play |
| The panel's ssh key | `aopenpanel` (private, 0400, app uid); `dellwp` (public, in `authorized_keys`) | the container | `scripts/wp_panel_credentials.py` makes a new pair |
| Cloudflare tunnel token, Resend key, Wordfence key | encrypted vault files for `dellwp` `[inferred from the per-host vault convention]` | root on `dellwp` | per provider |
| The Proxmox Backup Server encryption key | the vault, and a saved copy that `scripts/pbs_key_check.sh` compares with the key on the Dell; a second offline copy was an open item for you at the last notes | you | never lightly: backups made with it need it |

The control plane's cardinal rule, written in the design: **a control plane holds the narrowest key that can do its job,
not the vault.** The panel's container has no vault password, no Ansible, no root; it has one key that can say 21
words to one program on one host. That is why `dellwp` derives the sites' secrets itself rather than being told them.

## 1.10 Capacity, cost and limits

A hosting service is a promise about resources. This section writes the promise down as arithmetic, shows the
evidence for and against it, and says when to change it. Most of the figures come from the repository's variables, so
they can be recomputed; the places where a number is a guess are marked.

### 1.10.1 The budget, as the play enforces it

The sites play refuses to build anything if the sum of the sites' limits plus a reserve does not fit the machine. The
rule has two parts, both in `host_vars/dellwp/vars.yml`:

```yaml
wp_vm_ram_reserve_mb: 1536     # RAM for the OS, nginx, cloudflared, Alloy, node_exporter and the page cache
wp_data_reserve_mb: 9216       # disk for intake (3072) + scratch (3072) + archive (3072); backups have quotas of their own
wp_site_defaults:
  php_mem_mb: 512
  db_mem_mb: 256
  quota_mb: 3072               # a site's own files
  backup_quota_mb: 3072        # its backups, a quota of their own
```

For three sites at the defaults:

| | Per site | Three sites | Plus reserve | Total | The machine |
|---|---|---|---|---|---|
| RAM | 512 (PHP) + 256 (DB) = 768 MB | 2,304 MB | 1,536 MB | **3,840 MB** | 4,096 MB (`memory: 4096`) |
| Disk (the data disk, `/srv/wp`) | 3,072 + 3,072 = 6,144 MB | 18,432 MB | 9,216 MB | **27,648 MB** | about 40 GB (a disk only grows) |

So "three sites at most" is not a feeling; it is `3 x 768 + 1536 = 3840 <= 4096`, the smallest round VM size the
play accepts for three sites. A fourth site would need 768 MB more, which is 4,608 MB, so the VM would be set to
5,120 MB; the play would say so by refusing until the VM's `memory:` and the list agree. The disk has room for a
fourth (`4 x 6,144 + 9,216 = 33,792 MB`) before the 40 GB disk is full.

**If this is new:** a memory *limit* on a container is a ceiling the kernel enforces: go over it and the kernel kills
a process in that container (an "out-of-memory kill"). Setting the swap limit equal to the memory limit means the
container cannot borrow disk as memory to escape the ceiling. The budget above is therefore a *worst case*: every
site using its whole ceiling at the same moment. Real use is lower and rarely coincident, which is why the budget is
conservative and why the machine can run well under it.

### 1.10.2 The evidence, and the thing the numbers do not yet settle

There is a real measurement, and it points the wrong way, and it deserves its own paragraph because the decision it
informs has not been made.

On 2026-10-28 the HP's Prometheus was queried for `dellwp` over 30 days, which was only the VM's first week, the build
out and the rehearsals `[verified]`: the **least memory available was 0 MB**, the **most used was 6,023 MB of 6,144 MB**
(the VM then had 6 GB), and the **most swap used was 1,497 MB**. The VM was then cut to 4 GB on the same day, on the
reasoning that three sites at the defaults fit in 3,840 MB. With no site live, 3,431 MB was available `[verified]`.

Those two facts are compatible only if the peak was caused by something that is *not* three sites at their ceilings.
The handoff records the likely cause as a guess (`[inferred]`): the **ClamAV scan** and the **vulnerability-feed
update** each have a 2,048 MB limit, and a rehearsal can run beside them. Nothing has traced the actual job. So the
status, said plainly: **4 GB is applied; it is not shown to be enough.** The recommendation recorded is 5,120 MB unless
the peak is traced to a rehearsal and the scan and the feed are kept apart from each other and from the sites. The
existing `SwapThrashing` alert (swap above 50% for ten minutes) would show it in normal use.

> **The decision still open, and how to close it with evidence rather than opinion.** Run the three real sites for a
> week, with the scan and feed on their normal schedule, and read the same two queries again
> (`min_over_time(node_memory_MemAvailable_bytes{host="dellwp"}[7d])` and the swap equivalent). If the minimum
> available stays above about 500 MB and swap stays near zero, 4 GB is right and the question is closed. If not, trace
> which job ran at the minimum (the Loki logs and the systemd timer history show it) before spending 1 GB.

> **Closed, 2026-11-19 `[stated]`:** 4 GB was enough, because no more than three WordPress sites ran at once. The VM was later reduced to 3 GB for two sites (2026-11-13).

### 1.10.3 What the control plane adds to the budget

The panel is on another machine (the AOpen), so it adds **nothing to `dellwp`'s RAM**. What it adds to `dellwp` is
small and bounded:

- **The dispatcher's units.** A job runs as a transient unit with a memory cap from `wp_ctl_limits`
  (`default: { timeout_s: 120, memory_mb: 512, tasks: 256 }`, `import-run: memory_mb: 1024`), and with
  `MemorySwapMax=0`. A job that wants more is killed and its log says so (exit 124 for the time limit).
- **State on disk.** Job directories (a request, a log, an exit code) are kept `job_keep_days` and pruned by every
  request; the audit file is append-only and grows by one JSON line per phase.
- **The intake.** A 3 GB quota. An upload larger than that is refused by the panel (413) and by nginx
  (`client_max_body_size 3072m`, built from `wp_intake_quota_mb`).

On the AOpen the panel is a 512 MB container (`--memory=512m --memory-swap=512m`) on a 2 GB VM. In your rehearsal the
container sat at **83.6 MiB of 512 MiB** `[verified]`, and a 300 MB upload did not move it far, because the file is
never held. The AOpen host declares 12,800 MB of VM memory out of 15 GiB (its four VMs, the panel's 2 GB included, plus the restore drill's 2 GB spare); that was
accepted on 2026-10-29 with the instruction to check `free -m` afterwards `[stated]`, and the check is not recorded.

**The limits table is a proposal.** The numbers in `wp_ctl_limits` (1,800 s for `create` and `set`, 5,400 s for
`ensure-state`, 600 s for `install`, 14,400 s for `import-receive` and `import-run`) were chosen, not measured. The
handoff says so and says what to do: run a real import and a real backup, then raise or lower the caps from what the
job log and `systemctl status wpjob-<id>` show. One data point exists: a stop with its final backup of a 15 MB site
took under ten seconds; the 14 seconds in the audit for `stopped` to `hold` was probably the stack starting again `[inferred]`.

### 1.10.4 Cost: what you pay, and what a provider would

Money is the easy half; the harder half is attention. Both are written out so that the comparison is honest.

**Money you pay** (to be completed with your real figures; the structure matters more than my guesses):

| Item | Monthly | Note |
|---|---|---|
| Electricity for the Dell and the AOpen's share of it | **[H to fill]** | the Dell OptiPlex 3040 is a low-power desktop; a plug meter would give the real figure; at an assumed 15 W average it is about 11 kWh a month `[inferred]` |
| Internet | already paid | residential, no extra cost for hosting; a limit rather than a cost: upload speed and a link that "is slow at certain hours" `[stated]` |
| The domain | a share of the yearly renewal | `example.com` is used for everything; the hosting adds no domain cost `[inferred]` |
| Cloudflare | 0 | the free plan: DNS, tunnel, TLS, WAF managed rules |
| Wordfence Intelligence | 0 | a free key; the feed is about 160 MB of JSON |
| Resend (mail) | 0 | free tier; log mode is the default, so mail is rarely sent |

**What a provider charges for the same thing** (`[from memory]`, orders of magnitude, check current prices before
relying on them): managed WordPress hosting for one small site tends to start in the tens of US dollars a month on
the premium providers, and a few dollars to about ten on a cheap shared or cloud-VM plan. For three friends on three
separate plans that is roughly **US$40 to US$100 a month**, or **US$500 to US$1,200 a year**, and each of them would
get an SLA and support that you do not provide.

**The honest comparison** is not "you save US$1,000 a year". It is:

- You exchange **a few dollars a month of electricity** and **an unknown number of hours of your own time** for control,
  for transparency, and for a platform you understand end to end.
- The hours are the real cost. The WordPress hosting has absorbed a large share of a fortnight (the commit count shows
  it). A provider would have cost you less time and more money, and you would have learned less.
- **You chose this on purpose, as a learning project and a favour to friends**, and the document's job is to make sure
  the choice stays a choice: when the maintenance time per month exceeds the value of what you learn from it, moving
  the friends to a provider is a reasonable, even kind, decision.

### 1.10.5 When to grow, and when to stop

Signals, with the instruments that already exist, so that growing is a reaction to evidence:

| Signal | Instrument | What it means | Action |
|---|---|---|---|
| Memory available below about 500 MB at the minimum for a week | node_exporter in Prometheus | the budget is too tight for the real mix | trace the job, then raise `memory:` in `host_vars/delloptiplex3040/vars.yml` and re-run the VM play (RAM grows live; a shrink needs a reboot) |
| `SwapThrashing` fires | Alertmanager, ntfy | a container or job is using swap | look at the job, not the VM size first |
| A site's quota above 80% | the disk check on `dellwp` (`wp-disk-check`) | a friend's site is growing | raise that site's `quota_mb` with `set`; the play checks it still fits |
| Restore drill time grows | the AOpen drill's JSON results | recovery time rises with data | raise the drill timeout, and ask whether the recovery time is still acceptable |
| A fourth friend asks | you | the budget says no | add 768 MB and re-run, or say no |
| Maintenance takes more hours than you enjoy | you | the model has stopped being a learning project | simplify (fewer features), or move a friend to a provider |

**For the expert:** the weakest place in the capacity story is that the budget describes limits, not use. A Prometheus
recording rule for "sum of container memory against the VM's total" per site, and a Grafana page of it, would turn the
budget into something you can watch; the existing node and container metrics make that an hour of work, and it is the
most useful thing the missing "WordPress monitoring page" could show.

## 1.11 What the real runs taught us

Everything in this section was found by a real run on a real machine, not by an offline test. That is the lesson
of the section: the failures live in the seams between our code and someone else's (WordPress, Docker, nginx,
Elementor, ClamAV, the network, the browser). The first eight are carried over from the older explainer. The rest are
new, from the control app's weeks.

### The earlier eight (unchanged)

1. A friend's **cached update state** blocked plugin installs (exit code 0, nothing installed).
2. **Elementor prints into WP-CLI's output**, so "give me a number" returned several lines.
3. A **bind mount keeps the directory it started with**: a wipe that deleted and recreated `uploads` left the running
   container writing into a deleted folder.
4. **`open_basedir` silently disabled the must-use plugins**; the PHP test suite passed because it does not use the
   real `php.ini`.
5. **`cp -a dir/. .` copies the directory's own owner and mode**, so root without capabilities could no longer create
   `wp-content/upgrade`.
6. **freshclam needs two capabilities and real memory.**
7. **A stalled download cost 937 seconds**; the report now ends with a timing section.
8. **Editing a script while someone runs it breaks the run**: bash reads scripts by byte offset.

### The control app's weeks (new)

9. **An nginx directive that is legal alone and illegal in company.** The first deploy of the panel's vhost failed at
   `nginx -t` with `"server_tokens" directive is duplicate in /etc/nginx/conf.d/wp-panel.conf:4`. The Debian
   `nginx.conf` already set it in the `http` block, and our file, included inside that block, set it again at the top
   level. Fixed by setting it inside each `server` block, where it *overrides* instead of duplicating. The offline test
   had asserted that the text was present; the test that would have caught it (nginx's own syntax check) is skipped
   when nginx is not installed, and it is not installed in the sandbox or, as far as we know, the CI runner. A skipped
   test is a promise to run it elsewhere, and the play's `nginx -t` on the VM kept that promise.
10. **`docker top <c> -eo args` fails**: "Couldn't find PID field in ps output". Docker's `top` needs the PID column in
    the format. The rehearsal's worker-count check counted zero and failed on an otherwise perfect container. Fixed by
    giving `docker top` no options, and a test that fails if the script ever passes options again. Found by your first
    run of the rehearsal; the 25 offline tests of the script could not have seen it, because they have no real Docker.
11. **A shell option loop that never ends.** `while [ $# -gt 0 ]; do case $1 in --ip) IP=$2; shift 2 ;; ...` with
    `--ip` as the *last* argument: `shift 2` with one argument left shifts nothing and fails, and the loop runs for
    ever. It had been in the rehearsal script since batch 9. Found while testing a new option; the new test runs the
    script with each value-taking option last, with a 20-second timeout, because a hang is the failure.
12. **A parser I did not trust, and a number that proved it.** Section 1.8.4: 847 corrupted results in 95,839 random
    splits of a multipart body. The lesson is not about Werkzeug (the corrupted cases are adversarial; a real file would
    rarely reach them). The lesson is *how it was found*: by writing a 20-line stress test before building on the
    parser, not after a customer's file came out one byte longer. Cheap to run, decisive to see.
13. **A timeout that is not the timeout.** Installing WordPress waits up to 600 seconds for the tool; gunicorn's
    `timeout = 90` made me worry that it would kill the worker. With the `gthread` worker class the setting is the
    *heartbeat* of the worker's main thread, not the length of a request. Proven rather than argued: a test starts the
    real gunicorn with `--timeout 2` and a request that sleeps 5 seconds; the request completes.
14. **"The UI looks good" meant my screenshots looked good.** I rendered the pages headless with fake data and they
    looked right. Your browser showed three things mine could not: cards touching where a grid met the next card; two
    boxes of different heights; and a *serif* font everywhere, because `system-ui` on your machine resolves to a
    serif face. The fix named sans fonts first and `system-ui` last. The standing rule that came out of it already
    existed in Bloomy's `CLAUDE.md`: "jsdom cannot see CSS or layout: say plainly that how it looks is unverified."
15. **A test that asserted the absence of the thing we then added.** `wp_ctl_panel_keys` had a test, written weeks
    earlier, saying "no key until the control VM exists". When the key was added, the gate caught it, and the test
    now says "exactly one key, the app's". The stale assertion was correct in its day; the gate is what noticed the day
    had ended.
16. **A pinned certificate that had changed.** The OPNsense plays verify the box's certificate against a pinned file.
    The box's own certificate had been replaced (a new one, signed by `homelab-ca`, valid to 2036-10-26), so every
    play against it failed with a "self-signed certificate" error. The fix was a new pin; the lesson is that a pin is a
    dependency on a fact that can change, and it needs an expiry note in the one place you will look. The old doc still
    names the previous expiry (2027-07-22), which is why the fold-in matters.
17. **A virtualenv that did not survive a distribution change.** After the ThinkPad moved from Debian to Fedora, the
    play that builds VMs failed because `~/.venvs/ansible-proxmox` did not exist. The fix is documented in
    `requirements-controlnode.txt`; the lesson is that the control node has state too, and that state needs a rebuild
    recipe.
18. **CI cannot build a Docker image.** I claimed, wrongly, that CI could build the panel's image. The runner's job
    containers have no Docker socket. I corrected the claim as soon as it was checked and replaced it with a script
    that proves the image on a real host (`scripts/wp_panel_image_rehearsal.sh`, run on the HP: a 46 MB image, three
    workers on one CPU, a 111 MB peak). A confident wrong claim, caught and withdrawn, is the cheapest kind; the habit that
    caught it is "mark claims `[verified]` or `[inferred]`".
19. **The first image on the first real run saw all the host's CPUs.** gunicorn's default worker count derives from
    CPU count; inside a container with a 256 MB cap, 17 workers did not fit. The fix pinned the container to one CPU in
    the smoke test, a 384 MB cap, and `preload_app` so the workers share one import.

### Context only we know: what the pattern of these is

Look at the list from a distance. Not one of items 9 to 19 is a logic error in the program's *own* idea. They are an
nginx configuration inherited from a distribution, a Docker subcommand's argument, a shell loop's edge, a library's
chunk boundaries, a worker class's meaning of a word, a font stack on one machine, a stale assertion, a pin, a venv, a
CI limitation, a CPU count. Every one lives where *our* code meets *someone else's*. The test suite is excellent at the
first kind of error and blind to the second; the rehearsals are the second kind's net. That is the argument for
keeping the rehearsals when it would be easy to drop them as "just scripts".

## 1.12 What is proven, what is not, and what is next

### 1.12.1 Seen on a real host (by your runs, 2026-10-28 to 2026-10-30)

- The dispatcher: every verb's offline behaviour, and on `dellwp` the state moves, `set`, `delete` with the name twice,
  the tombstone, the audit lines (`scripts/wp_ctl_rehearsal_dellwp.sh`, `scripts/wp_state_rehearsal_dellwp.sh`,
  `scripts/wp_provision_rehearsal_dellwp.sh`).
- The control VM, its network in four layers, its certificate, its nginx, its container, its ssh to `dellwp` with the
  forced command (the rehearsal: "every check passed").
- The login with a real authenticator, the lockout logic (tested offline; the 15-minute lock itself is not something you
  triggered), the pages, the security headers, TLS verified against the name, a handshake for another name refused.
- The browser: create, set, state moves, delete (after stopping), and the new look, in light and dark.
- A 300 MB upload through the whole chain, checked by count and hash, with the memory peak and nginx's body directory
  checked (the rehearsal's step 4), and removed again.

### 1.12.2 Not yet seen on a host

- **Install** and the **password** page against a real site; the **delete page** (the CLI delete worked; the two-page
  flow in the browser is the checklist's section G).
- The **import pages**: plan, run, report, approve, reset, replace, remove a file, and the upload script in a browser
  with a real `.wpress` (checklist F).
- **A cut-off upload.** The panel's discard-after-cut-off path is tested offline against a stand-in `ssh`; the real
  `dellwp` behaviour (does it keep the short file, and does the discard remove it in time?) is checklist F step 8.
- **The job limits being enforced** by a job that hits them; **two requests at once** (only offline); a site with a
  friend's real content; **the public path through the tunnel** (`wp_state_rehearsal_dellwp.sh --with-live` never ran;
  checklist E step 2).
- The **monthly restore drill** at the time of writing is deployed and its next scheduled run is the 1st of
  November; its first real result is not in this document.

### 1.12.3 What is next, in the order I would take it

1. Run the click-through checklist (`docs/wordpress_hosting.md` section 12) and fix what it finds.
2. Close the truncated-upload hole at the source (an optional byte count on `import-receive`).
3. Add an `import-list` verb so the page can show what is in the intake.
4. Settle `dellwp`'s RAM with a week of real evidence (section 1.10.2).
5. Fold the finished work into the numbered docs (the handbook, `ansible_reference`, the archive for the retired
   tools, new lessons and principles), then retire the temp handoffs.

> **A last word on Part One.** Notice what the service is *not*: it is not a hosting company, and it does not try to
> look like one. It is a careful favour with an honest list of what it cannot do. The most valuable thing in the
> design may be the sentence in section 1.4: *a provider spends money and staff on being forgiving; we spend code and
> tests on being strict.* Everything else is that sentence worked out in detail.

---

