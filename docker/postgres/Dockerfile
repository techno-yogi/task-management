FROM postgres:16

RUN apt-get update \
  && apt-get install -y --no-install-recommends openssl \
  && rm -rf /var/lib/apt/lists/*

COPY docker/postgres/docker-entrypoint-ssl.sh /usr/local/bin/docker-entrypoint-ssl.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-ssl.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-ssl.sh"]
