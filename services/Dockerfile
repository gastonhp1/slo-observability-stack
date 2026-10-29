# One image for storefront, orders, payments, loadgen and alert-sink.
# The role is chosen at runtime (SHOP_SERVICE env var or the command).
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY shop ./shop

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin shop
USER 10001

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "shop.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
