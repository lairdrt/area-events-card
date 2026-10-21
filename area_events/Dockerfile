# Area Events engine as a Home Assistant app. Standard library only: nothing to pip install.
FROM ghcr.io/home-assistant/base:3.24

RUN apk add --no-cache python3

WORKDIR /app
COPY discovery/ /app/discovery/

COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
