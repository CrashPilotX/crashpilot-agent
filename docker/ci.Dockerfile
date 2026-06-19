ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION}

ARG NODE_MAJOR=24

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash build-essential ca-certificates curl dpkg-dev git gnupg iproute2 jq \
        libffi-dev libssl-dev postgresql-client python3 python3-pip python3-venv \
        shellcheck sudo systemd tar unzip xz-utils zip \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && . /etc/os-release \
    && echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list \
    && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-buildx-plugin docker-ce-cli docker-compose-plugin nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN node --version \
    && npm --version \
    && python3 --version \
    && docker --version \
    && docker buildx version \
    && docker compose version \
    && shellcheck --version

WORKDIR /workspace

CMD ["bash"]
