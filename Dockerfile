# One image for every role; the command picks the role:
#   serve                       REST control plane (Cloud Run service)        <- default
#   task --stage s4 ...         one data-engine shard (Cloud Run Job task)
#   api --tenant <id>           tenant business API (Cloud Run service deployed by stage s8)
# FACTORY_PROFILE picks the backends (memory | memory-offline | gcp); credentials come from the
# service account (Application Default Credentials), never from the image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FACTORY_CONFIG_DIR=/app/config \
    FACTORY_SKILL_DIR=/app/skills/healthcare-capability-builder \
    FACTORY_PROFILE=memory-offline \
    PORT=8080

WORKDIR /app

# dependencies first (better layer caching), then the code
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[gcp]"

COPY config ./config
# the capability skill: the agents read it at run time (FACTORY_SKILL_DIR)
COPY skills ./skills

RUN useradd --create-home --uid 10001 factory
USER factory

EXPOSE 8080
ENTRYPOINT ["factory"]
CMD ["serve", "--host", "0.0.0.0"]
