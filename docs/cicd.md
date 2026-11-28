# CI/CD для системы бронирования репетиционных баз «JamSlot»

Документ описывает настройку непрерывной интеграции и доставки (CI/CD) для
микросервисной системы JamSlot, реализованной в лабораторной работе №4.

## 1. Цель и объём

Настроить CI/CD для микросервисной системы: автоматическая сборка, тестирование,
сборка Docker-образов, публикация в реестр и автоматический деплой в
тестовое (staging) окружение.

Система состоит из 5 бизнес-сервисов и API Gateway:

| Сервис | `SERVICE_DIR` | `PACKAGE` | Назначение |
|--------|---------------|-----------|------------|
| `api-gateway` | `gateway` | `gateway` | Единая точка входа |
| `venue-service` | `venue-service` | `venue_service` | Управление площадками |
| `booking-service` | `booking-service` | `booking_service` | Бронирования |
| `equipment-service` | `equipment-service` | `equipment_service` | Инструменты и оборудование |
| `payment-service` | `payment-service` | `payment_service` | Платежи (мок ЮKassa) |
| `notification-service` | `notification-service` | `notification_service` | Уведомления (SMTP) |

Инфраструктура: PostgreSQL 17, RabbitMQ 4, WireMock (мок ЮKassa), Mailpit (SMTP).

## 2. Выбор платформы CI/CD

Выбрана **GitHub Actions**. Обоснование:

- **Репозиторий уже на GitHub** — не требуется разворачивать отдельный
  CI-сервер (Jenkins/TeamCity) и настраивать вебхуки.
- **Матричные сборки** (`strategy.matrix`) позволяют одним workflow параллельно
  проверить сразу все сервисы, не дублируя конфигурацию.
- **Встроенное хранилище секретов** (`GITHUB_TOKEN`, `secrets.*`) — закрывает
  требование «секреты не хранятся в коде».
- **Интеграция с Pull Request** — статус проверок отображается прямо в PR.
- **GitHub Container Registry (GHCR)** доступен «из коробки» через `GITHUB_TOKEN`.
- **Поддержка self-hosted runner** — позволяет выполнять деплой локально на
  машине разработчика (MacBook Air M1, ARM64) без публичного IP и проброса портов.
- **Бесплатный тариф** для публичных репозиториев.

## 3. Архитектура пайплайна

Workflow описан в файле `.github/workflows/ci-cd.yml` и состоит из четырёх
джобов, связанных зависимостями `needs`:

```
                ┌──────────────────────────────┐
                │  test (матрица: 6 сервисов)  │
                └──────────────┬───────────────┘
                               │ needs
                ┌──────────────▼───────────────┐
                │  build-image (матрица: 6)    │
                └──────┬────────────────┬──────┘
                       │ needs          │ needs
          ┌────────────▼───────┐  ┌─────▼──────────────────────────┐
          │ publish (main)     │  │ deploy-staging (lab5 / manual) │
          │ → GHCR booking-svc │  │ → self-hosted runner (Mac)     │
          └────────────────────┘  └────────────────────────────────┘
```

| Джоб | Что делает | Условие запуска |
|------|------------|-----------------|
| `test` | Устанавливает зависимости, запускает unit- и integration-тесты для каждого сервиса | push в `main`/`master`/`lab5`, PR в `main`/`master` |
| `build-image` | Собирает Docker-образ каждого сервиса (ARM64) из общего `services/Dockerfile` | после успешного `test` |
| `publish` | Публикует образ `booking-service` в GHCR с тегами `latest` и `<sha>` | только push в `main`/`master` |
| `deploy-staging` | Деплой staging-стека на self-hosted runner (Mac) | push в `lab5` или `workflow_dispatch` |

## 4. Триггеры

```yaml
on:
  push:
    branches: [ main, master, lab5 ]
  pull_request:
    branches: [ main, master ]
  workflow_dispatch:
```

- `push` в `lab5` — рабочий прогон CI + деплой в staging.
- `pull_request` в `main`/`master` — CI без CD (publish и deploy не запускаются).
- `push` в `main`/`master` (после merge) — CI + publish в GHCR.
- `workflow_dispatch` — ручной запуск из UI GitHub (Actions → Run workflow),
  удобно для повторного деплоя или демонстрации.

## 5. Этап `test`

Матрица из 6 сервисов, `fail-fast: false` — падение одного сервиса не отменяет
проверку остальных. Шаги:

1. `actions/checkout@v4` — развёртывание кода.
2. `actions/setup-python@v5` — Python 3.13.
3. `actions/cache@v4` — кэш pip по хэшу `requirements*.txt`.
4. `pip install -r requirements-dev.txt`.
5. `pytest tests/unit -v --tb=short`.
6. `pytest tests/integration -v --tb=short`.

## 6. Этап `build-image`

Матрица `include` задаёт пары `service`/`dir`/`package` (для gateway `dir`
и `package` равны `gateway`, для остальных — имя сервиса и `*_service`).

Шаги:

1. `actions/checkout@v4`.
2. **`Set lowercase image name`** — приводит `$GITHUB_REPOSITORY` к нижнему
   регистру (`IMAGE_NAME_LC`). Требование OCI: имя репозитория в реестре
   должно быть в нижнем регистре. `1Hav1k` содержит заглавную `H`, поэтому
   без этого шага сборка падает с `repository name must be lowercase`.
3. **`docker/setup-qemu-action@v3`** — регистрирует binfmt-обработчики QEMU
   для эмуляции ARM64 на x86_64-раннере. Без этого команды внутри Dockerfile
   (например, `useradd`) падают с кодом 255.
4. `docker/setup-buildx-action@v3`.
5. `docker/build-push-action@v6`:
   - `context: ./services`, `file: ./services/Dockerfile`;
   - `build-args: SERVICE_DIR=..., PACKAGE=...`;
   - `platforms: linux/arm64`;
   - `push: false` (только валидация сборки);
   - `cache-from: type=gha`, `cache-to: type=gha,mode=max`.

## 7. Этап `publish`

Выполняется только для `booking-service` (требование ЛР: CD хотя бы для одного
сервиса) и только при push в `main`/`master`.

Шаги:

1. `Set lowercase image name` (см. выше).
2. `docker/setup-qemu-action@v3`.
3. `docker/login-action@v3` — логин в GHCR через `GITHUB_TOKEN`.
4. `docker/build-push-action@v6`:
   - `push: true`;
   - теги:
     - `ghcr.io/<owner>/<repo>/booking-service:latest`,
     - `ghcr.io/<owner>/<repo>/booking-service:<sha>`;
   - `platforms: linux/arm64`.

Права:

```yaml
permissions:
  contents: read
  packages: write
```

`latest` — удобный «плавающий» тег; `<sha>` — иммутабельный, привязанный к
конкретному коммиту, что обеспечивает воспроизводимость.

## 8. Этап `deploy-staging`

Выполняется на **self-hosted runner** (MacBook Air M1) с метками
`[self-hosted, macOS, ARM64]`. Условие:

```yaml
if: github.ref == 'refs/heads/lab5' || github.event_name == 'workflow_dispatch'
```

Шаги:

1. `actions/checkout@v4` с `clean: true`.
2. Проверка Docker: версия, архитектура (`arm64`).
3. Остановка старых контейнеров:
   `docker compose -f docker-compose.staging.yml down --remove-orphans`.
4. Сборка и запуск: `docker compose -f docker-compose.staging.yml up -d --build`.
5. Health-check: ожидание `http://localhost:9000/health` (до 60 секунд),
   при неудаче — вывод логов и `exit 1`.

### 8.1. Архитектура деплоя

```
GitHub Actions (облако)
        │  (раннер забирает задание по исходящему HTTPS)
        ▼
┌─────────────────────────────────┐
│  MacBook Air M1 (ARM64)         │
│  ├── GitHub Actions Runner      │
│  │   (labels: self-hosted,      │
│  │    macOS, ARM64)             │
│  ├── Docker Desktop             │
│  └── Workspace раннера          │
│       └── docker-compose.staging.yml │
└─────────────────────────────────┘
```

### 8.2. Установка self-hosted runner

1. **Settings → Actions → Runners → New self-hosted runner**, выбрать
   macOS / ARM64.
2. Выполнить команды из UI: скачать архив, распаковать,
   `./config.sh --url ... --token ...`. При конфигурации указать метку
   `ARM64` (метки `self-hosted` и `macOS` добавляются автоматически).
3. Установить как сервис:
   ```bash
   ./svc.sh install
   ./svc.sh start
   ./svc.sh status
   ```
4. Убедиться, что в **Settings → Actions → Runners** раннер отображается
   как **Idle**.

### 8.3. Требования к машине

- **Docker Desktop for Apple Silicon** установлен и запущен.
- Включён автозапуск Docker Desktop при входе в систему.
- Mac не уходит в сон при подключённом питании
  (**System Settings → Battery → Options → Prevent automatic sleeping on
  power adapter**).

### 8.4. Безопасность self-hosted runner

- Раннер зарегистрирован только для этого репозитория (не для организации).
- Деплой выполняется только при push в `lab5` (доступ только у владельца)
  или вручную (`workflow_dispatch` требует прав write).
- PR из форков деплой не запускают, так как `github.ref` для них —
  `refs/pull/.../merge`.
- Не рекомендуется использовать self-hosted раннер на публичных репозиториях
  с недоверенными контрибьюторами. Для учебного проекта это допустимо.

## 9. Staging-окружение

Файл `docker-compose.staging.yml` в корне репозитория. Отличия от локального
`docker-compose.yml`:

- `name: jamslot-staging` — отдельный проект, своя сеть;
- отдельный volume `pgdata_staging` — данные staging не смешиваются с локальными;
- сдвинутые host-порты — staging и локальное окружение работают одновременно;
- `ENV: staging` в общих переменных;
- `PUBLIC_APP_URL: http://localhost:9000`.

| Сервис | Локально | Staging |
|--------|----------|---------|
| API Gateway | 8000 | **9000** |
| PostgreSQL | 5433 | **5434** |
| RabbitMQ UI | 15672 | **15673** |
| WireMock | 8089 | **8090** |
| Mailpit | 8025 | **8026** |

Проверка после деплоя: `curl -sf http://localhost:9000/health`.

## 10. Безопасность и секреты

- Публикация в GHCR и checkout выполняются через автоматический
  `GITHUB_TOKEN`, который GitHub выдаёт на время прогона. Дополнительные
  секреты не требуются и в репозитории не хранятся.
- В `publish` явно выставлены минимальные права:
  `contents: read`, `packages: write`.
- Self-hosted runner не использует SSH-секреты: деплой выполняется локально.
- Никакие ключи, пароли и токены не коммитятся. При необходимости добавить
  сторонние секреты (например, Docker Hub) они задаются в
  **Settings → Secrets and variables → Actions**.

## 11. Кэширование и скорость

- **Кэш pip** (`actions/cache@v4` по ключу `hashFiles('**/requirements*.txt')`) —
  зависимости переустанавливаются только при изменении файлов требований.
- **Кэш Docker-слоёв** (`cache-from: type=gha`, `cache-to: type=gha,mode=max`) —
  повторная сборка использует слои из предыдущих прогонов.
- **`fail-fast: false`** в матрице — падение одного сервиса не отменяет
  проверку остальных, что ускоряет диагностику.

## 12. Как проверить локально (без GitHub Actions)

```bash
# 1. Установить зависимости
pip install -r requirements-dev.txt

# 2. Запустить unit-тесты
pytest tests/unit -v

# 3. Запустить integration-тесты
pytest tests/integration -v

# 4. Собрать Docker-образ одного сервиса вручную
docker build \
  -f services/Dockerfile \
  --build-arg SERVICE_DIR=booking-service \
  --build-arg PACKAGE=booking_service \
  -t booking-service:local \
  services

# 5. Поднять staging-стек локально
docker compose -f docker-compose.staging.yml up -d --build
docker compose -f docker-compose.staging.yml ps
curl -sf http://localhost:9000/health && echo OK
docker compose -f docker-compose.staging.yml down
```

## 13. Как проверить на GitHub

1. Открыть вкладку **Actions**:
   `https://github.com/1Hav1k/jamslot/actions`.
2. Выбрать workflow **CI/CD Pipeline** и последний запуск.
3. Убедиться, что `test` и `build-image` зелёные.
4. Для прогона на `main` проверить, что `publish` выполнился.
5. Для прогона на `lab5` проверить, что `deploy-staging` выполнился.
6. Перейти в раздел **Packages** и убедиться, что появился пакет
   `booking-service` с тегами `latest` и `<sha>`.
7. На Mac проверить staging:
   ```bash
   docker compose -f docker-compose.staging.yml ps
   curl -i http://localhost:9000/health
   ```

## 14. Демонстрация (скриншоты)

### 14.1. Успешный прогон на `lab5`

![alt text](<Снимок экрана 2026-11-28 в 14.09.29.JPG>)

Пояснение: CD-этапы намеренно ограничены — `publish` только для `main`,
`deploy-staging` только для `lab5`/`workflow_dispatch`.

### 14.2. Успешный прогон на `main` с публикацией образа

![alt text](<Снимок экрана 2026-11-28 в 14.45.56.JPG>)

### 14.3. Публикация в GHCR

![alt text](<Снимок экрана 2026-11-28 в 14.47.31.JPG>)

### 14.4. Деплой в staging

![alt text](<Снимок экрана 2026-11-28 в 14.16.41.JPG>)

### 14.5. Статус self-hosted runner

![alt text](<Снимок экрана 2026-11-28 в 14.15.16.JPG>)

### 14.6. Staging на Mac

![alt text](<Снимок экрана 2026-11-28 в 14.13.36.JPG>)

### 14.7. Неуспешный прогон

![alt text](<Снимок экрана 2026-11-28 в 14.17.20.JPG>)

Как воспроизвести: временно добавить `assert False` в любой unit-тест,
сделать коммит и push. Пайплайн упадёт на джобе `test`, а `build-image`,
`publish` и `deploy-staging` не запустятся (благодаря `needs`). После
исправления вернуть тест в исходное состояние.