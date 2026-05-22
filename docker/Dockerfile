# CrashPilot Agent — Docker image
# Supports: all Linux distro hosts (mounts host paths for telemetry)
#
# Usage:
#   docker run --privileged \
#     -v /var/log:/var/log:ro \
#     -v /run/log/journal:/run/log/journal:ro \
#     -v /sys:/sys:ro \
#     -v /proc:/host/proc:ro \
#     -v /dev:/dev:ro \
#     -e CRASHPILOT_ANTHROPIC_API_KEY=sk-ant-... \
#     -p 7878:7878 \
#     ghcr.io/kdigitalsystems/crashpilot

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="CrashPilot"
LABEL org.opencontainers.image.description="AI-powered Linux crash forensics agent"
LABEL org.opencontainers.image.source="https://github.com/kdigitalsystems/CrashPilot"
LABEL org.opencontainers.image.licenses="MIT"

# System dependencies for telemetry collectors
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Log tools
    systemd \
    # Disk health
    smartmontools \
    # Temp sensors
    lm-sensors \
    # Machine check exceptions
    mcelog \
    # Diagnostics
    util-linux \
    procps \
    iproute2 \
    pciutils \
    dmidecode \
    curl \
    # Build tools for Python packages
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python package
COPY agent/ ./agent/
RUN pip install --no-cache-dir -e ./agent

# Copy systemd and k8s manifests (for reference)
COPY systemd/ ./systemd/
COPY k8s/ ./k8s/

# Entrypoint script — selects mode based on CMD
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Config directory
VOLUME ["/data"]
ENV CRASHPILOT_DATA_DIR=/data

# API server port
EXPOSE 7878

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
