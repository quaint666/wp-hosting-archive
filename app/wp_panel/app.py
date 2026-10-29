"""The Flask application: login, session, headers, error pages. The pages are views.py. create_app() takes everything it uses (settings, the ctl client, a clock), so the tests need no server, no
ssh and no real time.

What a request must have: a session that this app signed, that says the operator logged in, less than the session life ago by the app's own clock (the cookie's signature has a clock of its own
too). Every page except the login and the stylesheet needs it; anything else is sent to the login. A form is accepted only with the token the session holds (CSRF). Every answer carries the
headers that keep a page from being framed, sniffed, cached or loaded from anywhere but this site. No page runs a script or loads a font or a picture from another host: there are none."""
import hmac
import secrets
import time
from datetime import timedelta

from flask import Flask, abort, redirect, render_template, request, session, url_for

from . import auth, views

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
}
MAX_BODY = 16 * 1024                    # the login form is the largest thing any page takes; the upload page (step 6) sets its own limit on its own route


def create_app(settings, ctl, clock=time.time):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=settings.session_key, SESSION_COOKIE_NAME="panel_session", SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(seconds=settings.session_s), MAX_CONTENT_LENGTH=MAX_BODY, TESTING=False, DEBUG=False, PROPAGATE_EXCEPTIONS=False)
    app.extensions["panel_ctl"] = ctl
    app.extensions["panel_clock"] = clock
    throttle = auth.Throttle(settings.state_dir, clock)
    app.jinja_env.filters["clip"] = lambda value, n=300: (str(value)[:n] + "…") if len(str(value)) > n else str(value)

    def csrf_token():
        if not isinstance(session.get("csrf"), str) or len(session["csrf"]) < 20:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def check_csrf():
        sent = request.form.get("csrf", "")
        if not hmac.compare_digest(sent.encode(), csrf_token().encode()):
            abort(400)

    def logged_in():
        iat = session.get("iat")
        return session.get("uid") == "operator" and isinstance(iat, (int, float)) and not isinstance(iat, bool) and 0 <= clock() - iat < settings.session_s

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["logged_in"] = logged_in

    @app.before_request
    def need_login():
        if request.endpoint in ("login", "static"):
            return None
        if not logged_in():
            session.clear()
            return redirect(url_for("login"), 302)
        return None

    @app.after_request
    def headers(response):
        for name, value in HEADERS.items():
            response.headers[name] = value
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if logged_in():
                return redirect(url_for("sites"), 303)
            csrf_token()
            return render_template("login.html", error=None)
        check_csrf()
        result, wait = auth.check_credentials(settings, request.form.get("password", ""), request.form.get("code", "").strip(), throttle, clock)
        if result == "ok":
            session.clear()                              # a new session for a new login: nothing from before carries over
            session.update(uid="operator", iat=int(clock()), csrf=secrets.token_urlsafe(32))
            session.permanent = True
            return redirect(url_for("sites"), 303)
        if result == "locked":
            response = app.make_response(render_template("login.html", error="Too many wrong logins. Try again in %d minutes." % max(1, (wait + 59) // 60)))
            response.status_code = 429
            response.headers["Retry-After"] = str(wait)
            return response
        return render_template("login.html", error="That login was not accepted."), 401

    @app.post("/logout")
    def logout():
        check_csrf()
        session.clear()
        return redirect(url_for("login"), 303)

    views.register(app)

    @app.errorhandler(Exception)
    def failure(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return render_template("error.html", status=exc.code, title=exc.name, message=""), exc.code
        app.logger.exception("an unexpected error in a request")
        return render_template("error.html", status=500, title="Something went wrong", message="The app hit an error it did not expect. Nothing was changed. The details are in the container's log."), 500

    return app
