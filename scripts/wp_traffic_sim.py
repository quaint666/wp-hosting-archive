#!/usr/bin/env python3
"""Simulate visitors on one WordPress site and report what they cost the origin. Standard library only. Run it ON dellwp (scripts/wp_traffic_sim.sh pipes it there over ssh), so the
requests go to nginx on the loopback port with the site's Host header: no HTTP leaves the VM and nothing outside it is involved.

    sudo python3 - <site> [--minutes M] [--concurrency C] [--think S] [--pages N] [--returning F] [--seed N]   < scripts/wp_traffic_sim.py

What a simulated visitor does (the model, and the one assumption in it):
  - arrives as an anonymous visitor with no cookies and no cart, reads N pages (the home page most often, then pages the home page links to), thinking S seconds on average between pages;
  - for each page: the HTML, then (unless the visitor has been before: --returning is the share who have, and a browser keeps static files for a month) the css, js and images the page names,
    on one keep-alive connection;
  - ASSUMPTION: when the page's HTML loads WooCommerce's cart-fragments script, the browser sends WooCommerce's own "refresh the mini cart" POST (/?wc-ajax=get_refreshed_fragments) after every page
    view. That is what the script does for a visitor with no cart, and one such POST is in the site's real access log; the simulation does not prove it happens for every visitor.
It never logs in, never adds to a cart and never touches wp-admin. The requests carry the User-Agent "wp-traffic-sim/1" so they can be told apart in the access log.

Reported: per kind of request and page-cache verdict (X-Cache-Status), the count and the time to first byte and to the whole body as the client saw them; how much of the page views the cache
answered; what the origin spent building the uncached ones, and from that the page views per second one PHP worker (the container is limited to 1 CPU) could sustain at this mix; and, when
docker is there, the database's write counters before and after (writes per uncached request) and the busiest CPU and memory the two containers showed. WHAT IT DOES NOT SHOW: real visitors'
behaviour, Cloudflare, or the network between a visitor and the apartment; the numbers are for this mix and this site's content."""
import argparse
import collections
import gzip
import http.client
import random
import re
import statistics
import subprocess
import sys
import threading
import time

UA = "wp-traffic-sim/1 (load model; origin only)"
STATIC = re.compile(r"\.(js|css|png|jpe?g|gif|webp|avif|svg|woff2?|ttf|otf|eot|map)$", re.I)
SKIP_PAGE = re.compile(r"/(wp-|cart|basket|checkout|my-account|feed|xmlrpc|\?)|[?#]|/page/|\.(xml|txt|php|json)$", re.I)
ATTR = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
FRAGMENTS = "/?wc-ajax=get_refreshed_fragments"


def site_target(site, vhost_dir):
    """The server_name and the loopback port the site's vhost listens on (the same two lines scripts/wp_perf_measure.sh reads)."""
    text = open("%s/wp_site_%s.conf" % (vhost_dir, site)).read()
    host = re.search(r"^\s*server_name\s+([^\s;]+);", text, re.M).group(1)
    port = re.search(r"^\s*listen\s+127\.0\.0\.1:(\d+);", text, re.M).group(1)
    return host, int(port)


class Client:
    """One visitor's connection (keep-alive) and what it records."""

    def __init__(self, target, host, sink):
        self.host, self.sink = host, sink
        self.conn = http.client.HTTPConnection(target[0], target[1], timeout=60)

    def go(self, kind, method, path, body=None):
        headers = {"Host": self.host, "User-Agent": UA, "Accept-Encoding": "gzip", "X-Forwarded-Proto": "https", "Accept": "*/*"}
        if body is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Content-Length": str(len(body))})
        t0 = time.perf_counter()
        try:
            self.conn.request(method, path, body=body, headers=headers)
            r = self.conn.getresponse()
            t1 = time.perf_counter()
            data = r.read()
            t2 = time.perf_counter()
        except (OSError, http.client.HTTPException) as e:
            self.conn.close()
            self.sink.add(kind, method, path, 0, "error", 0.0, 0.0, 0)
            return 0, b""
        cache = r.getheader("X-Cache-Status") or "-"
        if r.getheader("Content-Encoding") == "gzip":
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        self.sink.add(kind, method, path, r.status, cache, (t1 - t0) * 1000, (t2 - t0) * 1000, len(data))
        return r.status, data


class Sink:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []

    def add(self, kind, method, path, status, cache, ttfb, total, size):
        with self.lock:
            self.rows.append((kind, method, path, status, cache, ttfb, total, size))


def links(html, host):
    """(pages, assets): same-site paths the HTML names. A page is a pretty URL with no query; an asset is a static file (its query, the ?ver=, kept)."""
    pages, assets = [], []
    for ref in ATTR.findall(html):
        m = re.match(r"^(?:https?:)?//([^/]+)(/.*)?$", ref)
        if m:
            if m.group(1).lower() != host.lower():
                continue
            ref = m.group(2) or "/"
        elif not ref.startswith("/") or ref.startswith("//"):
            continue
        bare = ref.split("?")[0]
        if STATIC.search(bare):
            if ref not in assets:
                assets.append(ref)
        elif not SKIP_PAGE.search(ref) and ref not in pages:
            pages.append(ref)
    return pages, assets


def visitor(args, target, host, pages, sink, rng):
    c = Client(target, host, sink)
    returning = rng.random() < args.returning
    path = "/"
    for view in range(args.pages):
        status, data = c.go("page", "GET", path)
        html = data.decode("utf-8", "replace") if status == 200 else ""
        _, assets = links(html, host)
        if not returning:
            for a in assets[:args.assets]:
                c.go("static", "GET", a)
        if "wc-cart-fragments" in html:
            c.go("fragments", "POST", FRAGMENTS, body="time=%d" % int(time.time() * 1000))
        if view + 1 < args.pages:
            time.sleep(min(rng.expovariate(1.0 / args.think), args.think * 5) if args.think > 0 else 0)
            path = "/" if rng.random() < 0.35 or not pages else rng.choice(pages)
    c.conn.close()


def docker(*argv):
    try:
        return subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def db_counters(site):
    sql = "SHOW GLOBAL STATUS WHERE Variable_name IN ('Com_insert','Com_update','Com_delete','Com_select','Questions','Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests')"
    out = subprocess.run(["docker", "exec", "-i", "wp_%s_db" % site.replace("-", "_"), "sh", "-c", 'MYSQL_PWD="$MARIADB_PASSWORD" exec mariadb -u"$MARIADB_USER" "$MARIADB_DATABASE" -N -B'],
                         input=sql, capture_output=True, text=True, timeout=20).stdout if docker("version", "--format", "x") else ""
    return {k: int(v) for k, v in (line.split("\t") for line in out.splitlines() if "\t" in line)}


def sampler(site, stop, seen):
    names = ["wp_%s_%s" % (site.replace("-", "_"), k) for k in ("php", "db")]
    while not stop.wait(8):
        for line in docker("stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}}", *names).splitlines():
            parts = line.split()
            if len(parts) >= 3:
                cpu = float(parts[1].rstrip("%") or 0)
                if cpu >= seen.get(parts[0], (0, ""))[0]:
                    seen[parts[0]] = (cpu, parts[2])


def pct(sorted_values, p):
    return sorted_values[int(p * (len(sorted_values) - 1))]


def report(args, rows, elapsed, before, after, seen):
    out = []
    p = out.append
    n_pages = sum(1 for r in rows if r[0] == "page")
    p("")
    p("==== the simulated traffic, %d virtual visitors at a time for %.0f s: %d requests (%.1f a second), %d page views" % (args.concurrency, elapsed, len(rows), len(rows) / elapsed, n_pages))
    groups = collections.defaultdict(list)
    for kind, method, path, status, cache, ttfb, total, size in rows:
        groups[(kind, cache if kind != "static" else "-", -1 if status == 0 else status if status >= 500 or status == 429 else 0)].append((ttfb, total))
    p("  %-10s %-10s %-9s %6s %12s %10s %10s %10s" % ("kind", "cache", "status", "n", "ttfb median", "ttfb p95", "total med", "total p95"))
    for (kind, cache, status), v in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        t, d = sorted(x[0] for x in v), sorted(x[1] for x in v)
        p("  %-10s %-10s %-9s %6d %9.1f ms %7.1f ms %7.1f ms %7.1f ms" % (kind, cache, "no answer" if status == -1 else status or "ok", len(v), statistics.median(t), pct(t, .95), statistics.median(d), pct(d, .95)))
    pages = [r for r in rows if r[0] == "page"]
    hits = sum(1 for r in pages if r[4] in ("HIT", "UPDATING", "STALE"))
    p("")
    p("  page views answered from the cache (HIT, STALE, UPDATING): %d of %d (%.0f %%); built by php (MISS, EXPIRED, BYPASS, none): %d" % (hits, len(pages), 100.0 * hits / max(1, len(pages)), len(pages) - hits))
    built = [r for r in rows if r[0] != "static" and r[4] not in ("HIT", "STALE", "UPDATING") and r[3] and r[3] < 500]
    spent = sum(r[6] for r in built) / 1000.0
    p("  what php spent building those %d requests: %.1f s in all (%.1f s of work a second, against the container's limit of 1.0)" % (len(built), spent, spent / elapsed))
    frag = [r for r in rows if r[0] == "fragments"]
    if frag:
        p("  the mini-cart refresh (POST) alone: %d requests, %.0f ms each, %.1f s in all: %.0f %% of everything php built" % (len(frag), statistics.mean(r[6] for r in frag), sum(r[6] for r in frag) / 1000.0, 100.0 * sum(r[6] for r in frag) / 1000.0 / max(spent, 0.001)))
    if pages and spent:
        per_view = spent / len(pages)
        p("  php work per page view at this mix: %.0f ms; one php worker on one CPU could therefore sustain about %.1f page views a second (%.0f an hour)" % (per_view * 1000, 1.0 / per_view, 3600.0 / per_view))
        cached_only = [r for r in built if r[0] != "fragments"]
        if frag and cached_only:
            alt = sum(r[6] for r in cached_only) / 1000.0 / len(pages)
            p("  without the mini-cart refresh: %.0f ms of php per page view, about %.1f page views a second" % (alt * 1000, 1.0 / alt if alt else float("inf")))
    errors = [r for r in rows if r[3] == 0 or r[3] >= 500]
    p("  errors (no answer, or 5xx): %d%s" % (len(errors), "" if not errors else "  <- look at these: " + ", ".join(sorted({"%s %s" % (r[1], r[2][:40]) for r in errors})[:5])))
    if before and after:
        d = {k: after.get(k, 0) - before.get(k, 0) for k in before}
        w = d.get("Com_insert", 0) + d.get("Com_update", 0) + d.get("Com_delete", 0)
        p("  database during the run: %d selects, %d inserts, %d updates, %d deletes; %.1f writes per php-built request (the page cache and static files write nothing)" %
          (d.get("Com_select", 0), d.get("Com_insert", 0), d.get("Com_update", 0), d.get("Com_delete", 0), w / max(1, len(built))))
    if seen:
        p("  busiest the containers were (sampled every 8 s): " + "; ".join("%s cpu %.0f %%, memory %s" % (k, v[0], v[1]) for k, v in sorted(seen.items())))
    print("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("site")
    ap.add_argument("--minutes", type=float, default=2.0, help="how long visitors keep arriving (the page cache keeps a page 300 s: run 7 or more to see pages expire; default 2)")
    ap.add_argument("--concurrency", type=int, default=3, help="visitors on the site at the same moment (default 3)")
    ap.add_argument("--think", type=float, default=3.0, help="average seconds a visitor reads a page (default 3)")
    ap.add_argument("--pages", type=int, default=4, help="page views per visit (default 4)")
    ap.add_argument("--assets", type=int, default=15, help="at most this many static files per page for a first-time visitor (default 15)")
    ap.add_argument("--returning", type=float, default=0.5, help="share of visitors whose browser already holds the static files (default 0.5)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--vhost-dir", default="/etc/nginx/conf.d")
    ap.add_argument("--target", help="host:port to send to instead of the site's loopback port (for testing the simulator itself)")
    ap.add_argument("--host", help="the Host header to send instead of the site's server_name (with --target)")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z]([a-z0-9]|-[a-z0-9]){0,22}", args.site):
        sys.exit("not a site name: %s" % args.site)
    if not (0 < args.minutes <= 30 and 1 <= args.concurrency <= 8 and 1 <= args.pages <= 20 and 0 <= args.returning <= 1):
        sys.exit("out of range: --minutes 0 to 30, --concurrency 1 to 8, --pages 1 to 20, --returning 0 to 1")
    if args.target:
        h, _, pt = args.target.partition(":")
        target, host = (h, int(pt)), args.host or "localhost"
    else:
        host, port = site_target(args.site, args.vhost_dir)
        target = ("127.0.0.1", port)
    print("simulating visitors on %s (%s:%d) for %.1f minutes, %d at a time" % (host, target[0], target[1], args.minutes, args.concurrency))

    sink = Sink()
    probe = Client(target, host, Sink())
    status, data = probe.go("probe", "GET", "/")
    if status != 200:
        sys.exit("the home page answered %s, not 200: nothing to simulate (is the site live?)" % (status or "nothing"))
    pages, _ = links(data.decode("utf-8", "replace"), host)
    print("the home page links to %d pages of its own; the visitors read them and the home page%s" % (len(pages), "" if "wc-cart-fragments" not in data.decode("utf-8", "replace") else "; it loads the cart-fragments script"))
    before = db_counters(args.site)
    stop, seen = threading.Event(), {}
    threading.Thread(target=sampler, args=(args.site, stop, seen), daemon=True).start()
    deadline, t0 = time.time() + args.minutes * 60, time.time()

    def worker(i):
        rng = random.Random(args.seed * 1000 + i)
        while time.time() < deadline:
            visitor(args, target, host, pages, sink, rng)
            time.sleep(rng.uniform(0.2, 2.0))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    report(args, sink.rows, time.time() - t0, before, db_counters(args.site), seen)


if __name__ == "__main__":
    main()
