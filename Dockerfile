ARG BASE_IMAGE=python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
FROM ${BASE_IMAGE}
USER root
WORKDIR /opt/platform
COPY distributed/requirements.txt distributed/constraints.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 60 --retries 8 -r requirements.txt
COPY distributed/requirements-aws.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 60 --retries 8 -r requirements-aws.txt
COPY distributed/helio ./helio
COPY deploy/aws/bootstrap.py ./aws_bootstrap.py
COPY web ./web
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
EXPOSE 8000
CMD ["python", "-m", "helio.main"]
