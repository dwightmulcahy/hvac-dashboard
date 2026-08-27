FROM python:3.14-slim
# ^ ci.yml and tests.yml both extract this version number (via
# `grep -oP '(?<=FROM python:)[0-9]+\.[0-9]+'`) to set up the same
# Python version for linting/testing — keep this line's exact format
# (FROM python:X.Y-slim, no extra text before the version) so that
# regex keeps matching. This is what closed the "Python version
# declared in 3 different places, drifting independently" problem;
# don't reintroduce a hardcoded version string in either workflow file.

# Build args — injected by GitHub Actions from git tag
ARG APP_VERSION=dev
ARG BUILD_DATE=unknown
ARG GIT_SHA=unknown

# Install nginx, supervisor, curl
# --no-install-recommends avoids pulling in optional extras, but does
# NOT remove supervisor's hard dependency on python3-setuptools —
# Debian's supervisor package needs setuptools' pkg_resources module
# at runtime (see the .trivyignore justification for the CVE this
# leaves unresolved, and the note further below on why we don't
# delete it directly).
#
# `apt-get upgrade` runs first and separately from the install below:
# util-linux and its related packages (libblkid1, libmount1, login,
# mount, etc.) ship as part of the python:3.14-slim base image itself,
# not something this Dockerfile installs — `apt-get install nginx
# supervisor curl` alone never touches already-installed packages, so
# a Debian security fix for one of them (e.g. CVE-2026-53615) sits
# unapplied until something explicitly upgrades it. Unlike the
# setuptools CVE in .trivyignore, Debian has a real fix published for
# these, so upgrading is the correct response — .trivyignore is only
# for cases with no reachable fix.
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends nginx supervisor curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
# upgrade pip's own bundled toolchain first — setuptools ships as
# part of the base image's Python install, not via requirements.txt,
# so pinning a safe version there alone isn't enough on its own
RUN pip install --upgrade pip setuptools wheel --no-cache-dir && \
    pip install -r /tmp/requirements.txt --no-cache-dir && \
    # .../pip/_vendor/msgpack — pip vendors its own internal copy of
    # msgpack for pip's own use, completely separate from (and
    # invisible to) the msgpack we install via requirements.txt.
    # `pip install --upgrade msgpack` can never touch this, since it's
    # bundled inside pip's own package rather than a normal top-level
    # dependency. pip itself is a build-time tool only — nothing at
    # runtime calls it — so instead of trying to patch pip's internals
    # (risky: pip's own code depends on that vendored copy), we remove
    # pip entirely once it's done its job. A raw file delete is used
    # rather than `pip uninstall pip`, which has known self-referential
    # edge cases.
    #
    # NOTE: we do NOT also delete the apt-managed setuptools copy at
    # /usr/lib/python3/dist-packages/setuptools, even though Trivy
    # flags it (CVE-2025-47273) — a previous attempt to delete it
    # broke the container outright:
    #   ModuleNotFoundError: No module named 'packaging'
    #   (supervisord -> supervisor.options -> pkg_resources -> packaging)
    # supervisor's own startup code imports pkg_resources (part of
    # setuptools) at runtime, not just install time. This CVE is
    # instead risk-accepted via .trivyignore — see that file for the
    # justification.
    #
    # IMPORTANT: this path is intentionally NOT hardcoded to a
    # specific Python minor version (previously
    # /usr/local/lib/python3.12/site-packages/pip). A Trivy scan once
    # found pip's vendored msgpack still present in the final image
    # despite this exact removal step already existing — the base
    # image's actual site-packages directory didn't match the
    # hardcoded 3.12 path (observed as python3.14 in that scan), so
    # `rm -rf` silently matched nothing and pip was never actually
    # deleted. `rm -rf` doesn't error on a no-match glob, so this
    # failed completely silently — nothing in the build log indicated
    # anything had gone wrong. The python3 -c call below resolves the
    # real path from the interpreter that's actually running, so this
    # can't drift out of sync with whatever Python version the base
    # image ships, ever again.
    PYTHON_SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    echo "Removing pip from: $PYTHON_SITE_PACKAGES" && \
    rm -rf "$PYTHON_SITE_PACKAGES/pip" "$PYTHON_SITE_PACKAGES"/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

# Diagnostic (temporary): print the actual Python version in this
# image plus every remaining copy of setuptools/msgpack found
# anywhere, to confirm pip's vendored msgpack copy is actually gone
# and the (intentionally kept) apt setuptools copy is the only
# remaining match. The version line exists specifically so a future
# base-image Python bump is visible directly in the build log instead
# of only being discoverable via a failing Trivy scan days later.
RUN echo "=== Python version in this image ===" && python3 --version && \
    echo "=== setuptools/msgpack locations in final image ===" && \
    find / -xdev \( -iname "*setuptools*" -o -iname "*msgpack*" \) 2>/dev/null | grep -v '^/proc' || true

# Remove default nginx config
RUN rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

# Copy configs
COPY nginx.conf /etc/nginx/conf.d/hvac.conf
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create dirs
RUN mkdir -p /var/www/html /app /data

# Copy app files
COPY frontend/hvac-dashboard.html /var/www/html/index.html
COPY frontend/kiosk.html /var/www/html/kiosk.html
# Glob rather than an enumerated file list on purpose — this exact
# line has been the site of two separate real bugs (maintenance_logic.py
# /notify.py once, logging_config.py the second time), both the same
# root cause: a new top-level module gets created and wired into
# api.py's imports, works perfectly under pytest (which imports
# directly from the repo root via sys.path, never touching this line
# at all), and the container silently ships broken because nobody
# remembered this COPY line needed the new filename added too. A glob
# means there's nothing to remember — any .py file that belongs at
# the repo root (never test files; those live under tests/, which this
# doesn't recurse into) ships automatically.
COPY *.py /app/
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
