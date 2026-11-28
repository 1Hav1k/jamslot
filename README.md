# JamSlot — система бронирования репетиционных баз и инструментов

![CI/CD](https://github.com/1Hav1k/jamslot/actions/workflows/ci-cd.yml/badge.svg)

Микросервисная система для бронирования репетиционных баз и музыкальных
инструментов. Клиент взаимодействует только через API Gateway; внутренние
сервисы общаются по HTTP и через RabbitMQ.

## Архитектура

### Сервисы

| Сервис | Назначение |
|--------|------------|
| `api-gateway` | Единая точка входа, маршрутизация, JWT-аутентификация, rate limiting |
| `venue-service` | Управление площадками (репетиционными базами) |
| `booking-service` | Бронирования, hold-механизм, outbox-события |
| `equipment-service` | Инструменты и оборудование |
| `payment-service` | Платежи (мок ЮKassa через WireMock) |
| `notification-service` | Уведомления (SMTP через Mailpit) |

### Инфраструктура

| Компонент | Назначение |
|-----------|------------|
| PostgreSQL 17 | Базы данных сервисов (отдельная БД на сервис) |
| RabbitMQ 4 | Асинхронные события (outbox, подписки) |
| WireMock | Мок платёжного шлюза ЮKassa |
| Mailpit | SMTP-сервер, письма видны в веб-интерфейсе |

### Структура репозитория

```
.
├── .github/workflows/ci-cd.yml     # CI/CD pipeline
├── docs/
│   └── cicd.md                     # описание пайплайна
├── infra/
│   └── postgres/init.sql           # инициализация БД
├── mocks/wiremock/                 # моки ЮKassa
├── services/
│   ├── Dockerfile                  # общий Dockerfile для всех сервисов
│   ├── common/                     # общая библиотека jamslot_common
│   ├── gateway/
│   ├── venue-service/
│   ├── booking-service/
│   ├── equipment-service/
│   ├── payment-service/
│   └── notification-service/
├── tests/
│   ├── unit/
│   └── integration/
├── docker-compose.yml              # локальный запуск
├── docker-compose.staging.yml      # staging-окружение для CI/CD
├── requirements-dev.txt
└── README.md
```

## CI/CD

В проекте настроен пайплайн непрерывной интеграции и доставки на
**GitHub Actions**.

- Конфигурация: [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml)
- Подробное описание: [`docs/cicd.md`](docs/cicd.md)
- 🔗 **Работающий пайплайн:**
  <https://github.com/1Hav1k/jamslot/actions/workflows/ci-cd.yml>

### Этапы пайплайна

| Этап | Действие | Когда запускается |
|------|----------|-------------------|
| **Test** | Матричный запуск unit- и integration-тестов для всех 6 сервисов | push в `main`/`master`/`lab5`, PR в `main`/`master` |
| **Build** | Сборка ARM64 Docker-образов для всех 6 сервисов (`services/Dockerfile`, build-args `SERVICE_DIR`/`PACKAGE`) | после успешного `test` |
| **Publish** | Публикация `booking-service` в GitHub Container Registry (GHCR) с тегами `latest` и `<sha>` | только push в `main`/`master` |
| **Deploy Staging** | Деплой staging-стека на self-hosted runner (MacBook Air M1) через `docker compose` | push в `lab5` или ручной запуск (`workflow_dispatch`) |

Пайплайн также можно запустить вручную: **Actions → CI/CD Pipeline → Run workflow**.

### Особенности реализации

- **Матричные сборки**: 6 сервисов проверяются и собираются параллельно.
- **ARM64**: образы собираются с `platforms: linux/arm64` и `setup-qemu-action`,
  чтобы работать нативно на Apple Silicon.
- **Lowercase-фикс**: имя репозитория приводится к нижнему регистру перед
  формированием тегов GHCR (требование OCI).
- **Кэширование**: pip-зависимости (`actions/cache`) и Docker-слои (`type=gha`).
- **Секреты**: используется только автоматический `GITHUB_TOKEN`; никакие
  ключи не хранятся в коде.
- **CD ограничен ветками**: `publish` — только для `main`, `deploy-staging` —
  только для `lab5`/`workflow_dispatch`. PR не публикуют и не деплоят.

### Деплой в staging

- **Платформа:** MacBook Air M1 (ARM64) с self-hosted GitHub Actions runner.
- **Механизм:** `docker compose -f docker-compose.staging.yml up -d --build`
  прямо на машине разработчика.
- **Метки раннера:** `self-hosted`, `macOS`, `ARM64`.
- **Секреты:** не требуются — деплой выполняется локально.
- **Health-check:** `http://localhost:9000/health`.
- **Изоляция от локального окружения:** отдельный проект `jamslot-staging`,
  отдельный volume `pgdata_staging`, сдвинутые host-порты
  (gateway 9000, postgres 5434, rabbitmq 15673, wiremock 8090, mailpit 8026).

#### Установка self-hosted runner (кратко)

1. **Settings → Actions → Runners → New self-hosted runner** → macOS / ARM64.
2. Выполнить команды из UI (скачивание, распаковка, `./config.sh`).
3. Установить как сервис: `./svc.sh install && ./svc.sh start`.
4. Убедиться, что Docker Desktop запущен, а раннер в статусе **Idle**.

Подробнее — в [`docs/cicd.md`](docs/cicd.md), раздел 8.

#### Как проверить staging-деплой

1. Убедиться, что Mac включён, Docker Desktop запущен, раннер в статусе **Idle**.
2. Сделать push в `lab5` или запустить workflow вручную.
3. Во вкладке **Actions** дождаться зелёного джоба `deploy-staging`.
4. На Mac проверить:
   ```bash
   docker compose -f docker-compose.staging.yml ps
   curl -i http://localhost:9000/health
   ```
5. Открыть в браузере `http://localhost:9000` (API Gateway) или
   `http://localhost:9000/health` — должен вернуться `200 OK`.

### Как проверить локально (без GitHub Actions)

```bash
# Зависимости
pip install -r requirements-dev.txt

# Тесты
pytest tests/unit -v
pytest tests/integration -v

# Сборка образа одного сервиса вручную
docker build \
  -f services/Dockerfile \
  --build-arg SERVICE_DIR=booking-service \
  --build-arg PACKAGE=booking_service \
  -t booking-service:local \
  services
```

## Локальный запуск

```bash
docker compose up -d --build
```

После старта:

| Компонент | URL |
|-----------|-----|
| API Gateway | http://localhost:8000 |
| RabbitMQ UI | http://localhost:15672 (jamslot / jamslot) |
| WireMock (мок ЮKassa) | http://localhost:8089 |
| Mailpit (SMTP UI) | http://localhost:8025 |

Остановка:

```bash
docker compose down          # остановить
docker compose down -v       # остановить и удалить volume с данными
```

## Staging

```bash
docker compose -f docker-compose.staging.yml up -d --build
```

| Компонент | URL |
|-----------|-----|
| API Gateway | http://localhost:9000 |
| RabbitMQ UI | http://localhost:15673 |
| WireMock | http://localhost:8090 |
| Mailpit | http://localhost:8026 |

Остановка:

```bash
docker compose -f docker-compose.staging.yml down
```

## Тесты

```bash
pytest tests/unit -v
pytest tests/integration -v
```

## Документация

- [`docs/cicd.md`](docs/cicd.md) — описание CI/CD пайплайна, staging, troubleshooting.

## Лабораторные работы

- **ЛР3** — архитектура микросервисов (YAML-описания, диаграммы).
- **ЛР4** — контейнеризация, запуск микросервисной системы, сценарии в Postman.
- **ЛР5** — CI/CD: GitHub Actions, GHCR, self-hosted runner, staging-деплой.