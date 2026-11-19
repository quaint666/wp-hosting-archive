# WordPress hosting for friends, on a home server (archive)

A small, strict WordPress hosting service that ran on one VM in a home lab from October to November 2026: each friend's site is its own Docker stack, reached through a Cloudflare tunnel, with no plugin installs through the web, nightly backups that are really restored in a monthly drill, imports treated as untrusted input, and approvals from a phone that can only confirm the safe case. It was built and run by one person, declared as code (Ansible, Jinja templates, Python tools) and tested heavily.

**Status: retired and archived (decided 2026-11-19).** Nothing here is running. This repository is the code, the design and the lessons, kept because the engineering is worth reading. It is a read-only snapshot, not a project to deploy.

## Start here

| If you want | Read |
|---|---|
| The whole story, from the comparison with a hosting provider to the real bugs | [`docs/wordpress_hosting.md`](docs/wordpress_hosting.md) |
| The architecture as C4 diagrams (context, containers, components, the import pipeline, states, deployment) | [`docs/architecture.md`](docs/architecture.md) |
| The decisions, one line each, with their cost | [`docs/decisions.md`](docs/decisions.md) |
| A longer retrospective of the same service (part one of a three-part document) | [`docs/retrospective_part_one.md`](docs/retrospective_part_one.md) |

## What is in the code

| Path | What |
|---|---|
| `dellwp/` | The playbooks for the VM that ran the sites (nginx base, tunnel connector, data disk, sites, scans, approvals) |
| `tasks/wp_*.yml` | The shared task files those playbooks import |
| `templates/wp/` | Everything rendered for the VM: per-site Compose and nginx, the `wp-site-*` tools, the timers, the must-use plugins (login throttle, two-factor, mail, plugin requests, download gate, timing) |
| `scripts/wp_*` | The dispatcher `wp_ctl.py`, the provisioning and import tools, the approval listener, the vulnerability feed, rehearsal and measurement scripts |
| `app/`, `templates/wp_panel/`, `aopenvms/wp_panel_aopenvms.yml` | The control app (Flask) and the play that deployed it on its own VM |
| `tests/` | About 35 test suites and a mutation harness (`mutate_wp_ctl.py`) that breaks the code on purpose to see whether the tests notice |
| `host_vars/dellwp/vars.yml`, `vars/wp.yml` | The settings and the image and plugin pins |

## What is not here, and what that means

- This is a slice of a larger infrastructure repository. Its inventory, shared variables, other hosts' plays and encrypted vault files are not included, so **the tests and playbooks cannot run as they stand**. They are kept as reading material.
- The history of these files is preserved (commit messages and dates as they were). The author's e-mail address was replaced with a GitHub no-reply address.
- The text was sanitized before publication: the real domain is `example.com` and the login name is `admin`. Private addresses in the `10.8.0.0/24` WireGuard range are left as they were; they are not routable.
- The documents refer to files of the original repository (a handbook, an Ansible reference, a restore runbook) that are not part of this archive. Where it matters, the relevant text was copied into the appendices of the main document.

## Licence

MIT, see [`LICENSE`](LICENSE).
