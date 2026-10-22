FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=444 app.py adguard_group.py auth.py security.py blocked_alerts.py notification_center.py oidc.py nintendo.py index.html manifest.webmanifest sw.js icon.svg icon-192.png icon-512.png apple-touch-icon.png favicon.png ./
COPY assets/ ./assets/
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=3)" || exit 1
CMD ["python", "app.py"]
