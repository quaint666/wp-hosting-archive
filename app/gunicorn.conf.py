"""gunicorn's settings for the control app. Workers: 2 x the CPU threads this container may use, plus one (gunicorn's rule of thumb), so a VM with one core runs three; the page waits on ssh most of the
time, so each worker also has a few threads. The lockout of the login is a file all workers share (wp_panel/auth.py), so more than one worker is safe. Everything that arrives from a browser is bounded:
the request line, the number and size of the headers, and (in the app) the body. The container's root file system is read-only: gunicorn's heartbeat files go to a RAM disk."""
import os

bind = os.environ.get("PANEL_BIND", "0.0.0.0:8000")
workers = 2 * len(os.sched_getaffinity(0)) + 1
threads = 4
worker_class = "gthread"
timeout = 90                               # a job log or a list is quick; the longest ssh call the app makes is 30 seconds
graceful_timeout = 30
keepalive = 5
worker_tmp_dir = "/dev/shm"
limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(L)s'          # no cookies, no referer, no user agent in the log
capture_output = True
