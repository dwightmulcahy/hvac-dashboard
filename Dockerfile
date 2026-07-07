FROM python:3.12-slim

# Install nginx and supervisor
RUN apt-get update && apt-get install -y nginx supervisor curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install fastapi uvicorn --no-cache-dir

# Remove default nginx config
RUN rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf

# Copy configs
COPY nginx.conf /etc/nginx/conf.d/hvac.conf
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create webroot and app dir
RUN mkdir -p /var/www/html /app /data

# Copy app files
COPY hvac-dashboard.html /var/www/html/index.html
COPY api.py /app/api.py

# Fix permissions
RUN chown -R www-data:www-data /var/www/html && chmod -R 755 /var/www/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf http://localhost/health || exit 1

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
