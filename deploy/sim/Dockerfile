# A device TARS runs on, simulated in a container (deploy/sim/run.sh builds it from a profile in devices/): its OS,
# its architecture, the system packages TARS installs on it, a normal user, and TARS run from the clone mounted at
# ~/voice-assistant. Not the device's speed, its audio hardware or systemd.
ARG BASE=debian:bookworm
FROM ${BASE}
ARG PACKAGES=pi-packages.txt
ARG USER_NAME=pi

COPY ${PACKAGES} /tmp/packages.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git procps sudo \
      $(grep -v '^\s*#' /tmp/packages.txt | grep -v '^\s*$') \
 && apt-get clean
# The cache folder holds a volume (the browser's profile, the Python environment): it starts with this owner.
RUN useradd -m -s /bin/bash ${USER_NAME} && echo "${USER_NAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/sim \
 && mkdir -p /home/${USER_NAME}/.cache && chown ${USER_NAME}:${USER_NAME} /home/${USER_NAME}/.cache
# The clone is the host's, owned by the host's user: git is told it's this one's, as the device's own clone is.
RUN git config --system --add safe.directory /home/${USER_NAME}/voice-assistant
USER ${USER_NAME}
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
# The clone is the host's, mounted: the device's Python environment goes in the volume, leaving the host's .venv be.
ENV PATH=/home/${USER_NAME}/.local/bin:$PATH \
    UV_PROJECT_ENVIRONMENT=/home/${USER_NAME}/.cache/tars-venv \
    UV_LINK_MODE=copy
WORKDIR /home/${USER_NAME}/voice-assistant
COPY --chmod=755 sim/entrypoint.sh /usr/local/bin/tars-sim
ENTRYPOINT ["tars-sim"]
CMD ["sleep", "infinity"]
