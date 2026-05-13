ARG VARIANT="debian"
FROM mcr.microsoft.com/vscode/devcontainers/base:${VARIANT}

# Install system packages needed for building Python packages and Home Assistant tests
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
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

# HA 2026.3.0+ requires Python >=3.14.2, but Debian Trixie ships Python
# 3.13 as ``python3``. ``uv`` manages a 3.14 install cleanly without
# pulling deadsnakes/source-building, and it's the modern HA toolchain
# default.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && uv python install 3.14

# Create a prebuilt venv at /opt/venv with the uv-managed Python 3.14 so
# the postCreate scripts have pip available without bootstrapping it at
# container runtime (avoids PEP 668 issues).
RUN uv venv --python 3.14 /opt/venv \
    && /opt/venv/bin/python -m pip install -U pip "setuptools>=70.0" wheel || true

# Make sure the venv, uv-python dir, and workspace directories are
# writable by the non-root 'vscode' user so the container user can
# install packages into them during postCreate/setup.
RUN chown -R vscode:vscode /opt/venv /opt/uv-python /workspaces || true
