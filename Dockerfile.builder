FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SPECKIT_INTEGRATION_DEFAULT=generic

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tar nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-builder.txt /tmp/requirements-builder.txt
RUN pip install --no-cache-dir -r /tmp/requirements-builder.txt \
    && python -c "import sys; assert sys.version_info >= (3, 11), sys.version" \
    && specify version

# Spec Kit may need the network while it scaffolds a project. Do that once while
# building the trusted image, then copy this pinned scaffold into each runtime
# sandbox. Runtime Build containers can therefore keep network_mode=none.
RUN mkdir -p /opt/softwareforge-speckit-template \
    && cd /opt/softwareforge-speckit-template \
    && specify init --here --force \
       --integration generic \
       --integration-options="--commands-dir .softwareforge/spec-kit" \
       --script py \
       --ignore-agent-tools \
    && rm -rf .git

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin forge

WORKDIR /work
USER 10001:10001
CMD ["sleep", "infinity"]
