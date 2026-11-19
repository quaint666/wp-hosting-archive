# Decisions

The decisions that shaped the service, one line each, with the reason and what it cost. They are drawn from the main document (the section number is given) and were made between 2026-10-13 and 2026-11-15. `[stated]` means the operator said it; the rest were reasoned and then tested where the main document says so.

The thesis behind all of them: **a hosting provider spends money and staff on being forgiving; this service spends code and tests on being strict.** With one operator, often away from the keyboard, every action has an undo, asks first, or is impossible.

| # | Decision | Why | Cost | Section |
|---|---|---|---|---|
| 1 | One VM for all WordPress, one Docker stack per site | A break-in on one friend's site must not reach another's database, nor the shop on the same hardware | RAM per stack, a hard cap on the number of sites (the play checks the budget before it changes anything) | 5.1 |
| 2 | Per-site secrets derived from a master secret, the site name and a revision | Rotating one site's secrets changes only that site | A master secret to protect | 5.1 |
| 3 | No plugin or theme installs through the web: `DISALLOW_FILE_MODS`, code read-only to PHP, install only from wordpress.org by slug | The commonest takeover is a vulnerable or malicious plugin; a stolen password then cannot add code | Friends cannot self-serve | 5.2 |
| 4 | A friend's Install press becomes a *request*; the operator approves it (installed inactive) or denies it. "Approve mode only, no auto" `[stated]` | Keeps the familiar screens without the risk | One decision per plugin | 5.2 |
| 5 | Deleting a plugin or theme stays with the operator `[stated]` | A delete-by-request path was dropped for simplicity | Operator action | 5.2 |
| 6 | Nightly update per site: snapshot first, update, check three things, roll back by itself on failure; major plugin updates held | No support staff: "update and hope" is unacceptable and "never update" gets sites hacked | A night's delay on majors | 5.3 |
| 7 | Backups hard-linked against the previous snapshot, a freshness check hourly, a monthly drill that restores a random site into a scratch copy and boots it | A backup that was never restored is a hope | Disk quota for snapshots | 5.4 |
| 8 | Restore in place with an undo; no way to skip the undo from the panel | The undo is what makes it safe to try | Slower restore | 5.4 |
| 9 | Delete is three steps: final backup, 14-day archive, removal; name typed twice; the name leaves a tombstone | A name is spent forever so a failed or mistaken operation is recoverable | More typing | 5.4, 5.5 |
| 10 | Four site states: `hold` (only the VM sees it), `live`, `stopped`, `closed` (tombstone); a new site starts on `hold` | Nothing goes public by accident | An extra step to go live | 5.5 |
| 11 | A `.wpress` import is untrusted input: reset the options that name code *before* WordPress runs, skip plugins on every early call, hold code that is not on wordpress.org, undo by manifest | The database names files WordPress will `include` | Slower, stricter imports | 5.6 |
| 12 | A phone can approve only a clean import or a clean plugin request, or deny. Tokens are HMAC-signed, one-time, expiring and bound to the exact report | A tap on a lock screen is the weakest form of consent, so it may only confirm the safe case | No override from a phone | 5.7 |
| 13 | Scans and the vulnerability feed are reported honestly: "NOT RUN" when the index is over three days old; a clean result proves nothing | False reassurance is worse than none | Operator judgement | 5.8 |
| 14 | Login: nginx rate limit plus a PHP throttle, one generic message, XML-RPC and user listing off, two-factor required for administrators (locked out if the component is missing) | The login page is what the internet attacks | A friend with a lost phone needs the operator | 5.9 |
| 15 | Mail through a must-use plugin calling Resend's HTTPS API; the VM cannot send SMTP at all | A compromised site cannot spam from the domain | A mail provider dependency | 5.9 |
| 16 | Our must-use plugins live outside `wp-content`, mounted read-only | A friend's archive brings its own `mu-plugins`; an import must not overwrite or shadow ours | `open_basedir` had to allow the path (found on the real VM) | 5.10 |
| 17 | Page cache in nginx (`fastcgi_cache`), no Redis `[stated]`; cache files deleted on plugin changes | Most of a request is PHP start-up, so the cure is not running PHP | Cache invalidation is by deleting files | 5.13 |
| 18 | `open_basedir` stays on `[stated]` | It is a security control; turning it off saved about 330 ms of CLI start-up | Slower uncached requests | 5.13 |
| 19 | Migration plugin export: one per-site switch, one narrow writable mount, an administrator-only download gate | Friends need to take their own backup; nothing else is opened | One switch per such plugin | 5.14 |
| 20 | The control app is reachable over WireGuard only, no Cloudflare, no public port `[stated]` | Lifts the 100 MB proxied-upload limit and removes the public surface | Only the laptop can open it | 11 |
| 21 | The app runs on a different VM and host from the sites (`aopenpanel`, not `dellwp`) `[stated]` | The controller must survive the thing it controls | A second VM to run | 11 |
| 22 | No Ansible inside the app: the repository builds the platform, the app runs the tenants | Keeps the app small and the privileges narrow | Two systems to understand | 11 |
| 23 | Stateless app, no database: a site is a record (`site.json`), written only by the provisioning tool; the audit is append-only (`chattr +a`) and written by the callee, never the caller | The app cannot edit the history it creates | The audit is on the same VM | 11 |
| 24 | One ssh key forced to `sudo -n wp-ctl` with `restrict`; a verb allowlist, argument validation, per-verb limits and a per-site lock | A compromised container can send only allowlisted verbs | Every new action is a new verb | 11 |
| 25 | Tests plus mutation checks plus real rehearsals; the rehearsal is trusted over the unit tests for anything at a seam | Most real bugs were in the seams between this code and WordPress, Docker, Elementor, ClamAV and the network | A lot of test code (about 1,600 cases at the end) | 5.11, 7 |
| 26 | Honest scope: a favour for friends, not a product; no SLA, one host, one disk, one residential link `[stated]` | Stated plainly so nobody expects more | Downtime is possible | 6 |

## Why it was retired

On 2026-11-19 the operator decided to retire the service so that the machine it ran on would do one job (host the shop's production and staging). The two remaining sites were simple blogs and favours, and go away with it. The code, design and lessons are kept here.
