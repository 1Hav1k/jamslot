# Общий образ для всех Python-сервисов JamSlot.
# Сборка: docker build -f services/Dockerfile --build-arg SERVICE_DIR=booking-service \
#           --build-arg PACKAGE=booking_service services
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Сначала зависимости — этот слой кэшируется и не пересобирается при правке кода сервиса
COPY common/pyproject.toml /srv/common/pyproject.toml
COPY common/jamslot_common /srv/common/jamslot_common
RUN pip install /srv/common

ARG SERVICE_DIR
ARG PACKAGE
COPY ${SERVICE_DIR}/${PACKAGE} /srv/${PACKAGE}

ENV PACKAGE=${PACKAGE}
RUN useradd --system --uid 10001 app
USER app

EXPOSE 8080
# Один процесс на контейнер: фоновые потоки (outbox, подписки, планировщик) живут внутри воркера
CMD gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 8 --timeout 60 \
    --access-logfile - --access-logformat '%(h)s "%(r)s" %(s)s %(L)ss' "${PACKAGE}.main:create_app()"
