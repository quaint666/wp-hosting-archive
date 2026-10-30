# The control app's image (step 5). Build context is this directory (app/). It is built on the control VM by the play, and by scripts/wp_panel_image_rehearsal.sh to prove the recipe on a host
# with Docker. The container runs as uid 10001 on a read-only root file system with its secrets mounted read-only (the play delivers them, owned by 10001); nothing is baked in but code.
# renovate: depName=python
FROM python:3.15.2-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH=/app

# ssh is how the app talks to dellwp's dispatcher (wp_panel/ctl.py); the user needs an entry in /etc/passwd or ssh refuses to run.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin panel

WORKDIR /app
COPY requirements.txt .
# Wheels only: the image has no compiler, and a package that would need one fails the build instead of pulling one in.
RUN pip install --only-binary=:all: -r requirements.txt

COPY gunicorn.conf.py .
COPY wp_panel ./wp_panel

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=3)"]
CMD ["gunicorn", "-c", "/app/gunicorn.conf.py", "wp_panel.wsgi:app"]
