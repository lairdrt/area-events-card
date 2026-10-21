#!/command/with-contenv bashio

bashio::log.info "Starting the Area Events engine"
cd /app || exit 1
exec python3 -m discovery
