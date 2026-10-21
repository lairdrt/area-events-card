# Area Events engine as a Home Assistant app. Standard library only: nothing to pip install.
# The Home Assistant Python base image is multi-architecture (amd64 and aarch64, so Raspberry Pi 4 works) and
# includes the s6 init, bashio and Python, so building needs no package downloads.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24

WORKDIR /app
COPY discovery/ /app/discovery/

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
