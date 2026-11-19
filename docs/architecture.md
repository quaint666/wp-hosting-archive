# Architecture (C4 views)

Four levels of the [C4 model](https://c4model.com/) plus the import flow, drawn as plain Mermaid flowcharts (GitHub renders them; Mermaid's own `C4` syntax is still marked experimental, so it is not used here). Everything is as of the last state of the service, 2026-11-15. The reasoning behind each choice is in [`wordpress_hosting.md`](wordpress_hosting.md); the one-line decisions are in [`decisions.md`](decisions.md).

Names: `dellwp` is the VM that runs the sites, `aopenpanel` is the VM that runs the control app, the "operator" is the one person who runs everything, a "friend" is the owner of a site.

## Level 1: system context

Who uses the system and which outside services it depends on.

```mermaid
flowchart LR
    visitor(["Visitor<br/>reads a friend's site"])
    friend(["Friend<br/>site owner: wp-admin only,<br/>two-factor required"])
    operator(["Operator<br/>creates, imports, approves,<br/>restores, deletes"])

    subgraph sys["WordPress hosting (the system)"]
        hosting["Sites + control app + tools<br/>declared as code, deployed with Ansible"]
    end

    cf["Cloudflare<br/>TLS, free WAF, tunnel<br/>(no inbound port at home)"]
    wporg["wordpress.org<br/>plugin and theme zips,<br/>core checksums, search"]
    wf["Wordfence Intelligence<br/>vulnerability feed"]
    resend["Resend<br/>mail over HTTPS API"]
    ntfy["Independent ntfy<br/>phone notifications"]
    pbs["Proxmox Backup Server<br/>VM backups"]

    visitor -->|HTTPS| cf
    friend -->|HTTPS| cf
    cf -->|tunnel| hosting
    operator -->|browser over WireGuard| hosting
    operator -->|phone: approve or deny| ntfy
    ntfy -->|signed token| hosting
    hosting -->|fetch by slug, verify checksums| wporg
    hosting -->|daily index| wf
    hosting -->|send mode only| resend
    hosting -->|notifications| ntfy
    hosting -->|nightly VM backup| pbs
```

## Level 2: containers (what runs where)

Two VMs on two Proxmox hosts. The sites never share a VM with the shop, and the control app never runs on the VM it controls.

```mermaid
flowchart TB
    cf["Cloudflare tunnel"]
    operator(["Operator's laptop<br/>(WireGuard peer)"])
    phone(["Operator's phone"])

    subgraph dell["Dell Proxmox host"]
        subgraph wp["VM dellwp (3 GB, CPU-capped, low weight)"]
            cfd["cloudflared<br/>connector"]
            nginx["nginx on 127.0.0.1<br/>vhost per site, rate limits,<br/>page cache, 503 pages"]
            sites["Per-site Docker stacks<br/>php-fpm + MariaDB<br/>(one systemd unit each)"]
            ctl["wp-ctl dispatcher<br/>allowlist, audit, lock, jobs"]
            tools["wp-site-* tools<br/>provision, backup, update,<br/>import, recover, scan"]
            lis["Approval listener<br/>reads one ntfy topic"]
            timers["systemd timers<br/>backup, update, drill,<br/>scans, requests"]
            disk[("/srv/wp data disk<br/>ext4 project quotas:<br/>sites, backups, intake")]
        end
    end

    subgraph aopen["AOpen Proxmox host"]
        subgraph panelvm["VM aopenpanel"]
            pnginx["nginx + TLS<br/>(fleet wildcard certificate)"]
            app["Control app container<br/>Flask + gunicorn, stateless,<br/>read-only root, 512 MB"]
            nt["Independent ntfy"]
        end
    end

    asus[("PBS primary<br/>+ replica on the AOpen")]

    cf --> cfd --> nginx --> sites
    operator -->|HTTPS| pnginx --> app
    app -->|"ssh, key forced to<br/>sudo wp-ctl"| ctl
    ctl --> tools --> sites
    tools --> disk
    timers --> tools
    sites --- disk
    tools -->|notify| nt
    nt --> phone
    phone -->|token| nt
    lis -->|reads topic| nt
    lis --> tools
    dell -.->|vzdump nightly| asus
```

## Level 3a: components of a site stack

What one friend's site is made of, and which parts PHP may write to.

```mermaid
flowchart LR
    ng["nginx vhost<br/>login rate limit, page cache,<br/>export download gate"] -->|loopback port| php

    subgraph stack["One site: Docker Compose, own network"]
        php["php-fpm container<br/>hardened php.ini, open_basedir,<br/>cap_drop ALL, no-new-privileges"]
        db[("MariaDB container<br/>own schema and user,<br/>not published")]
        php --> db
    end

    code["Code: core, plugins, themes<br/>root-owned, mounted READ-ONLY<br/>DISALLOW_FILE_MODS on"]
    up["uploads/<br/>the only writable path"]
    mu["Must-use plugins (outside wp-content,<br/>mounted read-only)<br/>login throttle, two-factor, mail,<br/>plugin requests, download gate, timing"]
    ai["ai1wm storage mount<br/>(per-site switch, only when<br/>the plugin is installed)"]

    php --- code
    php --- up
    php --- mu
    php -.-|optional| ai
```

## Level 3b: components of the control chain

Why a compromised control app can do only what the allowlist says.

```mermaid
flowchart TB
    b(["Browser<br/>(WireGuard only)"]) -->|"password + TOTP,<br/>signed 3 h cookie, CSRF token"| app

    subgraph panel["aopenpanel: control app container"]
        app["Pages: sites, site, plugins, backups,<br/>requests, import, jobs, audit"]
    end

    app -->|"one ssh key, command=sudo -n wp-ctl,<br/>restrict"| ssh["wpctl user<br/>sudoers: wp-ctl only"]

    subgraph vm["dellwp"]
        ssh --> disp["wp-ctl (root-owned)<br/>1 JSON line on stdin<br/>verb allowlist, argument validation,<br/>per-verb limits, per-site lock"]
        disp --> aud[("Audit file, append-only<br/>chattr +a: intent, then result")]
        disp --> jobs["Jobs: transient systemd units<br/>(survive a closed browser)"]
        disp --> t["wp-site-* tools"]
        t --> reg[("Registry: site.json per site,<br/>tombstone for a closed one")]
    end
```

## Level 3c: the import pipeline

A friend's `.wpress` file is untrusted input: the database names code that WordPress will `include`. The pipeline, in order (details in section 5.6 of the main document).

```mermaid
flowchart TD
    p1["1 Preflight on the operator's laptop<br/>package.json + plugin headers only;<br/>GO or NO-GO"] --> p2["2 Send over ssh, SHA-256 checked<br/>into a root-only intake with its own quota"]
    p2 --> p3["3 Unpack with a policy<br/>no caches, no PHP in uploads,<br/>no hidden files, names validated"]
    p3 --> p4["4 Load the SQL as the site's own user<br/>(table prefix swapped)"]
    p4 --> p5["5 Reset the options that name code<br/>active_plugins, template, stylesheet;<br/>drop the stale update state"]
    p5 --> p6["6 Reinstall from wordpress.org<br/>non-wordpress.org code is held back"]
    p6 --> p7["7 WP-CLI steps<br/>db upgrade, search-replace, flush"]
    p7 --> p8["8 Scan<br/>patterns, checksums, ClamAV,<br/>vulnerability feed"]
    p8 --> p9["9 Report, first backup,<br/>phone notification; site stays on hold"]
    p9 --> d{"10 Operator decides"}
    d -->|approve: only if clean from a phone| ok["Held code copied in, root-owned, read-only;<br/>intake deleted"]
    d -->|deny or reset| undo["Undo from the manifest +<br/>wp-content tree snapshot"]
```

## Site states

```mermaid
stateDiagram-v2
    [*] --> hold: create
    hold --> live: operator, name typed
    live --> hold
    live --> stopped: final backup first
    hold --> stopped: final backup first
    stopped --> hold
    stopped --> closed: delete, name typed twice
    closed --> [*]: tombstone stays,<br/>name, port, quota id never reused
```

## Deployment view

```mermaid
flowchart LR
    subgraph home["Dell Proxmox host"]
        d1["VM dellwp<br/>3 GB, 25 GB + 40 GB data disk"]
        d2["Shop VMs<br/>(not part of this system)"]
    end
    subgraph apt["AOpen Proxmox host"]
        a1["VM aopenpanel<br/>control app, independent ntfy,<br/>watcher, Uptime Kuma"]
        a2["PBS replica VM"]
    end
    bk[("PBS primary")]
    cfe["Cloudflare edge"]

    cfe ---|tunnel wordpress-dell| d1
    a1 ---|ssh over WireGuard| d1
    d1 -.->|vzdump| bk -.->|pull| a2
```
