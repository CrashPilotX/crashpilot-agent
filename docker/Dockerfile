ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ARG TARGETARCH
# The config dir is the data volume, so enrolled credentials and the pinned
# identity survive the container being recreated.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    CRASHPILOT_DATA_DIR=/var/lib/crashpilot \
    CRASHPILOT_CONFIG_DIR=/var/lib/crashpilot \
    CRASHPILOT_API_HOST=0.0.0.0

# Installing systemd generates an /etc/machine-id, which every container from
# the image would then share, and enroll under as one machine. Remove it: the
# Compose file mounts the host's instead.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl dmidecode iproute2 pciutils procps python3 python3-venv \
        smartmontools systemd-sysv util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/machine-id /var/lib/dbus/machine-id

WORKDIR /opt/crashpilot
COPY agent/ /opt/crashpilot/agent/
RUN python3 -m venv /opt/crashpilot/venv \
    && /opt/crashpilot/venv/bin/pip install --no-cache-dir --upgrade "pip>=26.1.2" \
    && /opt/crashpilot/venv/bin/pip install --no-cache-dir /opt/crashpilot/agent \
    && /opt/crashpilot/venv/bin/pip uninstall --yes pip setuptools wheel
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
