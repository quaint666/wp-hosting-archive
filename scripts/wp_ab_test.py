#!/usr/bin/env python3
"""A/B test of php.ini settings on ONE live WordPress site, run on dellwp (scripts/wp_ab_test.sh starts it over ssh). Standard library only.

A is the site's php.ini as it is; B is the same file with the --set values changed. The phases run A B A B (--rounds 2), because one run of each says little: the second pair shows how much the SAME setting
differs from itself, and a difference is only called real when it is larger than that. In each phase the program writes the variant, restarts the site (systemctl restart wp-<site>: about ten seconds
offline), waits until it answers, empties the page cache and then measures only requests PHP has to build, from nginx on this VM's loopback (no network, Cloudflare or browser in it):
  cold    the first request after the restart (OPcache is empty: this is the compile cost)
  login   GET /wp-login.php          PHP, never cached
  json    GET /wp-json/              PHP, never cached
  pages   GET /?ab=<n>, one new address each time: a page-cache MISS that PHP builds
  frag    POST /?wc-ajax=get_refreshed_fragments with a cart cookie: PHP, never cached (WooCommerce's mini-cart)
The file is written in place (the same inode: the container's bind mount keeps working) and put back exactly as it was on every way out: the end, an error, Ctrl-C, a dropped ssh (SIGHUP) or SIGTERM. At
the end the program asks wp-site-provision whether php.ini is again what the repo renders, and says so loudly when it is not. Only php.ini keys are settable here; PHP-FPM's pool file is not touched.
What this does not show: real visitors, a busy site (one request at a time), or OPcache's own gauges (that is the timing plugin's job, wp_php_timing in host_vars/dellwp/vars.yml)."""
import argparse
import fcntl
import http.client
import os
import re
import signal
import statistics
import subprocess
import sys
import time

KEY_RE = re.compile(r"^[a-z][a-z0-9_.]{1,60}$")
VAL_RE = re.compile(r"^[A-Za-z0-9_.:/,+-]{1,60}$")
SITE_RE = re.compile(r"^[a-z]([a-z0-9]|-[a-z0-9]){0,22}$")
METRICS = (("cold", "first request after the restart"), ("login", "GET /wp-login.php"), ("json", "GET /wp-json/"), ("pages", "a page built by PHP (cache MISS)"), ("frag", "POST mini-cart refresh"))
MIN_REAL_CHANGE = 0.03          # a difference under 3 % is never called real, however quiet the noise
NOISE_FACTOR = 2.0              # and it must be this many times the difference the same setting showed against itself


class Stop(Exception):
    """A signal asked the program to end: the file is put back on the way out."""


def say(text):
    try:
        print(text, flush=True)
    except OSError:
        pass            # the ssh that reads this has gone: the work (and the restore) goes on


def parse_sets(items):
    sets, seen = [], set()
    for item in items:
        key, eq, val = item.partition("=")
        if not eq or not KEY_RE.fullmatch(key) or not VAL_RE.fullmatch(val):
            raise SystemExit("--set wants  php.ini.key=value  with a plain key and value, not %r" % item[:80])
        if key in seen:
            raise SystemExit("--set %s twice" % key)
        seen.add(key)
        sets.append((key, val))
    if not sets:
        raise SystemExit("nothing to test: give at least one --set key=value")
    return sets


def apply_sets(text, sets):
    """(new text, [(key, old value or None, new value)]). A key that is in the file has its line changed; one that is not is added at the end. A value equal to the present one is refused: there would be nothing to compare."""
    changes = []
    for key, val in sets:
        pattern = re.compile(r"^(\s*%s\s*=\s*)(.*?)(\s*)$" % re.escape(key), re.M)
        m = pattern.search(text)
        if m:
            if m.group(2) == val:
                raise SystemExit("%s is already %s in php.ini: nothing to compare" % (key, val))
            text = pattern.sub(lambda mm: mm.group(1) + val + mm.group(3), text, count=1)
            changes.append((key, m.group(2), val))
        else:
            text = text.rstrip("\n") + "\n%s = %s\n" % (key, val)
            changes.append((key, None, val))
    return text, changes


def pct(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(p / 100.0 * len(ordered) + 0.4999) - 1)))]


def judge(a_medians, b_medians):
    """(change as a fraction of A, noise as a fraction, verdict text) from the medians of each A phase and each B phase."""
    a, b = statistics.mean(a_medians), statistics.mean(b_medians)
    if a <= 0:
        return 0.0, 0.0, "nothing measured"
    change = (b - a) / a
    spread = lambda xs: (max(xs) - min(xs)) / statistics.mean(xs) if len(xs) > 1 and statistics.mean(xs) > 0 else 0.0
    noise = max(spread(a_medians), spread(b_medians))
    if abs(change) > max(MIN_REAL_CHANGE, NOISE_FACTOR * noise):
        return change, noise, "B is %.0f %% %s" % (abs(change) * 100, "slower" if change > 0 else "faster")
    return change, noise, "no difference beyond the noise"


class Site:
    """What the test does to one site, with every outside action a parameter so the tests can replace them."""

    def __init__(self, name, args, run=subprocess.run, sleep=time.sleep, clock=time.monotonic, client=None):
        self.name, self.args, self.run, self.sleep, self.clock = name, args, run, sleep, clock
        self.ini = os.path.join(args.sites_dir, name, "php.ini")
        self.vhost = os.path.join(args.vhost_dir, "wp_site_%s.conf" % name)
        self.client = client or self.http

    def target(self):
        text = open(self.vhost).read()
        host = re.search(r"^\s*server_name\s+([^\s;]+);", text, re.M).group(1)
        port = int(re.search(r"^\s*listen\s+127\.0\.0\.1:(\d+);", text, re.M).group(1))
        return host, port

    def http(self, method, path, headers=None, body=None):
        """(status, milliseconds to the first byte, X-Cache-Status). 0 for a request that got no answer."""
        host, port = self.target()
        h = {"Host": host, "X-Forwarded-Proto": "https", "User-Agent": "wp-ab-test/1", "Accept-Encoding": "identity"}
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        t0 = time.perf_counter()
        try:
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            t1 = time.perf_counter()
            r.read()
            return r.status, (t1 - t0) * 1000, r.getheader("X-Cache-Status") or "-"
        except (OSError, http.client.HTTPException):
            return 0, 0.0, "error"
        finally:
            conn.close()

    # ---- the file
    def read_ini(self):
        with open(self.ini) as f:
            return f.read()

    def write_ini(self, text):
        with open(self.ini, "r+") as f:          # in place: the container's bind mount names this file's inode
            f.seek(0)
            f.write(text)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())

    # ---- the site
    def restart(self):
        p = self.run(["systemctl", "restart", "wp-%s.service" % self.name], capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError("systemctl restart wp-%s failed: %s" % (self.name, (p.stderr or p.stdout or "").strip()[-200:]))

    def wait_ready(self):
        """Seconds until /wp-login.php answers 200 after a restart; RuntimeError after the limit."""
        t0 = self.clock()
        while self.clock() - t0 < self.args.ready_timeout_s:
            if self.client("GET", "/wp-login.php")[0] == 200:
                return self.clock() - t0
            self.sleep(2)
        raise RuntimeError("the site did not answer 200 on /wp-login.php within %d s after the restart" % self.args.ready_timeout_s)

    def flush_cache(self):
        n = 0
        for dirpath, _dirs, files in os.walk(self.args.cache_dir, followlinks=False):
            for f in files:
                try:
                    os.unlink(os.path.join(dirpath, f))
                    n += 1
                except OSError:
                    pass
        return n

    def measure(self, label):
        """One phase's numbers, after the site is up."""
        out = {"cold": [], "login": [], "json": [], "pages": [], "frag": [], "cache": {}, "errors": 0}

        def take(key, method, path, headers=None, body=None):
            status, ms, cache = self.client(method, path, headers, body)
            if status != 200:
                out["errors"] += 1
                return
            out[key].append(ms)
            out["cache"][cache] = out["cache"].get(cache, 0) + 1

        self.flush_cache()
        take("cold", "GET", "/wp-login.php")
        for _ in range(3):
            self.client("GET", "/wp-login.php")                      # warm-up, not counted
        for _ in range(self.args.requests):
            take("login", "GET", "/wp-login.php")
        for _ in range(max(3, self.args.requests // 2)):
            take("json", "GET", "/wp-json/")
        for i in range(self.args.requests):
            take("pages", "GET", "/?ab=%s-%d" % (label, i))
        for _ in range(self.args.requests):
            take("frag", "POST", "/?wc-ajax=get_refreshed_fragments", {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest",
                                                                      "Cookie": "woocommerce_items_in_cart=1; woocommerce_cart_hash=ab"}, "time=1")
        return out


def summarise(label, ready_s, m, out):
    parts = []
    for key, what in METRICS:
        v = m[key]
        parts.append("%s %s" % (key, ("%.0f ms (p95 %.0f)" % (statistics.median(v), pct(v, 95))) if v else "no answer"))
    out("  %-4s ready in %.0f s; %s%s" % (label, ready_s, "; ".join(parts), ("; %d requests failed" % m["errors"]) if m["errors"] else ""))


def run_test(args, out=say, site=None):
    sets = parse_sets(args.set)
    site = site or Site(args.site, args)
    original = site.read_ini()
    variant, changes = apply_sets(original, sets)
    lock = None
    if args.lock_file:
        lock = os.open(args.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock)
            raise SystemExit("another change to %s is running (its lock is held): try again when it has finished" % args.site)
    for key, old, new in changes:
        out("B changes %s: %s -> %s" % (key, old if old is not None else "(not in the file)", new))
    phases = []
    for r in range(args.rounds):
        phases += [("A%d" % (r + 1), "A"), ("B%d" % (r + 1), "B")]
    handlers = {}

    def stop(signum, _frame):
        raise Stop("signal %d" % signum)
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        handlers[s] = signal.signal(s, stop)
    results, failure = {}, None
    try:
        for label, kind in phases:
            site.write_ini(variant if kind == "B" else original)
            site.restart()
            ready = site.wait_ready()
            results[label] = site.measure(label)
            summarise(label, ready, results[label], out)
    except (Stop, RuntimeError, OSError, KeyboardInterrupt) as e:
        failure = "stopped early: %s" % e
    finally:
        # the way out, whatever happened: the original text back, the site restarted on it
        for s in handlers:
            signal.signal(s, signal.SIG_IGN)            # the restore below must not be cut short by a second signal
        problem = None
        try:
            site.write_ini(original)
            site.restart()
            out("  restored php.ini and restarted; the site answered after %.0f s" % site.wait_ready())
        except (RuntimeError, OSError) as e:
            problem = "COULD NOT CONFIRM THE RESTORE (%s): check %s and run: sudo wp-site-provision apply %s" % (e, site.ini, args.site)
        if problem:
            out(problem)
        if lock is not None:
            os.close(lock)
        for s, h in handlers.items():
            signal.signal(s, h)
    verdict = None if failure or problem else check_repo(site, args, out)
    if failure:
        out(failure)
    report(results, phases, out)
    return 0 if not failure and not problem and verdict else 1


def check_repo(site, args, out):
    """True when wp-site-provision says php.ini is what the repo renders."""
    try:
        p = site.run(["/usr/local/sbin/wp-site-provision", "render", args.site, "--diff"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        out("  could not ask wp-site-provision whether php.ini is back as the repo renders it (%s)" % e)
        return False
    line = next((l for l in (p.stdout or "").splitlines() if l.rstrip().endswith("/php.ini")), "")
    ok = line.split()[:1] == ["same"]
    out("  wp-site-provision on php.ini: %s" % (line.strip() or "no line for php.ini (%s)" % ((p.stderr or "").strip()[-100:] or "no output")))
    if not ok:
        out("  PHP.INI IS NOT WHAT THE REPO RENDERS: run  sudo wp-site-provision apply %s" % args.site)
    return ok


def report(results, phases, out):
    a_labels = [l for l, k in phases if k == "A" and l in results]
    b_labels = [l for l, k in phases if k == "B" and l in results]
    if not a_labels or not b_labels:
        out("\n==== not enough phases finished to compare")
        return
    out("\n==== A (php.ini as it is) against B (with the --set values), medians in ms; the change is B against A")
    out("  %-8s %s %s %7s %7s  %s" % ("", "".join("%8s" % l for l in a_labels), "".join("%8s" % l for l in b_labels), "change", "noise", "verdict"))
    for key, what in METRICS:
        a = [statistics.median(results[l][key]) for l in a_labels if results[l][key]]
        b = [statistics.median(results[l][key]) for l in b_labels if results[l][key]]
        if not a or not b:
            out("  %-8s no answer to compare  (%s)" % (key, what))
            continue
        change, noise, verdict = judge(a, b)
        out("  %-8s %s %s %+6.0f%% %6.0f%%  %s" % (key, "".join("%8.0f" % statistics.median(results[l][key]) if results[l][key] else "%8s" % "-" for l in a_labels),
                                                  "".join("%8.0f" % statistics.median(results[l][key]) if results[l][key] else "%8s" % "-" for l in b_labels), change * 100, noise * 100, verdict))
    tallies = {}
    for m in results.values():
        for k, n in m["cache"].items():
            tallies[k] = tallies.get(k, 0) + n
    out("  page cache seen by the measured requests: %s (MISS and BYPASS are PHP-built; a HIT would not test PHP)" % ", ".join("%s %d" % kv for kv in sorted(tallies.items())))
    out("  Read it: \"noise\" is how much the same setting differed from itself between its two phases. A change is called real only when it is over %.0f %% and over %.0f times the noise. One request at a time\n"
        "  on an otherwise idle site: it says what the setting does to a request, not what it does under load." % (MIN_REAL_CHANGE * 100, NOISE_FACTOR))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("site")
    ap.add_argument("--set", action="append", default=[], help="a php.ini key=value to put in B (repeatable), e.g. opcache.interned_strings_buffer=8")
    ap.add_argument("--rounds", type=int, default=2, help="how many times each of A and B runs, interleaved A B A B (default 2)")
    ap.add_argument("--requests", type=int, default=12, help="requests per measurement in a phase (default 12)")
    ap.add_argument("--ready-timeout-s", type=int, default=180)
    ap.add_argument("--sites-dir", default="/srv/wp/sites")
    ap.add_argument("--vhost-dir", default="/etc/nginx/conf.d")
    ap.add_argument("--cache-dir", default="/var/cache/nginx/wp_pages")
    ap.add_argument("--lock-file", default="", help="a lock to hold while the test runs (wp-ctl's lock for the site), so no job changes the site meanwhile")
    args = ap.parse_args(argv)
    if not SITE_RE.fullmatch(args.site):
        raise SystemExit("not a site name: %s" % args.site)
    if not (1 <= args.rounds <= 4 and 3 <= args.requests <= 60):
        raise SystemExit("out of range: --rounds 1 to 4, --requests 3 to 60")
    if os.geteuid() != 0:
        raise SystemExit("run as root (sudo): it restarts the site and writes its php.ini")
    return run_test(args)


if __name__ == "__main__":
    sys.exit(main())
