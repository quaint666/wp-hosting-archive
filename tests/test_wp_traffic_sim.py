#!/usr/bin/env python3
"""Offline tests of scripts/wp_traffic_sim.py (the visitor simulator that scripts/wp_traffic_sim.sh runs on dellwp): what it takes from a page, what a visitor sends, and the report.

A small server in this process stands in for WordPress behind nginx: a home page with links, static files, the cart-fragments script and a slow mini-cart POST, and an X-Cache-Status header
that is MISS the first time a path is asked for and HIT after. What this does not prove: anything about dellwp, nginx or WooCommerce themselves. Run: python3 -I tests/test_wp_traffic_sim.py"""
import gzip
import http.server
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "wp_traffic_sim.py"
sys.path.insert(0, str(HERE.parent / "scripts"))
import wp_traffic_sim as sim  # noqa: E402

HOME = ('<html><head><link rel="stylesheet" href="https://shop.test/wp-content/a.css?ver=1"><script src="/wp-includes/j.js"></script>'
        '<script id="wc-cart-fragments-js" src="/wp-content/plugins/woocommerce/cart-fragments.min.js"></script><img src="https://elsewhere.test/x.png"></head>'
        '<body><a href="https://shop.test/shop/">s</a><a href="/about/">a</a><a href="/cart/">c</a><a href="/wp-admin/">w</a><a href="/?s=x">q</a><a href="/blog/page/2/">p</a><a href="/feed/">f</a></body></html>')


class Server:
    def __init__(self, fragments=True):
        self.seen, self.log, self.lock = set(), [], threading.Lock()
        outer = self
        home = HOME if fragments else HOME.replace("wc-cart-fragments", "nothing")

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, body, cache=None):
                z = gzip.compress(body.encode())
                self.send_response(code)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(z)))
                if cache:
                    self.send_header("X-Cache-Status", cache)
                self.end_headers()
                self.wfile.write(z)

            def do_GET(self):
                with outer.lock:
                    outer.log.append(("GET", self.path, self.headers.get("Host"), self.headers.get("User-Agent")))
                    hit = self.path in outer.seen
                    outer.seen.add(self.path)
                if self.path.endswith((".js", ".png")) or ".css?" in self.path:
                    return self.reply(200, "x")
                self.reply(200 if self.path in ("/", "/shop/", "/about/") else 404, home, "HIT" if hit else "MISS")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with outer.lock:
                    outer.log.append(("POST", self.path, self.headers.get("X-Requested-With"), body.decode()[:5]))
                self.reply(200, "{}")

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Parsing(unittest.TestCase):
    def test_pages_and_assets_are_told_apart_and_only_the_sites_own_are_kept(self):
        pages, assets = sim.links(HOME, "shop.test")
        self.assertEqual(pages, ["/shop/", "/about/"], "no cart, admin, search, feed or paging link, nothing from another host")
        self.assertEqual(assets, ["/wp-content/a.css?ver=1", "/wp-includes/j.js", "/wp-content/plugins/woocommerce/cart-fragments.min.js"], "the ?ver= is kept; another host's image is not")

    def test_the_server_name_and_port_come_from_the_vhost(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "wp_site_alpha.conf").write_text("server {\n    listen 127.0.0.1:8080;\n    listen 127.0.0.1:8081;\n    server_name alpha.example.test;\n}\n")
            self.assertEqual(sim.site_target("alpha", d), ("alpha.example.test", 8080))


class Run(unittest.TestCase):
    def run_sim(self, server, *extra):
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "alpha", "--minutes", "0.06", "--concurrency", "2", "--think", "0.05", "--pages", "3", "--target", "127.0.0.1:%d" % server.port,
                            "--host", "shop.test", *extra], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

    def test_visitors_read_pages_fetch_assets_send_the_mini_cart_post_and_the_report_says_so(self):
        s = Server()
        try:
            out = self.run_sim(s, "--returning", "0")
        finally:
            s.close()
        gets = [x for x in s.log if x[0] == "GET"]
        posts = [x for x in s.log if x[0] == "POST"]
        self.assertTrue(posts and all(x[1] == "/?wc-ajax=get_refreshed_fragments" and x[2] == "XMLHttpRequest" and x[3] == "time=" for x in posts))
        self.assertTrue(all(x[2] == "shop.test" and x[3].startswith("wp-traffic-sim/") for x in gets), "every request names the host and the simulator")
        for forbidden in ("/wp-admin", "/wp-login", "/cart/", "/checkout", "?s=", "add-to-cart"):          # "/cart/" with its slash: the asset woocommerce/cart-fragments.min.js is a legitimate request
            self.assertFalse(any(forbidden in x[1] for x in s.log), forbidden)
        self.assertTrue(any(x[1].endswith(".js") for x in gets) and any(".css" in x[1] for x in gets), "a first-time visitor fetches the static files")
        for needle in ("kind", "fragments", "page", "static", "page views answered from the cache", "what php spent building", "the mini-cart refresh (POST) alone", "errors (no answer, or 5xx): 0"):
            self.assertIn(needle, out)

    def test_returning_visitors_skip_the_static_files_and_a_page_without_the_script_gets_no_post(self):
        s = Server(fragments=False)
        try:
            out = self.run_sim(s, "--returning", "1")
        finally:
            s.close()
        self.assertFalse(any(x[1].endswith((".js", ".png")) or ".css" in x[1] for x in s.log if x[0] == "GET" and x[1] not in ("/", "/shop/", "/about/")), "a returning visitor's browser has them")
        self.assertFalse([x for x in s.log if x[0] == "POST"])
        self.assertNotIn("mini-cart refresh", out)

    def test_a_site_that_does_not_answer_200_stops_the_run_with_a_reason(self):
        s = Server()
        s.close()
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "alpha", "--minutes", "0.05", "--target", "127.0.0.1:%d" % s.port, "--host", "shop.test"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing to simulate", r.stderr)


if __name__ == "__main__":
    unittest.main()
