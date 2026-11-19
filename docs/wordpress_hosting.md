# WordPress hosting for friends on a home server: what we built, why, and how it compares with a hosting provider

*Archive edition, 2026-11-19. The service was retired on that date; the text below is the explainer as it stood on 2026-11-15, followed by appendices copied from the other documents of the original repository (the plays and timers, where it ran and how it was backed up, restore and rebuild, the open items at retirement, and the lessons it taught). Statements in the present tense describe the service while it ran.*

*Written for the operator, who is "you" throughout. Marks: `[verified]` = seen in real output during the work; `[stated]` = the operator said it; `[inferred]` = reasoning, not tested; `[from memory]` = general knowledge about the industry, not looked up. File names are real paths in this archive or in the original repository (a handbook, an Ansible reference and a restore runbook that are not included); where a path does not exist here, the relevant text is in an appendix. The C4 diagrams are in [`architecture.md`](architecture.md) and the decisions in [`decisions.md`](decisions.md).*

## 1. The service in one page

You run WordPress sites for a few friends on a virtual machine (`dellwp`) in your home lab. Each friend's site is its own small stack of containers. The public internet reaches a site through a Cloudflare tunnel, so nothing at home is open to the internet. You are the only operator: you create sites, import a friend's existing site from a file, approve what the friend may run, watch the backups and updates, and delete sites when a friend leaves. The friend gets a WordPress admin login with two-factor and nothing else: no server access, no plugin installs.

Everything about the service is written down as code in this repository: how the VM is set up, how each site is built, the tools you run, the tests that prove the tools do what they say, and the rehearsal scripts that run a whole site's life against the real VM. The rest of this document explains why it looks the way it does.

## 2. The comparison: what a hosting provider does behind the curtain, and what we do instead

A paid WordPress host sells you a long list of things that you only notice when they are missing. Here is that list, with what we do in each place and why. "Provider" below means a managed WordPress host such as Kinsta, WP Engine or Cloudways, or a cPanel shared host; the descriptions of them are `[from memory]` and typical, not exact.

| Concern | A provider typically | We do | Why, for our context |
|---|---|---|---|
| Isolation between customers | one container or account per site; stronger on managed hosts, weak on cheap shared hosts | one VM for all WordPress; inside it, one Docker stack per site with its own network, database container, project disk quota, memory and CPU limits | A break-in on one friend's site must not reach another's database or fill the disk. The VM keeps WordPress away from the shop (`dellapps`) and its staging copy (`dellstaging`) that live on the same hardware. |
| Who can change code | the customer, usually, via the admin (plugin and theme installs) | nobody through the web: `DISALLOW_FILE_MODS`, code read-only to PHP, plugins only from wordpress.org by slug, installed by you | The commonest way a WordPress site is taken over is a vulnerable or malicious plugin. If the admin cannot add code, a stolen friend password cannot add code either. |
| Updates | automatic, often with a visual check before and after | a nightly job per site: snapshot first, update, check the home page, the login form and PHP errors, roll back by itself on failure (not yet run on a real site, handbook §19); major plugin updates held for you | We cannot afford support staff. A silent bad update must undo itself. |
| Backups | daily, kept 14 to 30 days, restore button | nightly snapshot (database dump plus hard-linked `wp-content`), 7 daily and 3 weekly kept, a freshness check every hour, a monthly drill that restores one site into a scratch copy and boots it | A backup that was never restored is a hope. The drill is the proof. |
| Restore | one click, by support | `wp-site-recover`: replaces database and files from a snapshot in place, with an undo, and proves WordPress boots before opening | The undo is what makes it safe to try. |
| Malware and vulnerabilities | scanners, managed WAF, often virtual patching | every import scanned before you are asked (ClamAV in a pinned container, patterns, checksums against wordpress.org) and every site checked daily against the Wordfence vulnerability feed; Cloudflare's free WAF in front | Cheap, real, and honest about its limits (section 5.8). |
| Migration in | a plugin or a support ticket | a one-command import with a defined trust model (section 5.6) | Migration is the riskiest moment: you are running a stranger's database. |
| Staging and previews | a button | none yet; a `hold` state where only the VM itself can see the site | A friend's test site is the practice here. Staging is a later question. |
| Email | built in or an add-on | a must-use plugin that sends through Resend's HTTPS API, from `<site>@mail.example.com`; log mode by default | The VM cannot send SMTP at all (a firewall rule), by design: nothing on it can spam. |
| Login security | brute-force limits, sometimes 2FA | nginx rate limit, a second throttle in PHP, a generic login message, XML-RPC and user listing off, and two-factor required for administrators | The login page is what the internet attacks. |
| Uptime and support | an SLA, on-call staff, a status page | none. You are the on-call. A VM on a home network with a residential link | This is a favour for friends `[stated]`, not a product. The honest list of what that costs is in section 6. |
| Scaling | elastic plans | none: a fixed RAM and disk budget per site, checked by the play before it builds anything | Two live sites fit at the default sizes on the current 3 GB VM; the play refuses a change that does not fit, a third site included. |
| Cost | monthly per site | electricity, your time, a free Cloudflare plan, a free Wordfence key, a Resend free plan | |

The pattern in that table is the thesis: **a provider spends money and staff on being forgiving; we spend code and tests on being strict.** We cannot be forgiving at 3 a.m. when nobody is awake, so every action either has an undo, or asks you first, or is impossible.

## 3. The context that drives every decision

Six facts about your situation shape the design. When a decision looks odd, it is usually one of these.

1. **Small scale, long tail of care.** A handful of sites, each belonging to a friend. Nothing needs to be fast to build, everything needs to be safe to leave alone for weeks. `[stated]`
2. **One operator, often away from the keyboard.** You approve things from a phone. Anything that needs judgement while you are away must carry its own facts (the notification summarises the import report) and must be reversible. `[stated]`
3. **Friends are trusted but fallible.** They are not attackers, but their laptops, passwords and plugins are as weak as anyone's. The design assumes a friend's site will be compromised one day and limits what that costs.
4. **A friend's `.wpress` file is untrusted input.** It comes from a friend's laptop and contains a database and code. Loading it is running a stranger's data on your server (section 5.6).
5. **Home hardware and home internet.** One machine, a residential link that is slow at certain hours `[stated]`, no redundancy. So backups, restore tests and "what if the Dell dies" are first-class, and time-sensitive steps are cached or bounded.
6. **Infrastructure as code is the house style.** Your other services (the shop, the monitoring, the CI) are all declared in this repository and deployed with Ansible. WordPress follows the same rules rather than being a hand-built exception.

## 4. The architecture, from the hardware up

```
visitor ── Cloudflare (TLS, free WAF) ── tunnel ──▶ nginx on dellwp (127.0.0.1:8080)
                                                         │  one server block per site, rate limits, 503 pages
                                                         ▼
                           ┌────────────── per site (Docker Compose, one systemd unit) ───────────────┐
                           │ php-fpm container (code read-only, uploads writable, hardened php.ini)    │
                           │ MariaDB container (own network, own schema and user, not published)      │
                           └──────────────────────────────────────────────────────────────────────────┘
dellwp = a VM on the Dell Proxmox host; /srv/wp is its own 40 GB data disk (ext4 project quotas)
```

What each layer buys:

- **Cloudflare tunnel.** No inbound port at home. The visitor's real address arrives as a header that nginx trusts only from Cloudflare, which is why the login throttle can key on it.
- **One nginx outside Docker.** One place for the rate limits and the "this site is paused" pages. It reaches each site's php-fpm on a loopback port.
- **A Compose stack per site, under systemd.** `systemctl restart wp-<site>` is the only way a stack changes, so Ansible and a human never fight over container state.
- **Project quotas.** A site that fills its disk fills only its own quota; the backups have a separate quota so a full site cannot stop its own backup.
- **`/srv/wp`, a separate disk.** WordPress data is isolated from the OS and from the shop, and can be sized or snapshotted by Proxmox on its own.

Two small ports exist besides the public one: a *probe port* (8081) that ignores the maintenance flag, so the update and recovery tools can test a site before reopening it; and the `hold` state, in which only a request from the VM itself is answered.

## 5. The decisions, theme by theme

### 5.1 Why a VM, and a stack per site

A provider isolates customers with containers or accounts and a lot of operational experience. We have less experience and more reasons to be careful, so we isolate twice: a VM for the whole WordPress workload, and inside it a stack per site with its own database and network. A friend's database password is derived from a master secret, the site name and a revision number, so rotating one site's secrets changes only that site. The cost is RAM (each stack has two containers) and a hard limit on how many sites fit, which the play checks (`wp_vm_ram_reserve_mb`, the summed `php_mem_mb` and `db_mem_mb` of the live sites; and the summed `quota_mb` and `backup_quota_mb` against the data disk) before it changes anything.

### 5.2 Why the admin cannot install plugins, and how a friend asks

In most WordPress compromises the attacker gets a plugin or theme installed (a bad one, or an old vulnerable one) and uses it. We remove the step: `DISALLOW_FILE_MODS` is on, the code directories are owned by root and mounted read-only into PHP, and the only writable directory is `uploads`. Nothing but `wp-site-ext` puts code on a site, from wordpress.org and by slug; `wp-site-cli --code` refuses anything that is not a slug (a URL, a zip, a path).

A friend still gets the screens they know. A must-use plugin (`templates/wp/mu/50-wp-hosting-plugin-requests.php`) gives administrators the install permission back (WordPress takes it away when file changes are disallowed), so Plugins > Add New, the search and the Install button exist. Pressing Install is caught before WordPress's own handler runs and written down as a **request** (a slug, who asked, when) in an option of the site's database; nothing is installed by the press. The Upload Plugin button is hidden and its form refused. The read-only code mount is the second lock: should the interception ever fail to fire, the install fails at the filesystem `[inferred]`. That a press becomes a request, and that Add New and the Requested button show, was seen on a real site on 2026-10-31 `[verified: scripts/wp_checklist_dellwp.sh, step 7c]`.

The request reaches you without the site reaching out. `wp-plugin-requests` on dellwp (a timer every `wp_plugin_requests_interval_min` minutes, 15 by default, and the Refresh button on the panel's Requests page) reads each live site's waiting requests with WP-CLI as the web app's identity, so root never opens a file the site controls. Everything in that option is the site's word: each request is shape-checked, a site may have only so many waiting, and one that nobody answers expires after a week. For each new one dellwp asks wordpress.org whether the plugin exists under exactly that name and checks its latest version against the vulnerability index, then sends the phone notification (section 5.7). Approve installs it from wordpress.org by slug (checksum-checked, removed again if the checksum fails), **inactive**; then the page cache is cleared and the answer is written where the friend's administrator reads it. Activating runs the plugin's code, so it stays the administrator's step in wp-admin or yours from the panel's Plugins page. The cost is a decision per plugin; that is deliberate (`[stated]`: "approve mode only, no auto").

**Themes go the same way.** A request has a kind, plugin or theme (an entry without one is a plugin). The same must-use plugin gives administrators `install_themes` back, catches the Install press on Appearance > Themes > Add New and the plain link, and refuses a theme zip. The same collector looks the theme up with `theme search`, checks it with `wp-vulnfeed check-theme`, notifies the phone and, on Approve, runs `wp-site-ext <site> theme install <slug>`, inactive. One Requests list in the panel holds both kinds, with a badge. The rules are the plugin rules: a theme with a known vulnerability, or one that could not be checked, has no phone Approve button and is approved only on the panel with its name typed. A theme token is bound to its kind, so a plugin token cannot approve a theme. That a theme request is filed, approved and shows up inactive in wp-admin was seen on a real site `[stated: the operator, 2026-10-31]`; how the card button reads in every browser was not checked.

**Deleting stays with the operator.** An administrator can switch a plugin or theme on and off, but not delete one; that is the panel's Plugins page (the plugin's name typed). A delete-by-request path was designed and dropped for simplicity `[stated]`. Plugins we decline (Loco Translate, which writes translation files under `wp-content/languages` and honours the file-modification lock, and the cache plugins, which write `wp-content/cache` and a drop-in, when nginx already caches pages) are declined by answering the request, not by a list in the config `[stated]`.

### 5.3 Updates with rollback

A nightly job per site: take a snapshot tagged `pre-update`, apply the updates, then check three things (the home page answers, the login form is there, no PHP fatal in the log). If any check fails it restores the snapshot by itself and tells you. Under the `auto` policy a **major** plugin update is held for you (a major version is where breakage lives); under `notify` nothing is applied and you are told once; `freeze` skips the site entirely. The `auto` path has not yet run on a real site; that first run is a pending proof (handbook §19). The same tool does a rollback that you can run by hand. The reasoning: with no support staff, "update and hope" is not acceptable, and "never update" is how sites get hacked.

### 5.4 Backups, restores and why we rehearse them

`wp-site-backup` makes a snapshot in a hidden folder and renames it into place only when the dump has passed `gzip -t`, has its completion marker, and contains the `wp_options` table. Files are hard-linked against the previous snapshot, so a night costs only what changed. An hourly check alerts if a site's newest backup is older than 30 hours. A monthly drill restores a random site into a scratch copy with a throwaway database and boots it with WP-CLI. If no site on the VM has a good backup, the drill exits successfully with nothing to test (`templates/wp/wp_restore_drill.sh.j2`), so the first run, 2026-11-01, proved only that the timer runs; the first run against a real site is pending (handbook §19). Delete is a three-step safety: a final backup first, then an archive that is kept 14 days, then removal; the name is typed twice and never reused (a "tombstone" stays in `wp_sites`, so a deleted site's port and quota id cannot be handed to a new site by accident).

**Restoring from the panel.** A site's page has Backups and restore: the complete snapshots, newest first, each with its kind (nightly, before an update, before an import, last before stopping), the WordPress version and the size. Restore opens a page that names the snapshot and says what is replaced; the site's name typed starts `wp-site-recover` as a job. What it does: the database and `wp-content` are replaced by the snapshot's, core is set back to the version the snapshot recorded, a dump of the current state is kept to undo with, and the old state is put back if anything fails. The site shows the maintenance page (a 503) while it runs; a live site is allowed, a stopped one is not (it has no containers). The users and passwords come back with the database. `wp-config.php`, `.env`, the site's settings and the other sites are not touched. There is no way from the panel to skip the undo: that is for the operator at a shell. That it works was seen on a real site `[stated: the operator, 2026-10-31]`.

An empty site (just created, waiting for an import) has nothing to back up. Those jobs skip it quietly, because an alert every night for a site with no data teaches you to ignore alerts. A site that once had real backups and lost its tables is the opposite case and still alerts.

### 5.5 The states

`hold` (only the VM itself can see it), `live` (public), `stopped` (containers down, a paused page, data intact), `closed` (a tombstone). A new site starts on `hold`, so it never goes public by accident. Going `live` is its own step, with its own question.

### 5.6 Importing a friend's site: the hardest part, and why it is built the way it is

A `.wpress` file (All-in-One WP Migration, as Local writes it) holds a database dump, `wp-content`, and a `package.json`. A provider's migration plugin simply loads it. We cannot, because **the database is attacker-controlled input to code that will run.** WordPress stores which plugins and which theme are active in the `options` table, and then `include`s those files. A hostile or just damaged database can name a file you did not intend. So the importer never lets WordPress run before it has reset the options that name code (`active_plugins`, `template`, `stylesheet`) to what `package.json` says, and every WP-CLI call before approval runs with `--skip-themes` and, unless it truly needs them, `--skip-plugins`.

The pipeline, in order, and the reason for each step:

1. **Preflight** (on your laptop, before anything is sent): reads only `package.json` and the plugin headers, asks wordpress.org about each plugin, and says GO or NO-GO (an archive from a newer WordPress than ours, a plugin that needs newer PHP, an unsafe path, a multisite are refusals).
2. **Send** over ssh with a SHA-256 compared on both ends, into a root-only intake directory with its own quota.
3. **Unpack with a policy**, not everything: the migration plugin itself, caches, LiteSpeed data, half-finished updates, PHP files under `uploads`, and hidden files are not copied. Names are validated at the plan and again at approve and reset.
4. **Load the SQL** with the migration plugin's table-prefix placeholder swapped for ours, as the site's own database user.
5. **Reset the options that name code**, and drop the friend's cached *update state* (a stale record that says "elementor update available" made WordPress report "already installed" and install nothing; found on the real VM).
6. **Reinstall from wordpress.org**, at the archive's version when it is still offered. Plugins that are not on wordpress.org are **held back** in the work directory; nothing of them has run.
7. **WP-CLI steps**: database upgrade if needed, search-replace of the friend's old address in six forms, Elementor's URL replace and CSS flush, transients, rewrite rules, "discourage search engines".
8. **Scan**: pattern hits (`eval`, `base64_decode`, shell calls), `verify-checksums` of core and plugins against wordpress.org, ClamAV, and the vulnerability feed.
9. **Report, first backup, notification.** The site stays on `hold`.
10. **Your decision.** *Approve* copies the held code in, root-owned and read-only, deletes the intake file and the work directory. *Deny* (or `reset`) undoes the run by a manifest of what it wrote plus a snapshot of the `wp-content` tree taken before the load, so the site is back to what it was.

Why a manifest and a tree snapshot rather than "just delete the site and start over": a name is spent forever, so a failed import must be undoable in place.

**`--replace`.** A friend sends a newer file for a site that already has data. The tool takes a protected snapshot (`pre-import`, never pruned while the import waits), wipes the database and `wp-content` in place, and imports. A deny puts the old site back through `wp-site-recover`; an approve keeps the snapshot as an ordinary `pre-update` one so normal retention ages it out. You type the site name first, because this is the one operation that throws data away.

### 5.7 Approvals from the phone, and what a phone is allowed to do

The notification (to your independent ntfy server) carries a summary and two buttons. Each button posts a **token** to a topic. A token is an HMAC-signed statement: this site, this action (approve or deny), a one-time nonce, an expiry, and the SHA-256 of *this exact report*. The VM checks the signature before it parses anything, checks the nonce is one it issued, checks the report has not changed, and consumes the token before acting. The button's own login can only write to that one topic, and the listener on the VM can only read it and forwards token-shaped strings to the tool. So even if someone found the button's credentials, all they could do is post a token that does not verify.

The *policy* part matters more than the cryptography: **a phone can approve only a clean import, or deny.** `--accept-failures`, `--reject` and going live are never available to a token. A known vulnerability or a failed step removes the Approve button entirely. The reason: a tap on a phone, possibly from a lock screen, is the weakest form of consent, so it is only allowed to confirm the safe case.

Plugin requests (section 5.2) use the same mechanism with a second kind of token: the prefix is `p1.` and the signature covers the prefix, so neither kind passes for the other, and the listener picks the tool by the prefix. The policy has the same shape: **a phone can approve only a plugin that exists on wordpress.org under that exact name and has a clean vulnerability check**, or deny. A known vulnerability, or a check that could not run ("not checked" is never a pass), removes the Approve button. Such a request can still be approved on purpose from the panel, by typing the plugin's name. A plugin wordpress.org does not have cannot be approved at all.

### 5.8 Scans and the vulnerability feed: what they prove

ClamAV runs in a pinned container with no network and everything mounted read-only. Its signatures live on the VM and are refreshed daily by a separate one-shot container, so a scan never needs the internet. The vulnerability feed is Wordfence Intelligence's data (a free key, about 160 MB of JSON), reduced to an index; a daily job checks every site's core, plugin and theme versions against it and tells you once per new finding. If the index is older than three days the check says "NOT RUN", never "none known".

What they do not prove: a clean ClamAV result does not mean a plugin is safe (signatures catch known malware), and the feed only knows published vulnerabilities. The report says so in its own header: "a hit is a place to look, a clean result proves nothing". `[verified]` The first real use found real CVEs (at least three, one rated 7.5) in the practice file's WooCommerce 11.0.1, and correctly withheld the Approve button.

### 5.9 Login security, two-factor and mail

nginx limits `/wp-login.php` (about 10 a minute with a burst); inside PHP a second layer counts failed logins per visitor address and per user name over 15 minutes and refuses even a correct password past the limit. A wrong user name and a wrong password produce the same message, so the login form cannot be used to find out which users exist. XML-RPC, application passwords and the user listing (`?author=1`, the REST users endpoint, the users sitemap) are off unless a site's flag allows them.

Two-factor uses the official Two Factor plugin, pinned by version and by the SHA-256 of its zip. Only the authenticator app and backup codes are offered (no email method, because a mailbox is a weaker second factor and we have no mail to depend on). Administrators and shop managers are *required* to set it up: until they have, wp-admin sends them to their profile page, and the REST API answers 403 except for the plugin's own routes. If the component is missing or broken, they are locked out rather than let in without the second factor. If someone loses their phone and their codes, you run `wp-site-2fa-reset`, which clears that one user's settings after you type the site name.

Mail replaces `wp_mail()` through the `pre_wp_mail` filter. In log mode (the default) nothing leaves the site; in send mode it calls Resend's HTTPS API with a ten-second timeout, forces the From to the site's own sender address and moves the friend's address to Reply-To (so replies work and the domain with the DNS records is the only sender), and caps mails per hour and per day. The reason we do not just let WordPress use SMTP: the VM and its containers cannot reach SMTP at all (a firewall rule), so a compromised site cannot send spam from your address. Delivery is not yet verified: the 2026-10-31 checklist switched send mode on and off and checked the configuration only.

### 5.10 Why our plugins live outside `wp-content`

A friend's archive brings its own `mu-plugins` folder (must-use plugins load automatically, with no activation). If ours lived in the same folder, an import could overwrite or delete ours, and a friend's file could shadow them. So ours are in one root-owned directory on the VM, mounted read-only into every site's container, and `WPMU_PLUGIN_DIR` points there. The friend's `mu-plugins` is then inert unless you approve it, and even approved it is not loaded. The cost showed up in the first real run: PHP's `open_basedir` forbade reading the new directory, WordPress silently loaded none of the plugins, and nothing complained (section 7).

### 5.11 How we know any of it works

- **Offline tests with stand-ins** (docker, curl, systemctl, ntfy, the WP-CLI container): they prove the scripts' logic, not that the real programs behave the same. About 1,600 test cases across 33 suites, plus a PHP suite of 21 scenarios (`tests/php/run_all.php`, run with PHP installed or in the sites' own image), plus a test that gives every vhost the repo renders to the real `nginx -t` (`tests/test_wp_nginx_conf.py`, skipped where nginx is not installed).
- **Breaking the code on purpose.** For every change we copy the repo, apply small wrong edits ("mutants": flip a condition, drop a flag, swap two lines) and run the tests (`tests/mutate_wp_ctl.py`, 440 entries, each naming its suite); a mutant that survives means a test that passes for the wrong reason, and the test is fixed, or the mutant is shown to change nothing. This found dozens of vacuous tests.
- **Rehearsal against the real VM.** The lifecycle rehearsal was removed with the old per-site engine (commit `2115b0a`); the current rehearsals are the `*_rehearsal_dellwp.sh` scripts in `scripts/`. The old one created a site, imports the practice file, denies, imports again, approves, operates it, stops, starts and deletes it, checking dozens of things at each step and timing each phase. Most of the real bugs in this project were found there, not by the tests (section 7).
- **Plans that fail loudly.** A play that cannot do something safely stops before it changes anything and says why.

### 5.12 Deploy speed

An Ansible task costs a round trip to the VM (about 1.3 s each). A deploy went from 157 s to 46 s by three changes: SSH pipelining; probing the closed sites with two commands instead of a loop (each deleted site used to add about 8 s forever); and rendering the 40-odd tool and unit files on your laptop and sending them with three rsyncs. A `wp_tools` tag lets a fix to a script deploy in about 20 s.

### 5.13 Speed: what is cached, what is not, and how we measured

Most of a WordPress request is PHP starting WordPress up, not the database, and PHP starts it from nothing on every request: a worker that outlives a request keeps only OPcache (compiled code). So the only ways to be faster are to not run PHP, or to run less of it. On the test shop (`envo-ecommerce`, one request at a time, an idle site) a page the cache holds costs about 0.5 ms, a page PHP builds about 360 ms, `/wp-json/` about 320 ms and a bare WordPress start-up about 180 ms `[verified: scripts/wp_ab_test.sh, scripts/wp_perf_measure.sh, 2026-10-31]`. By CLI profile the plugins cost WooCommerce about 375 ms, Elementor 238, a cache plugin we then removed (WP-Optimize) 210 and the theme's companion plugin 129 `[verified: scripts/wp_perf_profile.sh, 2026-10-30]`.

**What is cached.**
- **Anonymous pages**, in nginx (`fastcgi_cache`, `wp_page_cache_s` seconds, 0 turns it off; no Redis `[stated: 2026-10-30]`). A page is not cached, and not served from the cache, for a login, comment, cart, language or currency cookie, for WordPress's and WooCommerce's own endpoints, the REST API, search and previews, for any request that names a `.php` file, for an Authorization header and on the probe port. A stale page is served while one request rebuilds it. 98 % of simulated page views were answered from it.
- **Static files**: nginx answers and PHP never starts; a missing static file is a plain 404 (about 0.4 s of WordPress start-up saved each), a present one gets a 30-day expiry, and text is gzip-compressed.
- **WooCommerce's mini-cart refresh** (`POST /?wc-ajax=get_refreshed_fragments`, sent by the browser after every page): in the traffic simulator it was all of the PHP work (about 235 ms each). The per-site setting `cart_fragments: cache` keeps the empty-cart answer, for visitors with none of the cookies above, for the page-cache lifetime, in a location of its own that ignores WooCommerce's `no-store` headers (nowhere else does). 474 of 476 refreshes were answered in 0.5 ms; adding a product still updated the header `[verified: simulator output; the manual cart test is the operator's, stated]`.
- A change that must show at once (a plugin change) **deletes the cache files**. An nginx reload does not clear a `fastcgi_cache`: its entries are files on disk.

**What we did not change.** The network path (the tunnel, DNS, WireGuard). PHP itself: an uncached request still costs 180 to 360 ms, and only fewer or lighter plugins reduce that. `open_basedir` stays on `[stated]`: turning it off saved about 330 ms of CLI start-up, but it is a security control, and why it costs that much is not verified.

**OPcache.** The timing plugin found the interned-strings buffer full (8 MB used, none free), and it is now 16 MB (`wp_php_opcache_strings_mb`). An A B A B comparison on the live site found **no difference beyond the noise** in any of five measurements (the same setting differed 2 to 3 % from itself; the two settings 0 to 2 %), so the 16 MB stays because the buffer was full, not because anything got faster.

**The tools** (all ssh-only; HTTP only to dellwp's own loopback inside the ssh session): `scripts/wp_perf_measure.sh` (request times by cache state from the access log, where the uncached PHP time goes, the slow log, the timing lines), `scripts/wp_perf_profile.sh` (a CLI profile, with one plugin left out at a time), the timing must-use plugin (`templates/wp/mu/40-wp-hosting-timing.php`, `wp_php_timing` 0, 1 or 2: stage marks, plugin include times, queries and OPcache's gauges from inside php-fpm), `scripts/wp_traffic_sim.sh` (a visitor model against nginx), `scripts/wp_ab_test.sh` (A B A B on `php.ini`, one confirmation, `php.ini` put back on every way out) and `scripts/wp_checklist_dellwp.sh` (a throwaway site through its whole life, steps 7b and 7c cover the mini-cart cache and plugin requests).

**Limits.** One request at a time on an idle test site; real traffic would change the picture. The cache helps repeated anonymous views, not the first one. Every number above is from one site.

### 5.14 A plugin that needs a place to write: the migration plugin's export

Friends need All-in-One WP Migration to take their own backup: press Back up, wait, press Download. Under the read-only code mount the plugin fails twice (it cannot make `wp-content/ai1wm-backups` or its own `storage` directory), and WordPress asks for FTP credentials because it sees nothing writable. Both are the lockdown working, not a fault. The answer is to open exactly what the plugin needs and nothing else, per site and only when the plugin is there.

- **The switch.** `ai1wm_export` is a per-site setting, on by default (`wp_site_defaults`). It counts only while the plugin's own directory exists on the site: Docker makes a missing mount point as a root-owned directory, which the plugin's install would then refuse `[inferred: not tested on a host]`. So `wp-site-provision` renders the mount only when `plugins/all-in-one-wp-migration/` exists, and drops it again if the plugin is removed. Approving that plugin's request applies the site, so the mount appears without a manual step (the stack restarts, about ten seconds).
- **What it opens.** One bind mount, `plugins/all-in-one-wp-migration/storage`, writable by PHP; the exports go to `uploads/ai1wm-backups` through `AI1WM_DEFAULT_BACKUPS_PATH` in `wp-config.php` (the plugin's storage path itself is not configurable). Everything else stays read-only and `DISALLOW_FILE_MODS` stays on. nginx runs no PHP and serves nothing from either directory.
- **The download.** The plugin's Download button is a plain link to `/wp-content/uploads/ai1wm-backups/<name>.wpress` `[verified: the operator's browser inspector, 2026-10-31]`. An export is a whole site (database and files), so nginx never serves that folder itself. A request for a `.wpress` there is rewritten to `/index.php?wph_dl=<name>`; a must-use plugin (`60-wp-hosting-download-gate.php`) lets an **administrator** through with an `X-Accel-Redirect` to an internal location, and everyone else gets 403 (a name that cannot be an export, 404). nginx sends the file, so PHP never reads it. That the download works for an administrator `[stated: the operator, 2026-10-31]`; the logged-out and non-administrator refusals were not exercised on a host.
- **The exports stay** in the plugin's Backups list, which is where friends find them again. Nothing deletes them, so they count against the site's quota until removed.
- **What does not work.** The plugin's own import: it extracts plugins and themes into the read-only `wp-content`. Importing a `.wpress` to make a site stays the operator's (`wp-site-import`, the panel's Import page), and it accepts `.wpress` only; Duplicator's zip-plus-installer is not supported, so a friend is asked to export with All-in-One WP Migration.

**Other plugins, by where they write** `[inferred from plugin documentation and forum threads; only the migration plugin was run here]`:

| Plugin | Writes | Here |
|---|---|---|
| Elementor, Yoast SEO, WooCommerce, Contact Form 7 | the database and `uploads/` (Elementor's CSS, WooCommerce's `wc-logs` and `woocommerce_uploads`, CF7's `wpcf7_uploads`, Yoast Premium's redirects) | expected to work |
| Redirection | database tables (its log can grow large) | expected to work |
| Rank Math | its robots.txt and `.htaccess` editors need write access | the editors do not work; the rest does |
| ACF | Local JSON to the theme's `acf-json` | Local JSON does not work; field groups stay in the database `[not checked]` |
| Wordfence | `wp-content/wflogs`; `WFWAF_LOG_PATH` can move it | partial; its firewall setup also writes outside `uploads/` |
| UpdraftPlus | `wp-content/updraft`, relative to the content directory | would need a mount, a path under `uploads/` and a download gate like the one above |
| WPML | a cache directory (`WPML_CACHE_PATH_ROOT`) and `.mo` files in a languages directory | partial; needs a real test |
| Loco Translate, WP Super Cache, W3 Total Cache | `wp-content/languages`, `wp-content/cache` and drop-ins | declined |
| Duplicator | `wp-content/backups-dup-lite` or a root `wp-snapshots` | not supported |
| Akismet, Site Kit, MonsterInsights, WP Mail SMTP, CookieYes, WPForms | not found by the search; believed to use the database and external services | not checked |

Where a plugin offers a constant or a filter to move its write path (Wordfence, CF7, WPML), one `define` in `wp-config.php` pointing at `uploads/` costs less than a mount; a mount, like the migration plugin's, is for a plugin that hard-codes its path.

## 6. What a provider gives you that you do not have, and the reverse

**You do not have:**

- **An SLA, support or an on-call team.** If the Dell dies at 2 a.m., sites are down until you act. The Proxmox nightly backups and `restore_runbook.md` are the plan.
- **Redundancy.** One host, one disk, one residential internet link.
- **A CDN for dynamic content, edge caching and image optimisation.** Cloudflare's free proxy helps with static files and attacks; it is not a performance product.
- **Elastic scale.** A friend whose site goes viral takes down their quota, not the others, but the site is simply slow.
- **Staging environments, uptime monitoring from outside, a status page.** (An HP probe is on the open list.)
- **Compliance paperwork.** Backups of customer data on home hardware, mail from a shared sender domain: fine for friends, not for strangers.

**You have, and most hosts sell as extras or do not offer:**

- **Full transparency.** Every decision is a line of code you can read; every check has a test.
- **A defined trust model for migrations.** Most hosts' import plugins load whatever is in the file.
- **Rollback by default** for updates, restores and imports.
- **No shared fate with strangers.** Your neighbours on this server are your friends' sites, and you chose each.
- **Cost near zero** and control of where the data lives.

## 7. What the real runs taught us (each found on dellwp, none by an offline test)

1. **A friend's cached update state blocked plugin installs.** The archive carried a record saying Elementor had an update waiting; WordPress then said "Plugin already installed" and installed nothing, with exit code 0. The importer now drops that state right after the load.
2. **Elementor prints into WP-CLI's output.** Its PHP deprecation notices and its own error log appear on stdout, so "give me a number" returned several lines and a check failed with "integer expression expected". Reads that need one value now skip plugins and take the last all-digit line. The same bug had to be fixed in three places (the restore drill, `wp-site-recover`, the importer).
3. **A bind mount keeps the directory it started with.** A wipe that deleted and recreated `uploads` left the running container writing into a deleted folder. Wipes now empty directories in place.
4. **`open_basedir` silently disabled the must-use plugins.** The PHP test suite passed because it does not use the real `php.ini`; the rehearsal's "harden" phase found it.
5. **`cp -a dir/. .` copies the directory's own owner and mode.** It changed `wp-content` to the image's owner, and root without capabilities could no longer create `wp-content/upgrade`: every plugin install of the next import failed.
6. **freshclam needs two capabilities and real memory.** It died with "initgroups() failed" until `SETUID` and `SETGID` were allowed, and with signal 9 until it had the memory of a scan.
7. **A stalled download cost 937 seconds.** A 20 MB zip arrived at 10 KB/s for five minutes. The report now ends with a timing section, and WP-CLI's downloads are cached between runs.
8. **Editing a script while someone runs it breaks the run.** Bash reads scripts by byte offset; an edit mid-run made it execute half a line.
9. **The first A/B run timed a page nginx rate-limits.** `/wp-login.php` is allowed ten requests a minute; the script timed it twelve times in a row, 11 got a 429, and the row read "no answer". It now times `/wp-admin/admin-ajax.php`, and the test's stand-in site applies the same limit `[verified: 2026-10-31]`.
10. **A reload does not empty a page cache, and nginx makes only the last directory of a cache path.** `nginx -t` failed on `mkdir` until the base play made `/var/cache/nginx` `[stated: 2026-10-30]`. The files of the cache are deleted by the plugin-change verb; nginx treats a missing file as a miss.
11. **Two PHP traps in the plugin-request code, found by reading the tests against it before PHP was installed.** An array key made only of digits becomes an integer (so a request id of digits only lost its answer), and a regex `$` matches before a final newline (`\z` does not). The PHP suite then passed on its first run with PHP installed `[verified: 18 scenarios at the time; 21 in tests/php/run_all.php now]`.

12. **`WP_CONTENT_DIR` does not exist yet when `wp-config.php` runs.** The migration switch's first version defined the backups path from that constant. WordPress defines it in `wp-settings.php`, after `wp-config.php`, and an undefined constant is a fatal error in PHP 8: every request of the site returned 500 the moment the switch was turned on. The tests had passed because they read the rendered text. The path now uses `__DIR__`, and a test fails if the constant is used before `wp-settings.php` `[stated: the operator's 500, 2026-10-31]`.
13. **A notice that never goes away teaches people to ignore notices.** The "Plugin requests" box on the admin pages listed every request of the last 60 days, so "installed" stayed after the plugin was on. It now lists an installed plugin or theme only while it is there and switched off, one line per name and kind, and drops a declined, failed or expired answer after 30 days.

The common thread: the failures were in the seams between our code and someone else's (WordPress, Docker, Elementor, ClamAV, the network). That is why the rehearsal script exists and why it is trusted over the unit tests for anything that touches those seams.

## 8. The threats we considered, and what each defence costs

| Threat | Defence | What it costs |
|---|---|---|
| A friend's password is stolen | two-factor required, login throttle, generic login message | a friend with a lost phone needs you |
| A plugin is vulnerable or malicious | no admin installs; wordpress.org only; checksums; vulnerability feed; scans | friends cannot self-serve |
| A friend's import file is hostile | options that name code reset before WordPress runs; skip-plugins on every early call; held code; unsafe paths refused | slower, stricter imports |
| A site is compromised | per-site network, database, quota and limits; no outbound SMTP; read-only code | a compromise stays inside one site |
| A bad update | snapshot first, probe, automatic rollback, majors held | a night's delay on majors |
| Data loss | nightly snapshots, hourly freshness check, monthly restore drill, 14-day archive on delete, protected pre-import snapshot | disk space for the quotas |
| Someone fakes an approval | signed, one-time, report-bound tokens; phone can only approve a clean import or deny | no override from a phone |
| A plugin needs to write outside `uploads/` | a per-site switch, one narrow mount, no PHP execution from it, and an administrator-only gate for the files it makes | one switch per such plugin; some plugins are declined |
| Mistakes by the operator | everything destructive asks for the name, twice for delete; tombstones; `--commit` only after the verifier ends clean | more typing |

What we did *not* defend against, on purpose: a determined attacker with a zero-day in WordPress core before the image updates; physical loss of the Dell without an off-machine backup (the Proxmox job and your PBS copy are the answer, outside this service); and a friend who is deliberately hostile.

## 9. What to read and research next

Questions worth your time, roughly in order of how much they would change decisions:

1. **How do managed WordPress hosts isolate customers?** Look up how Kinsta uses LXC containers per site and how WP Engine handles file permissions; compare with our container-per-site. The comparison shows what a stack-per-site does not give you (for example kernel-level isolation beyond Docker's).
2. **What does a WordPress "virtual patch" do?** Wordfence and Patchstack sell rules that block an exploit before the plugin is fixed. We only *report* vulnerabilities; consider whether a Cloudflare WAF custom rule per known CVE is worth it.
3. **SMTP deliverability.** SPF, DKIM and DMARC for a sender subdomain; why mail from a new domain lands in spam at first; what Resend's reputation gives you.
4. **Backups beyond the box.** The 3-2-1 rule applied to your setup: what is on the Dell, what is on the ASUS PBS, and what is off-site.
5. **Control-page design.** A local web page that is a thin layer over `wp-ctl` (`scripts/wp_ctl.py`): what a debug mode should show (the exact commands and their full output), and whether it belongs on the laptop or the HP.
6. **Container hardening.** Rootless Docker, user namespaces, seccomp profiles: what each would add to what we have (`cap_drop: ALL`, `no-new-privileges`, read-only root, per-site network).

Sources used in the work (`[looked up]`, in the retired worklist, section 9, for the older ones (`git show 25e68b6:docs/handoff_wp_hosting_temp.md`)): the Wordfence Intelligence v3 documentation (endpoint `GET /api/intelligence/v3/vulnerabilities/production`, Bearer key, no parameters, rate limit 429), the Two Factor plugin's source (filters `two_factor_providers`, `two_factor_is_required_for_user`; REST namespace `two-factor/1.0`), the WP-CLI handbook (plugin zips are cached by slug and version under the cache directory), the ClamAV image tags on Docker Hub.

## 10. Glossary and where things live

- **Hold / live / stopped / closed:** the four states of a site (section 5.5).
- **Tombstone:** a `closed` entry in `wp_sites`; the name, port and quota id are never reused.
- **Held code:** plugins, themes or must-use plugins from an archive that are not on wordpress.org; kept in the work directory until you approve.
- **Snapshot:** a backup directory named by its UTC time; tags `pre-update`, `pre-import` and `final` are kept on their own rules.
- **Must-use plugin:** a plugin WordPress loads without activation, from `WPMU_PLUGIN_DIR`.
- **Page cache:** nginx's copy of an anonymous page (`fastcgi_cache`), kept `wp_page_cache_s` seconds; section 5.13.
- **Plugin or theme request:** what Install in a site's wp-admin writes down; the operator approves it (installed inactive) or denies it; sections 5.2 and 5.7.
- **Export gate:** the must-use plugin and the two nginx locations that serve a migration export to an administrator only; section 5.14.
- **Probe port:** `127.0.0.1:8081` on the VM; answers even while a site is in maintenance, so tools can test before reopening.
- **Mutation check:** breaking the code on purpose to see whether a test notices.

| Thing | Where |
|---|---|
| The site list and every setting | `host_vars/dellwp/vars.yml` (`wp_sites`, `wp_site_defaults`), image pins in `vars/wp.yml` |
| The engine (what builds a site) | the registry, through `scripts/wp_ctl.py` and `scripts/wp_site_provision.py`; `tasks/wp_sites_common.yml` and `templates/wp/` for what every site shares |
| The tools you run on the VM | `templates/wp/wp_site_*.sh.j2`, `scripts/wp_site_import.py` (deployed to `/usr/local/sbin`, library in `/usr/local/lib/wp-tools`) |
| The operator's tool | `wp-ctl` on `dellwp`; it took over every verb of the retired `scripts/wp_sites.py` (create, import, report, approve, reset, modify, stop, start, delete, password) |
| Must-use plugins | `templates/wp/mu/`, tests in `tests/php/` |
| Phone approvals | `scripts/wp_approvals.py`, `scripts/wp_approval_listener.py` |
| Plugin and theme requests | `templates/wp/mu/50-wp-hosting-plugin-requests.php` (the site's side), `scripts/wp_plugin_requests.py` (`wp-plugin-requests` on the VM), `scripts/wp_vulnfeed.py` (`check-plugin`, `check-theme`), the `wp-ctl` verbs `plugin-requests`, `plugin-requests-collect` and `plugin-request-decide` |
| The migration export | `ai1wm_export` in `scripts/wp_site_model.py` and `host_vars/dellwp/vars.yml`, the mount in `templates/wp/site_compose.yml.j2`, the constant in `templates/wp/site_wp-config.php.j2`, the gate in `templates/wp/mu/60-wp-hosting-download-gate.php` and `templates/wp/site_nginx.conf.j2` |
| Speed and its measurement | `scripts/wp_perf_measure.sh`, `wp_perf_profile.sh`, `wp_traffic_sim.sh`, `wp_ab_test.sh`, `templates/wp/mu/40-wp-hosting-timing.php`, the cache in `tasks/wp_nginx_base_common.yml` and `templates/wp/site_nginx.conf.j2` |
| Which playbook a pin or a change reaches | `scripts/pin_consumers.py` (the `# used by:` line above each Renovate pin; `--since ORIG_HEAD` after a pull) |
| Scans and the feed | `scripts/wp_vulnfeed.py`, `templates/wp/wp_clamscan.sh.j2`, `dellwp/wp_scans_dellwp.yml` |
| The rehearsal | the `*_rehearsal_dellwp.sh` scripts in `scripts/`; the lifecycle rehearsal was removed with `wp_sites.py` |
| The facts and the plays | `docs/ansible_reference.md`, the `dellwp/` section; the open items: `docs/homeserver_handbook.md` §19 |

## 11. The control app (`aopenpanel`): design and decisions

A private operator tool: one user, reachable only over WireGuard, that behaves like the control panel of a small hosting provider. It takes a `.wpress` file and imports it; creates and configures a site; shows every site; moves a site between `live`, `hold` and `stopped` or deletes it; all from the browser. The CLI is the fallback and is the same code path: `ssh dellwp sudo wp-ctl <verb> ...`. Unlike Bloomy it has one user, not the public; its risk is privilege (it can stop and delete sites as root on `dellwp`), and if it is down nothing is lost, the CLI still works. Where it runs and how it is built: handbook §13; the plays: `ansible_reference.md`.

**Decisions `[stated]`.**
- Reachable over WireGuard only; no Cloudflare, no public port. That also lifts the 100 MB proxied-upload limit that would have ruled out a web upload of a 290 MB `.wpress`.
- It lives in the private-services VM on the AOpen (`aopenpanel`, VM 304), beside the watcher, ntfy, Uptime Kuma, AdGuard Home and the cold Vaultwarden copy. Only the ThinkPad opens the page, over HTTPS with the fleet's wildcard certificate (so the session cookie can be `Secure`).
- **No Ansible inside the app.** The repo builds the platform (the VM, Docker and the image pins, the nginx base, the data disk and quotas, the tools and the dispatcher, the timers and scans); the app runs the tenants, and the repo does not reach into the sites.
- **Stateless app, no database.** A site is a record (`/srv/wp/sites/<name>/site.json`, plus a `closed` record that outlives the directory: a closed site's name, port and quota id are never reused), written only by the provisioning tool on `dellwp`; the VM's backups cover it. The audit is an append-only file on `dellwp` (`chattr +a` took on the VM's filesystem `[verified]`) written by the dispatcher, JSON lines (`intent` before a change, then `result` or `refused`, each with time, verb, site, caller, arguments, never a secret), also sent to the journal as `wp-ctl-audit` and so to Loki. The app cannot edit what the callee wrote. Job output is a transient systemd unit whose log the page tails, so a job survives a closed browser. Login is a password and a TOTP code (root-only files delivered by the platform play); the session is a signed cookie of 3 hours with no server-side store (a stolen cookie cannot be revoked before it expires except by rotating the signing key; accepted for one user on WireGuard); forms carry a signed CSRF token.
- One container, one process group, no Redis, queue, Celery or hosting panel. Python, Flask with Jinja under gunicorn, `MAX_CONTENT_LENGTH` set explicitly, a read-only root filesystem, the container capped at 512 MB. The strict content-security policy allows a script only on the import page.

**The shape.** Browser → (WireGuard) → the control VM's one container → one ssh key, forced to `sudo -n /usr/local/sbin/wp-ctl` with `restrict`, for a dedicated `wpctl` user whose only sudoers entry is `wp-ctl` → `wp-ctl` on `dellwp` (root-owned) reads one JSON line on stdin, checks the verb against an allowlist, validates every argument, writes the audit line and runs the `wp-site-*` tool. Verbs that carry bytes (the upload, the password) read them raw after the first line, never inside the JSON. A compromised container can send only allowlisted verbs: no shell, no vault password (the sites' secrets derive from `dellwp_wp_master_secret`, which is already on `dellwp`), no other host.

**The verbs** (`VERBS` in `scripts/wp_ctl.py`): reads (`list`, `show`, `check-name`, `jobs`, `job-status`, `job-log`, `audit-tail`, `import-report`, `import-list`, `vuln-check`, `users`, `plugins`, `plugin-requests`, `backups`); jobs (`backup`, `delete`, `create`, `apply`, `set`, `ensure-state`, `import-run`, `import-approve`, `import-rescan`, `import-new`, `import-reset`, `restore`, `update-check`, `update-apply`); secret verbs (`password`, `install`); the stream `import-receive`; inline `import-discard`, `plugin-set` (activate, deactivate or delete one plugin, then clear the page cache), `plugin-requests-collect` and `plugin-request-decide`. Unknown verbs, unknown keys, wrong types and odd characters are refused. A site name is a hostname label; a name is not a tombstone and not on a reserved list. Each verb has its limits in one table, `wp_ctl_limits` in `host_vars/dellwp/vars.yml` (a time limit for every job and stream, plus `memory_mb` and `tasks`; the numbers are proposals, to be tuned from a real import and backup). A job request that carries a `req` is idempotent: the same request again returns the job it started. The dispatcher takes a lock per site.

**What needs a typed name.** `delete` is typed twice. `restore` is typed once, and the panel and `wp-ctl` both check it. `live` needs the site's name (a stopped site comes back on `hold`, never straight to `live`). Moving to `stopped` takes a final backup first (a failed backup stops the move) and the panel asks for the name (the panel checks it; `wp-ctl` is not sent the name). A live site may be stopped directly.

**What is typed once and what is edited later.** The name (also the subdomain: `<name>.<wp_domain>`, no custom domains) is typed once at `create` and never edited. `set` changes: `update_policy` (`auto`, `notify`, `freeze`), the limits (`quota_mb`, `backup_quota_mb`, `php_mem_mb`, `db_mem_mb`, `upload_mb`; PHP and DB memory restart the stack, about ten seconds offline), the hardening switches (`allow_xmlrpc`, `allow_app_passwords`, `allow_user_enum`), `mail_mode` (`log`, `send`; `send` needs the Resend key), `cart_fragments` (`normal`, `cache`; section 5.13), `ai1wm_export` (section 5.14) and the two info fields `label` and `note`. The WordPress title and the administrator's email live in WordPress's own database; the panel does not edit them. The provisioning tool refuses a change when the sum of all sites no longer fits the VM. A new site always starts on `hold`, with WordPress installed or empty and waiting for a `.wpress`.

**The pages.** Sites, Site (address, state, settings, details), the site's **Plugins** page (the list from WP-CLI with no plugin code run, so a plugin that breaks the site is still listed; activate, deactivate, and delete as two steps with the plugin's name typed), the site's **Backups and restore** page (section 5.4), **Requests** (waiting and decided plugin and theme requests; Approve, Deny, and Refresh now; a request with a known vulnerability or no vulnerability check is approved on a second page with the plugin's name typed), Import, Jobs and Audit. On a phone the pages sit behind one Menu button that closes on a second press or a tap outside; it is a hidden checkbox and its labels, because the content-security policy allows no script outside Import.

**The password verb** is a browser form: the password passes through the app's memory to the tool's stdin and is never logged, never put in an argument list and never written to the audit (the line records the user, not the value).

**What a compromise can do.** The container: allowlisted verbs only. A stolen session cookie: the same verbs for up to 3 hours, from a WireGuard peer. `dellwp` itself: everything, as before. It cannot read or rewrite the audit, change the tools (root-owned, deployed only by the platform play) or reach the vault.

## 12. Appendix: the control app's click-through checklist (the operator's hands, a browser, then dellwp)

Use two throwaway sites, `paneltest2` and `paneltest3` (the name of a closed site may be used again; its port and quota id never are, so `paneltest1` is no obstacle). Why two: an import needs an EMPTY site on hold, and installing WordPress makes a site non-empty, so `paneltest2` gets WordPress and the replace, `paneltest3` stays empty for the plain import. For the import you need one small real `.wpress` (export a throwaway WordPress with All-in-One WP Migration, a few MB). Tick a step only when you saw the stated result; write down anything that looks wrong, odd or slow, even if the step passed.

The later steps are also scripted: `bash scripts/wp_checklist_dellwp.sh` takes a throwaway site through its whole life, asks you at each manual step and checks dellwp's state after each. Its run on 2026-10-31 passed 70 checks, failed one (a wrong grep in the script itself, since fixed) and skipped the import steps `[stated: the operator's pasted output]`.

**A. The look (5 minutes).** 1. Log in with the password and a fresh code. 2. Open Sites, Import, Jobs, Audit: the bar marks the page you are on, the tables have badges, nothing overflows. 3. Switch the operating system to dark mode and reload: readable, buttons still stand out. 4. Narrow the window to a phone's width (or use the browser's device view): the settings rows stack, no sideways scrolling of the page.

**B. Create and install.** 1. *Sites* then *New site*: name `paneltest2`, label `test two`. Expect the job page; it reloads itself every 3 seconds while the job runs and shows a green `done, exit 0`. 2. The site page shows it on `hold`. 3. *Install WordPress*: a title, an administrator name and email, and a password of 14 characters or more typed twice. Expect "WordPress installed" and the tool's text. 4. Try it again on the same site: expect a refusal in plain words (the tool refuses a site that has WordPress), not a crash. 5. Repeat 1 for `paneltest3` and do NOT install it.

**C. Password.** 1. *Set a user's password* with the administrator from B and a new password typed twice: "Password changed". 2. Two different passwords: refused, "Nothing was changed". 3. A password of 13 characters: refused by the form or the page. 4. A user that does not exist: the tool's own message is shown, and the password is nowhere on the page.

**D. Settings.** 1. Change the note and the label. 2. Set PHP memory to 640, wait for the job, then back to 512 (it restarts the stack: about ten seconds offline). 3. Type 600 into *Upload size* (the limit is 512): the browser stops the form. That is the form's own check; wp-ctl's check is the real one and is covered by the offline tests, so there is nothing more to try by hand here.

**E. States (on `paneltest2`).** 1. hold to live with the wrong name typed: refused. With the right name: the job passes. 2. Open `https://paneltest2.example.com/` (the public path through the tunnel; `wp_state_rehearsal_dellwp.sh --with-live` never ran, so this is its first look): WordPress answers. 3. live to hold: the public page stops answering as before (hold). 4. hold to stopped: it takes a final backup (the job log says so) and visitors get a 503. 5. The site page offers only *hold* now; stopped to hold works.

**F. Import (the upload and the pipeline).** 1. *Import*: choose the `.wpress`; the name box fills itself; *Upload*. Expect a progress bar and "Uploaded …: N bytes, sha256 …. dellwp reported the same count and hash." 2. Upload the same name again: refused, "already in the intake directory" (and the first file is still there: see the dellwp check below). 3. *Plan or run*: site `paneltest3`, the file, *Plan only*. Expect a job whose log is the plan; nothing is written. 4. Run the same with *Import into an empty site on hold*. Expect a job that takes minutes; then *Import report* of `paneltest3` shows the report. 5. *Reset*: type `paneltest3`; the tables and copied files go; the report page says there is no report. 6. Run the import again, then *Approve*: leave the box unticked if the report is clean; if it lists failures, read them, and tick only if you accept them. 7. *Replace* on `paneltest2` (it has data): choose *Replace*, type `paneltest2`, run. Then *Reset* it (type the name) and check that the old WordPress is back (log in to its admin with the password from B). 8. Cut an upload off: start uploading the file again under a new name and close the tab at about half; within a few seconds the file must not be in the intake (the dellwp check below).

**G. Delete.** 1. `paneltest3` and `paneltest2`: move each to stopped, then *Delete…*, type the name, then the page shows what goes and asks again: type it again. 2. Both disappear from Sites; the jobs are `done, exit 0`. 3. A site that is not stopped shows "Only a stopped site can be deleted" and no form.

**H. What to check on dellwp afterwards (read-only, one paste).**

```
ssh dellwp 'bash -s' <<'EOF'
W=/usr/local/sbin/wp-ctl
sudo -n $W list | head -c 2500; echo
sudo -n $W jobs | head -c 3000; echo
sudo -n $W audit-tail lines=40 | head -c 6000; echo
sudo -n ls -la /srv/wp/intake 2>&1 | head -20
sudo -n ls /srv/wp/tombstones 2>&1 | tail -8
EOF
```

The intake path is `wp_data_mount` + `/intake` `[inferred]`; the audit should show `caller: panel` for every action and no password anywhere; the intake should hold no `.recv-*` leftovers and no file from the cut-off upload; `list` should show neither test site; the tombstones should have two new dated files. Paste the output and any error page text (never a password).

---

## Appendix A. The playbooks and the timers

*Copied from the Ansible reference of the original repository (the `dellwp/` section).*

The hosting service for friends' sites. A site is NOT a playbook: it is a record, `/srv/wp/sites/<name>/site.json`, made and changed by the tools below; the sites play installs the tools and what every site needs around it.
The explanation is `docs/wordpress_hosting.md`; the control app (`aopenpanel`) asks the `wp-ctl` dispatcher here, over ssh with a key forced to that command.

| Playbook | Purpose | Schedule |
|---|---|---|
| `wp_data_disk_dellwp.yml` | Wrapper → `tasks/wp_data_disk_common.yml`: an ext4 filesystem with project quotas at `/srv/wp` (sites, backups, intake, scratch) | one-off |
| `nginx_base_dellwp.yml` | nginx on `127.0.0.1` only, and the base every site's vhost builds on (`tasks/wp_nginx_base_common.yml`): the login limiter, the page-cache zone and its skip maps, the mini-cart maps, the timed log format. Run it before the sites play, or `nginx -t` fails on an unknown zone | one-off |
| `cloudflared_dellwp.yml` | The connector for the WordPress tunnel (`wordpress-dell`): it carries every name under the domain that has no explicit record to the nginx base | one-off (systemd-managed) |
| `cloudflared_check_dellwp.yml` | Wrapper → `tasks/cloudflared_check_common.yml`: connector active and ready, nginx answering on its loopback port | every 5 min |
| `wp_sites_dellwp.yml` | The tools (`wp-site-cli`, `wp-site-ext`, `wp-site-backup`, `wp-site-update`, `wp-site-import`, `wp-site-provision`, ... in `templates/wp/`), the dispatcher access (`tasks/wp_ctl_common.yml`: the `wpctl` account, its one sudoers entry), the must-use plugins (login throttle, two-factor, mail, timing, plugin and theme requests, and the download gate for migration exports), the timers and what each site needs. **Run it untagged and twice** (the second run is `changed=0`); `--tags wp_tools,wp_ctl` updates the tools and the dispatcher, but the per-site `include_tasks` may be skipped under tags | one-off (re-run after a tool change) |
| `wp_scans_dellwp.yml` | ClamAV (the signatures directory, the image, a daily refresh) and the Wordfence vulnerability feed (key, state, daily update-and-check) | ClamAV refresh 02:40, feed 04:20 |
| `wp_approvals_dellwp.yml` | Approvals by phone for imports and for plugin and theme requests: the signing key, the two ntfy logins, the listener service (it hands a token to `wp-site-import` or `wp-plugin-requests` by its prefix) and the plugin request collection timer (`tasks/wp_approvals_common.yml`). Run the sites play first | one-off |

The timers the sites play installs (the calendars are variables in `host_vars/dellwp/vars.yml`): site cron every 5 min (`wp_cron_calendar`), the nightly update at 02:00 (`wp_update_calendar`), the nightly backup at 03:15 (`wp_backup_calendar`), the backup-age check hourly, the disk alert every 10 min, the archive purge at 05:15 every day, the restore drill on the 1st at 05:30, the ClamAV refresh at 02:40, the vulnerability feed at 04:20 and the collection of plugin and theme requests every 15 min (`wp_plugin_requests_interval_min`; the control page's Refresh button collects at once). A backup is skipped as a success while a freeze flag exists. The tools' rules are in `templates/wp/`; the registry and the rules for a site are `scripts/wp_site_model.py`.


---

## Appendix B. Where it ran, how big it was, and how it was backed up

*Copied from the handbook of the original repository (the VM table, the Dell and AOpen sections, the backup table).*

**The VMs involved** (the sizes are the declared ones):

| VM | Host | VM ID | RAM | Cores | Disk | WireGuard IP | Role |
|----|------|-------|-----|-------|------|--------------|------|
| `dellwp` | Dell | 102 | 3GB | 2 (CPU limit 2, low weight) | 25GB + 40GB data (`/srv/wp`) | 10.8.0.41 | WordPress hosting, limited to two sites |
| `aopenpanel` | AOpen | 304 | 3GB | 2 | 35GB | 10.8.0.68 | The private-services VM: the WordPress control app, the watcher (blackbox probes, its own Prometheus and Alertmanager), the independent ntfy and its tunnel connector, Uptime Kuma, AdGuard Home, and a cold Vaultwarden (§11, §13) |

- **`dellwp` (102)**: WordPress hosting for friends' sites, limited to two sites, with its own MariaDB per site. A site is a record (`/srv/wp/sites/<name>/site.json`), not a playbook. Behind the `wordpress-dell` tunnel. 3 GB (two sites at the default sizes plus the play's RAM reserve fill it exactly, so the sites play refuses a third), 2 cores
  capped at 2 with a low CPU weight so a broken-into site cannot starve the shop, 25 GB + a 40 GB data disk at `/srv/wp` with project quotas. How it works: `wordpress_hosting.md`.

**Isolation.** Each VM group has its own bridge (`proxmox_vm_bridges`), so a broken-into WordPress site shares no segment with the shop. The host routes between the bridges and `wg0` (IPv4 forwarding, masquerade out through `vmbr0`, no NAT into the tunnel) and drops everything from the VM

**What backs it up** (the columns are: what, where it is made, where the copies go, when):

| What | Where it is made | Copies | When |
|---|---|---|---|
| The Dell's VMs `dellapps`, `dellstaging`, `dellwp` | Proxmox `vzdump` job `dell-vms-nightly` | Proxmox Backup Server on the ASUS (7 daily, 4 weekly, 3 monthly), replicated by pull to `aopenpbs` | Nightly 04:00 (+07); prune 02:00; the replica pulls at 06:00, verifies Monday 03:00 and collects garbage Sunday 04:00 |
| WordPress sites (each site's dump and files) | `dellwp`, nightly per site (7 daily and 3 weekly) | inside the VM backup above | Nightly |

**The AOpen side** (the restore drill and the control app):

- **The restore drill** (on the host, `restore_drill_aopen.yml`). Tuesday and Wednesday 12:00 it restores the newest replicated backup of `dellapps` and of `dellwp` (not `dellstaging`: staging holds test data) into the spare VM from the read-only `pbs-replica` storage, boots it
  - *The WordPress control app.* nginx with TLS in front of one container (Flask under gunicorn) that reaches `dellwp`'s `wp-ctl` dispatcher over ssh with a key forced to that command. It holds no state; the login is a password and an
    authenticator code; the strict CSP allows a script only on the import page. The behaviour is in `wordpress_hosting.md`.

---

## Appendix C. Restore and rebuild

*Copied from the restore runbook (section 5) and the disaster-recovery document of the original repository.*

**Status:** the nightly snapshots and the restore are exercised by `scripts/wp_recover_rehearsal_dellwp.sh` and by the monthly restore drill (the 1st at 05:30: one random site that has a good backup is restored
into a scratch copy with a throwaway database). No site with a friend's real content has been recovered yet `[inferred]`. The behaviour is explained in `wordpress_hosting.md` (section 5.4).

**What a snapshot is.** `wp-site-backup` makes one per site per night (03:15): a database dump (`gzip -t` checked, with its completion marker and a `wp_options` table) plus `wp-content` hard-linked against the
previous snapshot. 7 daily and 3 weekly are kept; the `pre-update` snapshot (2 kept) and the `final` snapshot taken when a site is stopped (1 kept) are tagged and pruned on their own; the `pre-import` snapshot is never pruned while its import waits.
They live on the VM (`/srv/wp/backups/<site>`), protected by the Proxmox backup of the VM (section 3) and nothing else: **there is no copy outside those backups, by decision.**

### Put a site back to a snapshot (in place, with an undo)

```bash
ssh -t dellwp sudo wp-site-recover <site> latest          # root-only tool in /usr/local/sbin [verified]; or a stamp such as 20261030T200000Z; asks you to type the site's name
```

`wp-site-recover` replaces the database and `wp-content` with the snapshot's and puts core back to the recorded version (checksums verified), inside one transaction: the maintenance flag goes up, a dump of the
current database and the current `wp-content` are moved aside as undo material in the site's own `.recover-undo`, the snapshot is loaded, WordPress must boot and have users, then the flag comes down. If
anything fails after the undo material is made, it is put back. `--no-undo` skips the undo (a database too broken to dump); `--yes` skips the typed name (the update job uses it to roll back a bad update). It does
not take a backup first: take one with `wp-site-backup --force` if the current state matters.

### A whole site is gone, or the VM is

If the site's directory is gone but the VM is fine, the site is a record (`site.json`) plus snapshots: provision it again with the same name through the app or `wp-site-provision`, then recover from the newest
snapshot. If the VM is gone, restore the VM first (section 3): the sites come back with it. If the PBS copies are gone too, the sites are lost; the friend's own copy is the last resort.

---

### Rebuilding the VM from the Proxmox backups

3. **Restore the VMs from the PBS** [the restore path is verified by the weekly drill into a spare VM; on a real rebuilt host unrehearsed]: `qmrestore` of the newest `dellapps` and `dellwp` backups from `pbs-asus` (`dellstaging` holds test data: restore it too if you want it, or rebuild it as in step 5) (if the ASUS is also lost, from the AOpen's `pbs-replica`; `scripts/proxmox_restore_test.sh` shows the commands, but a real restore keeps the network card). Then `proxmox_vms_delloptiplex3040.yml` to bring the declared settings (memory, cores, start order) back in line.
**Rehearsal, 2026-11-01** [verified: `evidence/gameday_2026-11-01_timings.log`]: a nested Proxmox node inside an AOpen VM, not the Dell's hardware. Steps and durations: install-to-login 587 s from the second start (the step started twice); dellapps boot: two failed attempts (exit 255, 2 s and 3 s), then 150 s; restore of dellwp: 4148 s (69.1 min); node back up: 1 s; dellwp boot: two failed attempts (exit 255, 4 s and 3 s), then 74 s for the guest agent. The cause of the failed boots is not recorded. The script that ran the steps is not stored in the repo [stated].
Single-disk concentration: the Dell's one SSD carries all three VMs, so a failed disk is the shop, staging and every WordPress site at once; the PBS backups are what protect it (`homeserver_handbook.md` section 12).

---

## Appendix D. Open items at retirement

*Copied from the handbook's open-items section (the WordPress entries only).*

**WordPress hosting** (`wordpress_hosting.md`)
- Harden and replace phases passed. The scripted checklist (`scripts/wp_checklist_dellwp.sh`) took a throwaway site through creation, WordPress, live, two-factor with its QR code and authenticator set-up, `mail_mode` switched to send and back (the configuration only: a delivered mail was not checked), the three switches, the mini-cart cache and a plugin request, stop and delete on 2026-10-31: 70 passed, 1 failed (a wrong grep in the script, fixed), 2 skipped (the import steps) `[stated: the operator's pasted output]`. The click-through of the control app's pages (`wordpress_hosting.md` §12) is a separate list.
- Follow-ups from the speed work `[stated: 2026-10-31]`: a `cart_fragments: limited` mode (WooCommerce's own filter for the script data), a re-run of the visitor simulator after a few weeks of real traffic, and the optional OPcache gauges (`wp_php_timing: 1` briefly) to see the interned-strings buffer with free space. The plugin-request flow has run once on a throwaway site; a friend's first real request is its next proof.
- Backlog `[stated: 2026-10-30]`: an ntfy message after three failed mails, WordPress update mails to ntfy, a download retry, and a Renovate rule for the Two Factor plugin.
- **The control app's backlog** (design: `wordpress_hosting.md` §11; the app is live and in use):
  1. Run the click-through checklist (`wordpress_hosting.md` §12) and fix what it finds; the later batches have run on a host only for the upload.
  2. Refuse a truncated upload at `dellwp`: `wp-site-import receive` cannot tell a cut-off stream from a finished file, so the panel removes the file afterwards. Better, an optional `bytes` argument on `import-receive` that `wp-ctl` and the tool check (touches `scripts/wp_ctl.py`, `scripts/wp_site_import.py`, their tests and mutants, then the sites play with `--tags wp_tools,wp_ctl`).
  3. The numbers in `wp_ctl_limits` are proposals: tune `memory_mb` per verb from a real import and backup (`systemctl status wpjob-<id>`).
  4. Unproven on a host: the job limits being enforced, two requests at once, a site with a friend's real content, the public path through the tunnel.
  5. Look: the job log is reloaded whole every 3 seconds (no streaming); a favicon needs `img-src` in the policy.
  6. Login and session hardening ideas: an idle timeout shorter than 3 hours; a journal line (and so Loki) for every login and failed login; an ntfy notice on a login from a new address.
  7. Leftover test sites on `dellwp` may remain (`provtest2`, `rehearsal7`); look at `wp-ctl list`.
- **2026-11-01 05:30:** the first monthly WordPress restore drill ran on a VM with no site, so it had nothing to restore and passed without testing a restore (`templates/wp/wp_restore_drill.sh.j2`); the first run against a real site is pending `[stated: 2026-11-03]`. **2026-11-08 05:15:** the first real purge must have removed the `wptest` archive `[stated]`.

**WordPress pending proofs** (each waits for a site, a date or a release)
- With a site on `update_policy: auto`: the first 02:00 `wp-update@<site>.timer` run by itself, a real core MINOR update, the php fatal-error detection (the forced-failure rehearsal bypasses it), the `notify` policy on
  the host, and the major-core notice.
- `wp-site-password` and the redaction in `wp-site-install` were never run on the host after the fix.
- Renovate's managers for `vars/docker.yml`, `vars/cloudflared.yml` and `vars/wp.yml` were tested offline only; the first moby, cloudflared or wp-cli release proves them.

---

## Appendix E. Lessons from the incident log

*Copied from the original repository's lessons file (entries 346, 347, 350, 351, 352, 388 and 395). Each entry ends with a pointer to a numbered principle in a document that is not part of this archive.*

**346.** WordPress runtime traps found by real runs [stated: the WordPress hosting handoff, 2026-10-25]. A friend's cached update transient made `wp plugin
install` print "already installed" and install nothing. Elementor prints PHP notices into WP-CLI's stdout, so a count read with plugins loaded is several
lines (reads that must be one value use `--skip-plugins --skip-themes` and take the last all-digit line). A bind mount keeps the directory it started
with, so deleting and recreating `uploads` left the container writing into a deleted one. `open_basedir` made `is_dir()` false for the must-use directory
and WordPress loaded none of the plugins in silence (one variable now feeds the mount, `WPMU_PLUGIN_DIR` and `open_basedir`). `cp -a dir/. .` copies the
directory's own owner and mode. freshclam switches user even to root and needs `SETUID` and `SETGID`, and testing a new database needs the memory of a scan
(512 MB was killed). Each is a single fact; no principle follows.

**347.** Tests and checks that passed for the wrong reason [stated: the WordPress, open-items handoffs, 2026-10]. A renderer that turned a list into a
string made a "clean" check vacuous; mutation checks (break the code on purpose, in a copy) found it and about thirty more. A manual test that checks only
HTTP 400 passes for the wrong reason: assert the error body too. A failed step must name itself (a verifier failure that scrolled away was unnamed). A
flaky network is part of the environment (a 937 s import: a download stalled at 10 KB/s). `Errors behavior: Continue` hid an ext4 `EIO` on the
ThinkCentre's data disk for two months, because no check read the superblock error count.
See `homeserver_principles.md` P98 and P107.

**350.** WordPress speed work and plugin requests [stated: the operator's runs on dellwp, 2026-10-30 and 2026-10-31]. A reload of nginx does not empty a
`fastcgi_cache`, because its entries are files on disk; the plugin-change verb deletes them (nginx treats a missing file as a miss), and nginx makes only
the last directory of a cache path, so `nginx -t` failed until the play made `/var/cache/nginx`. WooCommerce marks its mini-cart answer `no-store`, so
caching it needs `fastcgi_ignore_headers` in a location of its own, while the ordinary PHP location keeps obeying those headers. The first A/B run of
`php.ini` settings timed `/wp-login.php` twelve times in a row: nginx allows that page ten requests a minute, 11 of 12 got a 429 and the row read "no
answer"; the script now times `/wp-admin/admin-ajax.php`, and the test's stand-in site applies the same limit. Offline, before PHP was installed, reading the
tests against the new PHP found two traps (an array key made only of digits becomes an integer, and a regex `$` matches before a final newline).
See `homeserver_principles.md` P123.

**351.** A constant in a rendered `wp-config.php` runs in a fixed order [stated: the operator's runs on dellwp, 2026-10-31]. The migration export switch defined its
backups path from `WP_CONTENT_DIR`; WordPress defines that constant in `wp-settings.php`, after `wp-config.php`, and an undefined constant is a fatal error in
PHP 8, so every request of the site returned 500 when the switch was turned on. Every offline test passed, because they read the rendered text and never ran it.
The path now uses `__DIR__`, and a test fails if the constant appears before `wp-settings.php`. The same work added two rules [inferred, not tested on a host]: Docker makes a missing
mount point as a root-owned directory, so a mount inside a plugin's directory must wait until the plugin is installed (the switch now counts only while the
directory exists, and approving the plugin applies the site). A request notice that is never cleared teaches people to ignore it [stated: the operator saw it stay]. The plugin's own export
was also read: its Download button is a plain link to the file, so a folder that holds whole-site exports needs a gate before it is served.
See `homeserver_principles.md` P98.

**352.** A test suite that nothing runs rots, and a fixture that reads the real host passes only where it was written [verified: the suites run on the laptop, 2026-10-31]. `tests/test_thinkpad_backup.py` failed
20 of 29 tests (18 failures and 2 errors) on the operator's laptop. The commit that added `/etc/wireguard` to the backup (`8bfecc5`) left the tests behind, and the script read a hardcoded `/etc/wireguard`, which the tests had only survived on a
machine where that directory did not exist `[inferred]`; the laptop has a root-only one `[verified: the tests' own error]`. Nothing ran the file (it is not in the CI gate), so it stayed red until a new test needed it. `ETC_WG` is now overridable like
`NM_DIR`, the fixture points it at a path that does not exist, and a new test puts a real directory in the archive. The same evening the gate was red on `main` itself: `test_wp_panel.py` listed the control app's routes
and the merged restore page had added three, and a key pattern `[a-z_]+` cannot match `ai1wm_export`; the merge had not been run through the gate. Mutation checks (break the code, see the new test fail) were what showed the
repaired tests could fail at all.
See `homeserver_principles.md` P124.

**388.** A script shut a VM down for a shrink, and the play that was meant to follow could not run [verified: 2026-11-13]. The Dell's VM play refuses to shrink a running VM, so the cutover script shut `dellwp` down first and then ran the play; the play failed on its first Proxmox call with "Failed to import the required Python library (proxmoxer)", because the control node's venv had been built on 2026-10-29 with Python 3.14.8 and `/usr/bin/python3` was 3.15.2 by then (the venv's `python3` links to it, and the packages sat in `lib/python3.14`). WordPress stayed down until the operator started the VM. The script checked its own stage's preconditions and nothing that a later stage needed. It now imports `proxmoxer` with the play's interpreter before it shuts anything down, and the rebuild command (`python3 -m venv --clear --system-site-packages ~/.venvs/ansible-proxmox`, then `pip install -r requirements-controlnode.txt`) is in `ansible_reference.md`. A venv is tied to the interpreter that built it: a system upgrade breaks it without any change in the repo. See `homeserver_principles.md` P141.

**395.** The `dellwp` restore drill failed once and then raised "has not succeeded since: 16 days ago" every day, because a new unit was not on its list of units that may fail [verified: 2026-11-11 to 11-13]. The drill restores the newest backup into a spare VM with no network and fails on any failed unit not listed in `expected_failed`. The WordPress vulnerability feed (`wp-vulnfeed`) fetches from the internet, was added after the last good drill (2026-10-28), ran at the restored copy's boot and failed; the one-time FAILED message of 2026-11-11 12:12 was easy to miss, and the daily overdue message only said how old the last success was. `wp-vulnfeed` is now on the list (`host_vars/aopen/vars.yml`) and the drill was started by hand after the play was re-run; `systemctl start restore-drill@dellwp.service` returned with no error `[stated]`, and the new result file was not read. The drill is a consumer of every unit added to a VM it restores: adding a timer that needs the network means adding it to this list in the same change. See `homeserver_principles.md` P134.

