# syntax=docker/dockerfile:1
#
# haresign-billing — Haresign Billing.
#
# Two stages. The build stage owns pip and the wheel cache; the runtime stage
# gets the installed packages and the application, and nothing else. A service
# that holds subscription state should not ship a package manager and a compiler
# to production.

FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Installed into a virtualenv rather than the system site-packages purely so the
# whole dependency tree is one directory to copy into the runtime stage.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

COPY --from=build /venv /venv
COPY . .

# Static files are hashed and compressed at build time, not on boot: every
# container in a rollout then serves byte-identical assets, and start-up does no
# work that could fail.
#
# The key below is used by this build step alone and never at runtime — the real
# one comes from the environment. collectstatic neither signs nor stores
# anything, so nothing built here depends on its value.
RUN SECRET_KEY=build-only-not-a-runtime-secret \
    DEBUG=1 \
    python manage.py collectstatic --noinput \
    && find /app -name '__pycache__' -type d -prune -exec rm -rf {} +

# Unprivileged. Nothing in the image is written at runtime: static files are
# baked in above, and this service accepts no uploads. Migration artifacts are
# written by an operator process to a mounted directory, never by the web
# process to the image.
RUN groupadd --system --gid 10001 billing \
    && useradd --system --create-home --uid 10001 --gid billing billing \
    && chown -R billing:billing /app
USER billing

ENTRYPOINT ["python", "/app/config/secret_entrypoint.py"]

EXPOSE 8000

# Readiness rather than liveness: route only to an instance that can reach both
# durable billing state and the fail-closed throttle state the webhook endpoint
# and the entitlement API depend on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready/', timeout=4).status == 200 else 1)"

# Migrations are NOT run here. An entrypoint that migrates on boot means every
# replica races to alter the schema during a rollout, and a failed migration
# becomes a crash loop instead of a decision. Run it deliberately — see README.
CMD ["sh", "-c", "gunicorn config.wsgi:application \
     --bind 0.0.0.0:8000 \
     --workers ${GUNICORN_WORKERS:-3} \
     --threads ${GUNICORN_THREADS:-4} \
     --timeout ${GUNICORN_TIMEOUT:-30} \
     --access-logfile - \
     --error-logfile -"]
