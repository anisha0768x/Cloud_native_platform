FROM python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
WORKDIR /workload
COPY worker.py ./worker.py
USER 65532:65532
EXPOSE 9090
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9090/health',timeout=2)"
CMD ["python", "worker.py"]
