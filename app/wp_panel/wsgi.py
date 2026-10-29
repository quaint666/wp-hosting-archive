"""The entry point gunicorn loads: `gunicorn -c gunicorn.conf.py wp_panel.wsgi:app`. It builds the app from the environment and the secret files, and stops with a message if one is missing."""
from . import config
from .app import create_app
from .ctl import Ctl

settings = config.load()
app = create_app(settings, Ctl(settings.ctl_host, settings.ctl_user, settings.ctl_key, settings.known_hosts))
