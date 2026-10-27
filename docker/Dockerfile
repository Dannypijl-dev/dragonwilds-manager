# Dragonwilds Manager: web panel + the game servers it creates, in one container.
# Data (servers, worlds, backups, settings) lives in /data; mount a volume there.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 \
    DWM_IN_DOCKER=1 DWM_DATA_DIR=/data DWM_PORT=8080
# SteamCMD is a 32-bit program (lib32gcc-s1, lib32stdc++6). nftables + sudo: kicks and the
# whitelist, which only work when the container also gets NET_ADMIN (see docker-compose.yml).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates lib32gcc-s1 lib32stdc++6 nftables sudo tini util-linux \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin dwm \
 && mkdir -p /data && chown dwm:dwm /data

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src \
 && mkdir -p /usr/local/lib/dragonwilds-manager \
 && printf '#!/bin/sh\nexec /usr/local/bin/python -m dwmanager firewall "$@"\n' >/usr/local/lib/dragonwilds-manager/dwm-firewall \
 && chmod 755 /usr/local/lib/dragonwilds-manager/dwm-firewall \
 && echo 'dwm ALL=(root) NOPASSWD: /usr/local/lib/dragonwilds-manager/dwm-firewall' >/etc/sudoers.d/dwm \
 && chmod 440 /etc/sudoers.d/dwm
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh

VOLUME /data
EXPOSE 8080/tcp 7777/udp
STOPSIGNAL SIGTERM
# entrypoint drops to user dwm; tini forwards SIGTERM so the game servers stop cleanly (they save the world)
ENTRYPOINT ["/entrypoint.sh"]
CMD ["dragonwilds-manager", "run"]
