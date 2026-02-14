FROM python:3.10-slim-bookworm

ENV UV_TOOL_BIN_DIR=/usr/local/bin
ENV MISE_VERBOSE=1
ENV MISE_INSTALL_PATH=/usr/local/bin/mise
ENV MISE_DATA_DIR=/usr/local/share/mise
ENV MISE_CONFIG_DIR=/usr/local/etc/mise
ENV UV_INSTALL_MODE=copy
# Append mise shims to PATH so system/venv python takes precedence
ENV PATH="$PATH:$MISE_DATA_DIR/shims"

# Copy mise files
COPY mise.lock mise.toml /app/

# Copy uv files
COPY uv.lock pyproject.toml /app/

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    git \
    build-essential \
    sudo \
    xdg-utils \
    # Install mise
    && curl https://mise.run | sh \
    && mise trust /app/mise.toml \
    && mise install -y \
    && mise reshim \
    && mise run install:dev \
    && mise use -g uv \
    && uv sync --no-install-project \
    # Clean up aggressively
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/* \
    && rm -rf /tmp/* \
    && rm -rf /root/.cache

ENV PATH="/app/.venv/bin:$PATH"

# Copy application code
COPY . /app
