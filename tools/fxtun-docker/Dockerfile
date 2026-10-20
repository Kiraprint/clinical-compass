FROM alpine:3.20

RUN apk add --no-cache socat

COPY fxtunnel /usr/local/bin/fxtunnel
RUN chmod +x /usr/local/bin/fxtunnel

# Наш сервис на хосте доступен через host.docker.internal:8090 (--add-host=host-gateway).
# fxtunnel http <port> всегда шлёт на 127.0.0.1:<port> внутри контейнера, поэтому
# socat релеит 127.0.0.1:8090 -> host.docker.internal:8090 (мимо xray-TUN).
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
