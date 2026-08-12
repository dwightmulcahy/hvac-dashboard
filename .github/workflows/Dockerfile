FROM python:3.12-slim

# Build args — injected by GitHub Actions from git tag
ARG APP_VERSION=dev
ARG BUILD_DATE=unknown
ARG GIT_SHA=unknown

# Install nginx, supervisor, curl
# --no-install-recommends: supervisor's Debian package can pull in
# python3-setuptools as an optional "Recommends" dependency, installed
# into apt's own /usr/lib/python3/dist-packages/ tree — completely
# separate from and invisible to the pip-managed setuptools we
# explicitly upgrade below. That leftover apt copy is what Trivy keeps
# flagging even after the pip-side fix. --no-install-recommends skips
# optional recommended packages; it never skips a genuine hard
# dependency, so this can't break anything supervisor actually needs.
RUN apt-get update && apt-get install -y --no-install-recommends nginx supervisor curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
# upgrade pip's own bundled toolchain first — setuptools ships as
# part of the base image's Python install, not via requirements.txt,
# so pinning a safe version there alone isn't enough on its own
RUN pip install --upgrade pip setuptools wheel --no-cache-dir && \
    pip install -r /tmp/requirements.txt --no-cache-dir

# Diagnostic (temporary): print every remaining copy of setuptools/
# msgpack found anywhere in the image. If a vulnerable copy is still
# present after the --no-install-recommends fix above, this tells us
# exactly where it lives in the next CI build log, instead of another
# guess-and-check round trip. Safe to remove once the Trivy scan is
# confirmed clean.
RUN echo "=== setuptools/msgpack locations in final image ===" && \
    find / -xdev \( -iname "*setuptools*" -o -iname "*msgpack*" \) 2>/dev/null | grep -v '^/proc' || true

# Remove default nginx config
RUN rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

# Copy configs
COPY nginx.conf /etc/nginx/conf.d/hvac.conf
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create dirs
RUN mkdir -p /var/www/html /app /data

# Copy app files
COPY hvac-dashboard.html /var/www/html/index.html
COPY api.py auth.py state.py models.py worker.py /app/
COPY routers/ /app/routers/

# Inject build version into dashboard
RUN sed -i "s/DASHBOARD_VERSION_PLACEHOLDER/${APP_VERSION}/g" /var/www/html/index.html

# Fix permissions
RUN chown -R www-data:www-data /var/www/html && chmod -R 755 /var/www/html

# Expose version at runtime
ENV APP_VERSION=${APP_VERSION}
ENV BUILD_DATE=${BUILD_DATE}
ENV GIT_SHA=${GIT_SHA}

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -sf http://localhost/health || exit 1

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
