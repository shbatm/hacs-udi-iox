ARG VARIANT="debian"
FROM mcr.microsoft.com/vscode/devcontainers/base:${VARIANT}

# Install system packages needed for building Python packages and Home Assistant tests
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      python3-dev \
      python3-venv \
      python3-pip \
      python3-setuptools \
      python3-full \
      git \
      curl \
      jq \
      ca-certificates \
      libssl-dev \
      libffi-dev \
      libjpeg-dev \
      zlib1g-dev \
      pkg-config \
      cargo \
    && rm -rf /var/lib/apt/lists/*

# Create a prebuilt venv at /opt/venv so the postCreate scripts have pip available
# without bootstrapping it at container runtime (avoids PEP 668 issues).
RUN python3 -m venv /opt/venv \
  && /opt/venv/bin/python -m pip install -U pip "setuptools>=70.0" wheel || true

# Make sure the venv and workspace directories are writable by the non-root 'vscode' user
# so the container user can install packages into them during postCreate/setup.
RUN chown -R vscode:vscode /opt/venv /workspaces || true
