"""The pages: Sites, Site, Jobs, Job, Audit (they only read), and the first forms: create a site, change one setting, move a site to hold, live or stopped (batch 11 of step 6), delete a stopped site, set a WordPress user's password and install WordPress into an empty site (batch 12), and the import: upload a .wpress, plan or run it, read its report, approve or reset it, discard a file (batch 13). Each asks dellwp through the
ctl client and renders what it was told. A form is a POST with the session's token (checked before anything else), asks for ONE verb, and answers with a redirect to that verb's job page; wp-ctl decides, the
page only asks. Every form carries a request id made when the page was drawn, so a form sent twice (a double click, a reload) starts one job. Whatever dellwp sends is text to show, never markup:
the templates escape it, and a job log goes in a <pre>. A page that cannot be had says why, in a page of its own, with a status the browser understands."""
import json
import secrets
import time

from flask import abort, current_app, jsonify, redirect, render_template, request, url_for

from werkzeug.exceptions import ClientDisconnected

from .ctl import CHUNK, FILE_RE, JOB_RE, PLUGIN_ACTIONS, PLUGIN_RE, REQUEST_DECISIONS, REQUEST_ID_RE, SET_KEYS, UPLOAD_MAX, CtlError

STATUS = {"not_allowed": 404, "unreachable": 503, "bad_reply": 502, "failed": 502}
NOT_FOUND = {"unknown_site", "unknown_job"}


def ask(verb, **args):
    return current_app.extensions["panel_ctl"].call(verb, **args)


def error_page(e):
    status = 404 if e.code in NOT_FOUND else STATUS.get(e.kind, 400 if e.kind == "refused" else 502)
    titles = {404: "Not found", 400: "dellwp refused the request", 502: "dellwp gave an unexpected answer", 503: "dellwp cannot be reached"}
    return render_template("error.html", status=status, title=titles.get(status, "Error"), message=str(e)), status


def form_error(e):
    """A POST that the client or wp-ctl refused: the reason, in a page of its own. Nothing was started."""
    if e.kind == "not_allowed":
        return render_template("error.html", status=400, title="The form was not accepted", message=str(e)), 400
    return error_page(e)


def new_req():
    return "p" + secrets.token_hex(16)


def field(name, limit=600):
    """One form field as text (an absent one is empty; a field sent twice counts as absent: only the first is read and the rest ignored by form.get)."""
    value = request.form.get(name, "")
    return value if isinstance(value, str) and len(value) <= limit else ""


def started(reply):
    """Redirect to the page of the job a verb started; anything else is not an answer wp-ctl gives for these verbs."""
    job = reply.get("job")
    if not isinstance(job, str) or not JOB_RE.fullmatch(job):
        raise CtlError("bad_reply", "wp-ctl did not name the job it started")
    return redirect(url_for("job", job=job), 303)


# the moves wp-ctl allows (scripts/wp_site_model.py TRANSITIONS): a stopped site comes back on hold, never straight to live; the page offers only these, wp-ctl refuses the rest anyway
MOVES = {"hold": ("live", "stopped"), "live": ("hold", "stopped"), "stopped": ("hold",)}

# what the Site page offers to change: key -> (what it is, the kind of input, the choices or the range, what applying it costs); the rules are wp-ctl's, these only shape the form
SETTINGS = [
    ("update_policy", "Update policy", "choice", ("auto", "notify", "freeze"), "nothing is restarted"),
    ("quota_mb", "Disk quota (MB)", "int", (512, 100000), "applied to the site's disk quota"),
    ("backup_quota_mb", "Backup quota (MB)", "int", (512, 100000), "applied to the backup quota"),
    ("php_mem_mb", "PHP memory (MB)", "int", (384, 4096), "restarts the stack: about ten seconds offline"),
    ("db_mem_mb", "Database memory (MB)", "int", (192, 4096), "restarts the stack: about ten seconds offline"),
    ("upload_mb", "Upload size (MB)", "int", (1, 512), "reloads nginx and PHP"),
    ("allow_xmlrpc", "Allow XML-RPC", "bool", ("true", "false"), "a reload"),
    ("allow_app_passwords", "Allow application passwords", "bool", ("true", "false"), "a reload"),
    ("allow_user_enum", "Allow user enumeration", "bool", ("true", "false"), "a reload"),
    ("mail_mode", "Mail", "choice", ("log", "send"), "send needs the Resend key in the vault"),
    ("cart_fragments", "Mini-cart refresh", "choice", ("normal", "cache"), "cache keeps WooCommerce's empty-cart answer for visitors with no cart; nginx reloads"),
    ("label", "Label", "text", 100, "registry only: nothing is applied"),
    ("note", "Note", "text", 500, "registry only: nothing is applied"),
]


class UploadCutOff(Exception):
    """The browser's body ended before its Content-Length: the upload was abandoned."""


def register(app):
    check_csrf = app.extensions["panel_check_csrf"]
    csrf_ok = app.extensions["panel_csrf_ok"]

    def page(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except CtlError as e:
                return error_page(e)
        wrapper.__name__ = fn.__name__
        return wrapper

    def intake():
        """(files, imports, why-not) from dellwp. A page that only wants to MENTION what waits must still open when dellwp cannot say, so a failure is a sentence here, not an error page."""
        try:
            reply = ask("import-list")
        except CtlError as e:
            return [], [], str(e)
        return ([f for f in reply.get("files", []) if isinstance(f, dict)], [i for i in reply.get("imports", []) if isinstance(i, dict)], "")

    @app.get("/")
    def home():
        return redirect(url_for("sites"), 302)

    @app.get("/sites")
    @page
    def sites():
        rows = ask("list").get("sites", [])
        _, imports, why = intake()
        return render_template("sites.html", rows=[r for r in rows if isinstance(r, dict)], waiting=[i for i in imports if i.get("stage") == "reported"], unfinished=[i for i in imports if i.get("stage") != "reported"], intake_problem=why)

    @app.get("/sites/<name>")
    @page
    def site(name):
        reply = ask("show", site=name)
        registry = reply.get("registry") or {}
        status = reply.get("status") or {}
        state = status.get("deployed") if status.get("deployed") in ("hold", "live", "stopped") else registry.get("state")
        return render_template("site.html", name=name, status=status, registry=registry, registry_error=reply.get("registry_error"), state=state, moves=MOVES.get(state, ()), settings=SETTINGS, req=new_req())

    def action(fn):
        """A POST page: the token first, then the verb; a refusal is shown, never retried."""
        def wrapper(*a, **kw):
            check_csrf()
            try:
                return fn(*a, **kw)
            except CtlError as e:
                return form_error(e)
        wrapper.__name__ = fn.__name__
        return wrapper

    @app.get("/new-site")
    def new_site():
        return render_template("new_site.html", req=new_req())

    @app.post("/new-site")
    @action
    def create_site():
        args = {"name": field("name", 100), "req": field("req", 80)}
        if field("label", 200):
            args["label"] = field("label", 200)
        return started(ask("create", **args))

    @app.post("/sites/<name>/set")
    @action
    def set_value(name):
        key = field("key", 40)
        if key not in SET_KEYS:
            abort(400)
        return started(ask("set", site=name, key=key, value=field("value", 600), req=field("req", 80)))

    @app.post("/sites/<name>/state")
    @action
    def set_state(name):
        args = {"site": name, "state": field("state", 20), "req": field("req", 80)}
        if args["state"] == "live":
            args["confirm_name"] = field("confirm_name", 60)
        if args["state"] == "stopped" and field("confirm_name", 60) != name:      # wp-ctl does not ask for this one; the page does, because stopping takes a final backup and shows visitors a 503
            return render_template("error.html", status=400, title="The form was not accepted", message="The name typed is not the site's name. The site was not stopped."), 400
        return started(ask("ensure-state", **args))

    @app.post("/sites/<name>/delete")
    @action
    def delete_site(name):
        """Two steps, two entries of the name: the first only opens the page that says what will be deleted; the second starts the job. The tool wants a stopped site and says so; the page says so first."""
        typed, again = field("confirm_name", 60), field("confirm_again", 60)
        if typed != name:
            return render_template("error.html", status=400, title="The form was not accepted", message="The name typed is not the site's name. Nothing was deleted."), 400
        if not again:
            reply = ask("show", site=name)
            status, registry = reply.get("status") or {}, reply.get("registry") or {}
            if (status.get("deployed") or registry.get("state")) != "stopped":
                return render_template("error.html", status=400, title="Stop the site first", message="Only a stopped site is deleted: move it to stopped (it takes a final backup that is kept for the archive), then delete it."), 400
            return render_template("delete_confirm.html", name=name, status=status, registry=registry, req=new_req())
        return started(ask("delete", site=name, confirm_name=typed, confirm_again=again, req=field("req", 80)))

    def secret_form(template, name):
        reply = ask("show", site=name)                         # an unknown site is a 404 here, before a form is drawn
        return render_template(template, name=name, registry=reply.get("registry") or {})

    def done(title, name, out, back="site"):
        return render_template("done.html", title=title, name=name, out=str(out or "")[-1500:], back=back)

    @app.get("/sites/<name>/password")
    @page
    def password_form(name):
        reply = ask("show", site=name)                         # an unknown site is a 404 here, before a form is drawn
        try:                                                    # the list is a convenience: when it cannot be had, the form takes a typed name as before
            users = [u for u in ask("users", site=name).get("users", []) if isinstance(u, dict) and isinstance(u.get("user"), str)]
            why = ""
        except CtlError as e:
            users, why = [], str(e)
        return render_template("password.html", name=name, registry=reply.get("registry") or {}, users=users, users_problem=why, wanted=request.args.get("user", ""))

    @app.post("/sites/<name>/password")
    @action
    def change_password(name):
        user, pw, again = field("user", 80), field("password", 300), field("again", 300)
        if pw != again:
            return render_template("error.html", status=400, title="The form was not accepted", message="The two passwords are not the same. Nothing was changed."), 400
        return done("Password changed", name, ask("password", secret=pw, site=name, user=user).get("out"))

    @app.get("/sites/<name>/install")
    @page
    def install_form(name):
        return secret_form("install.html", name)

    @app.post("/sites/<name>/install")
    @action
    def install_wordpress(name):
        pw, again = field("password", 300), field("again", 300)
        if pw != again:
            return render_template("error.html", status=400, title="The form was not accepted", message="The two passwords are not the same. Nothing was changed."), 400
        reply = ask("install", secret=pw, site=name, title=field("title", 200), user=field("user", 60), email=field("email", 300))
        return done("WordPress installed", name, reply.get("out"))

    # ---------------------------------------------------------------------------------------------------------------------------- the import (batch 13)
    @app.get("/import")
    @page
    def import_hub():
        rows = [r for r in ask("list").get("sites", []) if isinstance(r, dict) and isinstance(r.get("site"), str)]
        files, imports, why = intake()
        wanted = request.args.get("file", "")
        return render_template("import.html", sites=[r["site"] for r in rows], req=new_req(), upload_max_mb=UPLOAD_MAX // (1024 * 1024), files=files, imports=imports, intake_problem=why,
                               prefill=wanted if FILE_RE.fullmatch(wanted) and len(wanted) <= 100 else "", just_uploaded=request.args.get("uploaded") == "1")

    def discard_quietly(file):
        """Best effort, after an upload that did not end cleanly: wp-site-import cannot tell a cut-off stream from a finished file, so a short file may have been kept. If this fails too, the importer's
        own check of the archive (a truncated file is refused) is the second line."""
        try:
            app.extensions["panel_ctl"].call("import-discard", file=file, if_present=True)
        except CtlError:
            pass

    @app.put("/import/files/<file>")
    def upload(file):
        """The raw bytes of the file are the body. The token is a header (a page of another origin cannot set one), the length must be announced, and the body is passed on a chunk at a time: nothing is kept
        here, in memory or on disk. wp-ctl's answer must match what was sent (bytes and sha256)."""
        def fail(text, status):
            return jsonify(ok=False, error=text), status
        if not csrf_ok(request.headers.get("X-CSRF", "")):
            return fail("The page's token is missing or wrong: reload the page and try again.", 400)
        length = request.content_length
        if length is None or length < 1:
            return fail("The file is empty, or its length was not announced.", 411 if length is None else 400)
        if length > UPLOAD_MAX:
            return fail("The file is larger than the %d MB the intake can hold." % (UPLOAD_MAX // (1024 * 1024)), 413)
        request.max_content_length = UPLOAD_MAX
        body = request.stream

        def chunks():
            left = length
            while left:
                try:
                    data = body.read(min(CHUNK, left))
                except (ClientDisconnected, OSError, ValueError):
                    raise UploadCutOff()
                if not data:
                    raise UploadCutOff()
                left -= len(data)
                yield data
        try:
            got = app.extensions["panel_ctl"].stream("import-receive", chunks(), file=file)
        except UploadCutOff:
            time.sleep(2)                                    # dellwp finishes the short file when its input ends; give it that moment, then remove it
            discard_quietly(file)
            return fail("The upload was cut off. What reached dellwp was removed again.", 400)
        except CtlError as e:
            if e.kind in ("bad_reply", "unreachable"):
                discard_quietly(file)
            return fail(str(e), 400 if e.kind in ("not_allowed", "refused") else 503 if e.kind == "unreachable" else 502)
        return jsonify(ok=True, file=got["file"], bytes=got["bytes"], sha256=got["sha256"])

    @app.post("/import/run")
    @action
    def import_run():
        mode = field("mode", 10)
        if mode not in ("plan", "run", "replace", "new"):
            abort(400)
        if mode == "new":                                   # a new site and the import into it, one job
            args = {"name": field("name", 40), "file": field("file", 120), "req": field("req", 80)}
            if field("label", 200):
                args["label"] = field("label", 200)
            return started(ask("import-new", **args))
        args = {"site": field("site", 40), "file": field("file", 120), "req": field("req", 80), "plan": mode == "plan", "replace": mode == "replace"}
        if mode == "replace":
            args["confirm_name"] = field("confirm_name", 60)
        return started(ask("import-run", **args))

    @app.post("/import/discard")
    @action
    def import_discard():
        reply = ask("import-discard", file=field("file", 120), if_present=False)
        return done("File removed", "", reply.get("out"), back="import_hub")

    @app.get("/sites/<name>/import")
    @page
    def import_site(name):
        ask("show", site=name)                              # an unknown site is a 404 before anything else
        try:
            report, problem = ask("import-report", site=name).get("out", ""), ""
        except CtlError as e:
            if e.kind not in ("failed", "refused"):
                raise
            report, problem = "", str(e)
        _, imports, _why = intake()
        mine = next((i for i in imports if i.get("site") == name), None)
        return render_template("import_site.html", name=name, report=str(report)[-20000:], problem=problem, req=new_req(), pending=mine)

    @app.post("/sites/<name>/import/approve")
    @action
    def import_approve(name):
        args = {"site": name, "req": field("req", 80), "accept_failures": field("accept_failures", 5) == "yes"}
        if args["accept_failures"]:
            args["confirm_name"] = field("confirm_name", 60)       # accepting known failures is the one approval that asks for the site's name
        if field("reject", 250):
            args["reject"] = field("reject", 250)
        return started(ask("import-approve", **args))

    @app.post("/sites/<name>/import/rescan")
    @action
    def import_rescan(name):
        return started(ask("import-rescan", site=name, req=field("req", 80)))

    @app.post("/sites/<name>/import/reset")
    @action
    def import_reset(name):
        return started(ask("import-reset", site=name, confirm_name=field("confirm_name", 60), req=field("req", 80)))

    @app.get("/sites/<name>/plugins")
    @page
    def plugins_page(name):
        """The site's plugins with what can be done to each. WordPress is asked through WP-CLI without loading any plugin, so a plugin that breaks the site is still listed (and can be switched off). When the list cannot be had the
        page says why instead of an error page: the site's other pages are still there."""
        reply = ask("show", site=name)                          # an unknown site is a 404 here
        state = (reply.get("status") or {}).get("deployed") or (reply.get("registry") or {}).get("state")
        try:
            rows, problem = [p for p in ask("plugins", site=name).get("plugins", []) if isinstance(p, dict) and isinstance(p.get("name"), str)], ""
        except CtlError as e:
            if e.kind not in ("failed", "refused"):
                raise
            rows, problem = [], str(e)
        return render_template("plugins.html", name=name, state=state, rows=rows, problem=problem)

    @app.post("/sites/<name>/plugins")
    @action
    def plugin_change(name):
        """Activate or deactivate at once; a delete is two steps like a site's: the first press opens the page that says what will go, the second (with the name typed) asks wp-ctl. wp-ctl re-reads the
        site's own list, so a name or a state that is not true now is refused there."""
        plugin, what, typed = field("plugin", 120), field("action", 20), field("confirm_plugin", 120)
        if what not in PLUGIN_ACTIONS or not PLUGIN_RE.fullmatch(plugin):
            return render_template("error.html", status=400, title="The form was not accepted", message="That is not a plugin action. Nothing was changed."), 400
        if what == "delete" and not typed:
            ask("show", site=name)
            return render_template("plugin_delete.html", name=name, plugin=plugin)
        args = {"site": name, "plugin": plugin, "action": what}
        if what == "delete":
            args["confirm_plugin"] = typed
        reply = ask("plugin-set", **args)
        titles = {"activate": "Plugin activated", "deactivate": "Plugin deactivated", "delete": "Plugin deleted"}
        return done(titles[what], name, reply.get("out"), back="plugins")

    def request_rows():
        return [r for r in ask("plugin-requests").get("requests", []) if isinstance(r, dict) and isinstance(r.get("site"), str) and isinstance(r.get("id"), str)]

    @app.get("/requests")
    @page
    def requests_page():
        """What the sites ask to have installed (Install in a site's wp-admin), waiting ones first. The list is what dellwp last collected: the Refresh button asks every site now."""
        rows = request_rows()
        waiting = [r for r in rows if r.get("state") == "pending"]
        return render_template("requests.html", waiting=waiting, finished=[r for r in rows if r.get("state") != "pending"][:30])

    @app.post("/requests/refresh")
    @action
    def requests_refresh():
        reply = ask("plugin-requests-collect")
        return done("Requests collected", "", reply.get("out"), back="requests_page")

    @app.post("/requests/decide")
    @action
    def request_decide():
        """Approve or deny one request. A request that is clean is decided at once. One with a known vulnerability, or that could not be checked, is approved only on purpose: the first press opens a page that says why, and
        the plugin's name has to be typed. wp-ctl and the tool check the same thing again, so this page is only where the person is asked."""
        site, rid, what, typed = field("site", 40), field("id", 40), field("decision", 20), field("confirm_slug", 120)
        if what not in REQUEST_DECISIONS or not REQUEST_ID_RE.fullmatch(rid):
            return render_template("error.html", status=400, title="The form was not accepted", message="That is not a request decision. Nothing was changed."), 400
        mine = next((r for r in request_rows() if r["site"] == site and r["id"] == rid), None)
        if mine is None:
            return render_template("error.html", status=404, title="Not found", message="dellwp does not remember that request: it may have expired. Nothing was changed."), 404
        if what == "approve" and not (mine.get("found") and mine.get("vuln") == "clean") and not typed:
            return render_template("request_confirm.html", r=mine)
        args = {"site": site, "id": rid, "decision": what}
        if typed:
            args["confirm_slug"] = typed
        reply = ask("plugin-request-decide", **args)
        return done("Request approved" if what == "approve" else "Request denied", site, reply.get("out"), back="requests_page")

    @app.get("/sites/<name>/security")
    @page
    def security(name):
        """What the site runs against the vulnerability index, and the buttons for updates. A vulnerability check that fails is shown on the page: the buttons must still be there."""
        reply = ask("show", site=name)
        state = (reply.get("status") or {}).get("deployed") or (reply.get("registry") or {}).get("state")
        result, rc, problem = "", None, ""
        try:
            got = ask("vuln-check", site=name)
            result, rc = str(got.get("out", ""))[-20000:], got.get("rc")
        except CtlError as e:
            if e.kind not in ("failed", "refused"):
                raise
            problem = str(e)
        _, imports, _why = intake()
        mine = next((i for i in imports if i.get("site") == name), None)
        return render_template("security.html", name=name, state=state, result=result, rc=rc, problem=problem, req=new_req(), pending=mine)

    @app.post("/sites/<name>/updates/check")
    @action
    def updates_check(name):
        return started(ask("update-check", site=name, req=field("req", 80)))

    @app.post("/sites/<name>/updates/apply")
    @action
    def updates_apply(name):
        if field("understood", 5) != "yes":
            return render_template("error.html", status=400, title="The form was not accepted", message="Tick the box to say you know the site is paused while it updates. Nothing was changed."), 400
        return started(ask("update-apply", site=name, req=field("req", 80)))

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
