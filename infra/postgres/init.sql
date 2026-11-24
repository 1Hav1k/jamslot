-- Выполняется один раз при первом запуске контейнера postgres (пустой том pgdata).
-- У каждого сервиса своя база и своя учётная запись: чужие таблицы ему недоступны.

CREATE USER venue_user WITH PASSWORD 'venue_pass';
CREATE DATABASE venue_db OWNER venue_user;

CREATE USER booking_user WITH PASSWORD 'booking_pass';
CREATE DATABASE booking_db OWNER booking_user;

CREATE USER equipment_user WITH PASSWORD 'equipment_pass';
CREATE DATABASE equipment_db OWNER equipment_user;

CREATE USER payment_user WITH PASSWORD 'payment_pass';
CREATE DATABASE payment_db OWNER payment_user;

CREATE USER notification_user WITH PASSWORD 'notification_pass';
CREATE DATABASE notification_db OWNER notification_user;

-- Чужие базы закрыты: по умолчанию PUBLIC может подключаться к любой базе
REVOKE CONNECT ON DATABASE venue_db, booking_db, equipment_db, payment_db, notification_db FROM PUBLIC;
GRANT CONNECT ON DATABASE venue_db TO venue_user;
GRANT CONNECT ON DATABASE booking_db TO booking_user;
GRANT CONNECT ON DATABASE equipment_db TO equipment_user;
GRANT CONNECT ON DATABASE payment_db TO payment_user;
GRANT CONNECT ON DATABASE notification_db TO notification_user;

-- btree_gist нужен для ограничения EXCLUDE (запрет пересечения интервалов брони)
\connect booking_db
CREATE EXTENSION IF NOT EXISTS btree_gist;
