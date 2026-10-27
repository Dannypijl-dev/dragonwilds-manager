# Dragonwilds Manager: web panel + the game servers it creates, in one container.
# Data (servers, worlds, backups, settings) lives in /data; mount a volume there.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    DWM_IN_DOCKER=1 DWM_DATA_DIR=/data DWM_PORT=8080
# SteamCMD is a 32-bit program (lib32gcc-s1, lib32stdc++6). nftables is optional: kicks and the
# whitelist only work when the container also gets NET_ADMIN (see docker-compose.yml).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates lib32gcc-s1 lib32stdc++6 nftables tini \
 && rm -rf /var/lib/apt/lists/*

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

VOLUME /data
EXPOSE 8080/tcp 7777/udp
STOPSIGNAL SIGTERM
# tini forwards SIGTERM so the manager can stop the game servers cleanly (they save the world then)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["dragonwilds-manager", "run"]
