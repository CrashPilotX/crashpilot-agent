ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    CRASHPILOT_DATA_DIR=/var/lib/crashpilot \
    CRASHPILOT_API_HOST=0.0.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl dmidecode iproute2 pciutils procps python3 python3-venv \
        smartmontools systemd-sysv util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/crashpilot
COPY agent/ /opt/crashpilot/agent/
RUN python3 -m venv /opt/crashpilot/venv \
    && /opt/crashpilot/venv/bin/pip install --no-cache-dir /opt/crashpilot/agent
ENV PATH="/opt/crashpilot/venv/bin:${PATH}"

COPY docker/entrypoint.sh /usr/local/bin/crashpilot-entrypoint
RUN chmod 0755 /usr/local/bin/crashpilot-entrypoint \
    && mkdir -p /var/lib/crashpilot

EXPOSE 7878
VOLUME ["/var/lib/crashpilot"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:7878/health || exit 1

ENTRYPOINT ["/usr/local/bin/crashpilot-entrypoint"]
CMD ["crashpilot", "serve", "--host", "0.0.0.0", "--port", "7878"]
