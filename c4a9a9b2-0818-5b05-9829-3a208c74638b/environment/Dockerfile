# Task environment for c4a9a9b2-0818-5b05-9829-3a208c74638b
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

RUN apt-get update && apt-get install -y --no-install-recommends \
        xxd \
        file \
        bash \
        curl \
        procps \
        nodejs \
        npm \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The evaluation harness injects its coding agent (openhands-sdk) into this image
# at build time. This task runs sealed (network_mode = "no-network"), so the agent
# CANNOT be fetched at runtime -- without this pre-bake the sealed container dies
# immediately with "curl: (6) Could not resolve host: astral.sh" and produces no
# trajectory. Build time is NOT sealed, so the download belongs here.
# The path is load-bearing: the SDK agent's install guard skips installation only
# when /opt/openhands-sdk-venv/bin/python can import openhands.sdk.
ARG OH_PY=3.12
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && . "$HOME/.local/bin/env" \
    && uv python install ${OH_PY} \
    && mkdir -p /opt/openhands-sdk-venv \
    && uv venv /opt/openhands-sdk-venv --python ${OH_PY} --clear \
    && . /opt/openhands-sdk-venv/bin/activate \
    && uv pip install openhands-sdk openhands-tools fastapi \
    && /opt/openhands-sdk-venv/bin/python -c "import openhands.sdk" \
    && chmod -R a+rX /opt/openhands-sdk-venv

WORKDIR /app
RUN mkdir -p /app
COPY data /app/data
