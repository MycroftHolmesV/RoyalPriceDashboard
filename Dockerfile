ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.06.1
FROM ${BUILD_FROM}

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.5.2

ENV PYTHONUNBUFFERED=1 \
    ROYAL_PRICE_DATA_DIR=/data \
    ROYAL_PRICE_HOST=0.0.0.0 \
    ROYAL_PRICE_OPTIONS_FILE=/data/options.json \
    ROYAL_PRICE_UPSTREAM_SCRIPT=/opt/upstream/RoyalPriceDashboardBrowse.py \
    ROYAL_PRICE_PINNED_SCRIPT=/opt/upstream/BrowseRoyalCaribbeanPrice.py

RUN addgroup -S royalprice \
    && adduser -S -D -H -G royalprice royalprice \
    && mkdir -p /app/static /opt/upstream \
    && pip3 install --no-cache-dir requests==2.34.2 curl-cffi==0.16.2

ADD https://raw.githubusercontent.com/jdeath/CheckRoyalCaribbeanPrice/bf5212c26576d468a6af2043565ece2d01f8b503/BrowseRoyalCaribbeanPrice.py /opt/upstream/BrowseRoyalCaribbeanPrice.py
COPY upstream_adapter.py /opt/upstream/RoyalPriceDashboardBrowse.py

RUN echo "ba57bff356d7739158af83a991f2a79de2be583572def0039e73a103244cfa01  /opt/upstream/BrowseRoyalCaribbeanPrice.py" | sha256sum -c - \
    && chmod 0444 /opt/upstream/BrowseRoyalCaribbeanPrice.py \
        /opt/upstream/RoyalPriceDashboardBrowse.py

COPY server.py /app/server.py
COPY static/ /app/static/
COPY upstream-LICENSE /opt/upstream/LICENSE
COPY rootfs/ /

RUN chmod 0755 /etc/services.d/royal-price-dashboard/run \
    && chown -R royalprice:royalprice /app

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"
