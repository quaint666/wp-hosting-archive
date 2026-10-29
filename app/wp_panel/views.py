"""The read-only pages: Sites, Site, Jobs, Job, Audit. Each asks dellwp (through the ctl client, read verbs only) and renders what it was told. Whatever dellwp sends is text to show, never markup:
the templates escape it, and a job log goes in a <pre>. A page that cannot be had says why, in a page of its own, with a status the browser understands."""
import json

from flask import current_app, redirect, render_template, request, url_for

from .ctl import CtlError

STATUS = {"not_allowed": 404, "unreachable": 503, "bad_reply": 502, "failed": 502}
NOT_FOUND = {"unknown_site", "unknown_job"}


def ask(verb, **args):
    return current_app.extensions["panel_ctl"].call(verb, **args)


def error_page(e):
    status = 404 if e.code in NOT_FOUND else STATUS.get(e.kind, 400 if e.kind == "refused" else 502)
    titles = {404: "Not found", 400: "dellwp refused the request", 502: "dellwp gave an unexpected answer", 503: "dellwp cannot be reached"}
    return render_template("error.html", status=status, title=titles.get(status, "Error"), message=str(e)), status


def register(app):
    def page(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except CtlError as e:
                return error_page(e)
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.get("/")
    def home():
        return redirect(url_for("sites"), 302)

    @app.get("/sites")
    @page
    def sites():
        rows = ask("list").get("sites", [])
        return render_template("sites.html", rows=[r for r in rows if isinstance(r, dict)])

    @app.get("/sites/<name>")
    @page
    def site(name):
        reply = ask("show", site=name)
        return render_template("site.html", name=name, status=reply.get("status") or {}, registry=reply.get("registry") or {}, registry_error=reply.get("registry_error"))

    @app.get("/jobs")
    @page
    def jobs():
        return render_template("jobs.html", rows=[r for r in ask("jobs").get("jobs", []) if isinstance(r, dict)])

    @app.get("/jobs/<job>")
    @page
    def job(job):
        try:
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            offset = -1
        log = ask("job-log", job=job, offset=offset)          # first: a bad offset or job stops here, before the second question is asked
        status = ask("job-status", job=job)
        return render_template("job.html", job=job, status=status, log=log, offset=offset)

    @app.get("/audit")
    @page
    def audit():
        try:
            lines = int(request.args.get("lines", "50"))
        except ValueError:
            lines = 0
        rows = []
        for r in ask("audit-tail", lines=lines).get("lines", []):
            if isinstance(r, dict):
                rest = {k: v for k, v in r.items() if k not in ("ts", "phase", "verb", "site", "error")}
                rows.append(dict(r, rest=json.dumps(rest, sort_keys=True, ensure_ascii=False) if rest else ""))
        return render_template("audit.html", rows=rows, lines=lines)
