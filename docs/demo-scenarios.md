# Проверенные сценарии ЛР4

Все сценарии запускаются из Postman-коллекции [`postman/JamSlot.postman_collection.json`](../postman/JamSlot.postman_collection.json): у каждого есть своя папка и проверки в Tests. Те же сценарии автоматизированы в pytest: в разделе «Автотест» указано имя теста.

Система поднята через `docker compose up -d --build`. Клиент обращается только к API Gateway на `http://localhost:8000`.

## Что настоящее, а что мок

| Взаимодействие | Как реализовано |
|---|---|
| Клиент → API Gateway → сервисы | **реально**: HTTP, проверка JWT, маршрутизация, `X-Correlation-Id` |
| booking → venue `GET /internal/v1/rooms/{id}/quote` | **реально**: REST между контейнерами |
| booking → equipment `POST /internal/v1/reservations` | **реально**: REST, `Idempotency-Key` |
| booking → payment `POST /internal/v1/payments` | **реально**: REST, `Idempotency-Key` |
| Все события и команда `payment.refund` | **реально**: RabbitMQ, transactional outbox, дедупликация по `eventId`, повторы и DLQ |
| Хранилища сервисов | **реально**: PostgreSQL 17, своя база и своя учётная запись у каждого сервиса |
| payment → ЮKassa (создание платежа, отмена, возврат) | **мок**: WireMock, стабы в [`mocks/wiremock/mappings`](../mocks/wiremock/mappings) |
| ЮKassa → payment (уведомление об оплате) | **мок**: webhook отправляет Postman от имени шлюза |
| Страница оплаты | **мок**: заглушка WireMock `http://localhost:8089/checkout` |
| notification → почта | **мок**: SMTP-сервер Mailpit, письма видны на `http://localhost:8025` |
| Вход пользователя (Keycloak) | **мок**: `POST /auth/token` в Gateway выдаёт JWT демо-пользователям |

Демо-пользователи: `ilya` и `anna` — клиенты, `manager` — менеджер площадки «Гараж 61», `admin` — администратор. Пароль у всех `password`.

Где смотреть, что происходит внутри:
- **логи сервисов:** `docker compose logs -f booking-service payment-service equipment-service notification-service`. В каждой строке есть `correlationId` сценария, по нему цепочку видно целиком;
- **очереди и сообщения:** RabbitMQ — `http://localhost:15672`, логин и пароль `jamslot`;
- **запросы к мок-шлюзу:** `http://localhost:8089/__admin/requests`;
- **письма:** `http://localhost:8025`.

---

## Сценарий 1. Успешное бронирование с арендой оборудования (happy path)

**Папка Postman:** «2. Успешный сценарий (happy path)». **Автотест:** `tests/e2e/test_booking_saga.py::test_happy_path`.

| Шаг | Что происходит | Тип |
|---|---|---|
| 1 | Клиент: `POST /api/v1/bookings` (комната «Красная», 18:00–20:00, гитара) | Gateway → booking |
| 2 | booking → venue: проверка интервала и цена комнаты (1400 ₽) | sync, реально |
| 3 | booking: бронь `PENDING_PAYMENT` в своей БД, интервал удержан на 15 минут | локальная транзакция |
| 4 | booking → equipment: резерв гитары `HELD` (600 ₽) | sync, реально |
| 5 | booking → payment → ЮKassa: платёж `PENDING`, ссылка на оплату | sync реально + мок шлюза |
| 6 | Клиент получает `201`, `status: PENDING_PAYMENT`, `totalPrice: 2000`, `paymentUrl` | |
| 7 | Webhook `payment.succeeded` от имени ЮKassa | мок |
| 8 | payment: платёж `SUCCEEDED`, событие `payment.completed` | async, реально |
| 9 | booking: бронь `CONFIRMED`, событие `booking.confirmed` | async, реально |
| 10 | equipment: резерв `CONFIRMED`; notification: письмо и напоминание | async, реально |

**Ожидаемый результат:** бронь `CONFIRMED`, платёж `SUCCEEDED`, гитара на это время недоступна (`availableQuantity: 0`). В Mailpit — письмо «Бронь подтверждена». У всех событий сценария один `correlationId`, равный `X-Correlation-Id` ответа на шаг 1.

## Сценарий 2. Сбой оплаты и компенсация

**Папка Postman:** «3. Сбой: оплата отклонена». **Автотест:** `test_payment_declined_releases_slot_and_equipment`.

Шаги 1–6 совпадают со сценарием 1. Затем:

| Шаг | Что происходит | Тип |
|---|---|---|
| 7 | Webhook `payment.canceled` (`reason: general_decline`) от имени ЮKassa | мок |
| 8 | payment: платёж `FAILED`, событие `payment.failed` | async, реально |
| 9 | **Компенсация в booking:** бронь `CANCELLED` (`PAYMENT_FAILED`), интервал освобождён, событие `booking.cancelled` | async, реально |
| 10 | **Компенсация в equipment:** резерв `RELEASED`, гитара снова доступна | async, реально |
| 11 | notification: письмо «Бронь отменена» | async, реально + мок SMTP |

**Ожидаемый результат:** бронь `CANCELLED` с причиной `PAYMENT_FAILED`, платёж `FAILED`, `availableQuantity` гитары снова 1, тот же интервал можно забронировать заново.

## Сценарий 3. Нет свободного оборудования (синхронный откат)

**Папка Postman:** «4. Сбой: нет оборудования». **Автотест:** `test_equipment_unavailable_cancels_booking`.

Анна бронирует «Синюю» комнату с гитарой. Илья бронирует «Красную» на то же время с той же гитарой: equipment-service отвечает `409 EQUIPMENT_UNAVAILABLE`, booking-service отменяет свою бронь (`EQUIPMENT_UNAVAILABLE`) и возвращает клиенту тот же `409`. Резерв у соседа не создавался, поэтому событие не публикуется, а платёж не создаётся. Ответ — `application/problem+json` с `detail: "Fender Stratocaster — свободно 0 из 1"`.

## Сценарий 4. payment-service недоступен

**Папка Postman:** «8. Ручной сценарий: payment-service недоступен». **Автотесты:** `test_payment_service_down_compensates`, `test_payment_gateway_down_compensates` (не отвечает мок ЮKassa).

```bash
docker compose stop payment-service
# в Postman: папка 8
docker compose start payment-service
```

booking-service создаёт бронь и резерв, но вызов payment-service не проходит: повтор с тем же `Idempotency-Key`, затем отказ. Компенсация: бронь `CANCELLED` (`PAYMENT_UNAVAILABLE`), публикуется `booking.cancelled`, equipment-service снимает резерв. Клиент получает `503 SERVICE_UNAVAILABLE` с `Retry-After: 5`. Письмо не отправляется: о сбое клиент узнал из ответа API.

Так же ведут себя отказы других соседей: venue недоступен — `503`, ничего не создаётся (`test_venue_service_down_creates_nothing`); equipment недоступен — бронь отменяется, `booking.cancelled` снимает резерв, если он успел создаться (`test_equipment_service_down_compensates`).

## Сценарий 5. Бронь не оплачена вовремя, оплата пришла поздно

**Папка Postman:** «9. Ручной сценарий: бронь истекла, оплата пришла поздно». **Автотест:** `test_expired_hold_then_late_payment_is_refunded`.

```bash
BOOKING_HOLD_SECONDS=30 docker compose up -d booking-service   # удержание 30 с вместо 15 минут
# в Postman: первый запрос папки 9, подождать 40 секунд, затем остальные
docker compose up -d booking-service                            # вернуть 15 минут
```

| Шаг | Что происходит |
|---|---|
| 1 | Планировщик booking-service: срок удержания прошёл → бронь `EXPIRED`, событие `booking.expired` |
| 2 | Компенсации: equipment снимает резерв; payment отменяет платёж у шлюза (мок) → `CANCELLED` |
| 3 | Гонка: шлюз успел провести оплату и присылает `payment.succeeded` |
| 4 | payment: `SUCCEEDED`, `payment.completed`; booking видит бронь `EXPIRED` и не подтверждает её |
| 5 | Компенсация: booking отправляет команду `payment.refund`, payment делает возврат в моке ЮKassa → `REFUNDED`, `payment.refunded`, письмо «Деньги возвращены» |

## Сценарий 6. Отмена оплаченной брони с возвратом

**Папка Postman:** «5. Отмена с возвратом денег». **Автотесты:** `test_user_cancel_more_than_24h_before_refunds`, `test_user_cancel_less_than_24h_before_keeps_money`, `test_manager_sees_and_cancels_with_full_refund`.

Клиент отменяет бронь больше чем за 24 часа до начала: `booking.cancelled` снимает резерв, команда `payment.refund` возвращает деньги. Позже 24 часов возврата нет. Отмена менеджером площадки — всегда полный возврат. Повторная отмена — `409 BOOKING_NOT_CANCELLABLE`.

## Сценарий 7. Идемпотентность

**Папка Postman:** «6. Идемпотентность». **Автотесты:** `test_idempotent_booking_creation`, `test_idempotent_error_is_replayed`.

| Запрос | Ответ |
|---|---|
| `POST /bookings` с ключом K | `201`, бронь создана |
| тот же запрос с ключом K | `201`, та же бронь, заголовок `Idempotent-Replayed: true`; второй платёж в ЮKassa не создаётся |
| ключ K, другое тело | `422 IDEMPOTENCY_KEY_REUSED` |
| без ключа | `400 IDEMPOTENCY_KEY_REQUIRED` |

Шаги саги между сервисами тоже идемпотентны: booking-service передаёт соседям ключи, вычисленные из id брони, поэтому повтор после таймаута не создаёт второй резерв или платёж. Повторная доставка события обрабатывается один раз (`test_duplicate_event_is_processed_once`). Повторный webhook шлюза не меняет статус и не публикует событие второй раз.

## Сценарий 8. Надёжность обмена сообщениями

**Автотесты:** `test_handler_failure_goes_to_dlq_after_retries`, `test_smtp_failure_does_not_block_events`, `tests/unit/test_common.py`.

- Если обработчик падает (например, мок ЮKassa недоступен при отмене платежа), сообщение доставляется повторно, а после 5 попыток уходит в `<очередь>.dlq`. Остальные подписчики то же событие обрабатывают независимо.
- Изменения обработчика откатываются вместе с отметкой об обработке, поэтому повтор начинается с чистого состояния.
- SMTP недоступен — письма копятся в статусе `SCHEDULED` и уходят, когда почта поднимется. Бронирование при этом не блокируется.

## Сценарий 9. Gateway и единая модель ошибок

**Папка Postman:** «7. Gateway и единая модель ошибок». **Автотесты:** `tests/integration/test_services_api.py`.

`/internal/**` снаружи — `404`; без токена — `401`; подделанный заголовок `X-User-Id` игнорируется; ошибка валидации — `400` со списком полей; бизнес-правило — `422`; занятый интервал — `409`; чужая бронь — `403`; превышен лимит — `429` с `Retry-After`. Все ошибки отдаются в формате Problem Details (`application/problem+json`) с полями `type`, `title`, `status`, `code`, `detail`, `instance`, `traceId`.
