"""Доступ к БД сервиса.

Запросы пишутся один раз с именованными параметрами `:name` и выполняются
на PostgreSQL (в docker-compose) или на SQLite (в тестах и при запуске без Docker).
Различия диалектов собраны здесь: стиль параметров, блокировки строк, начало транзакции
и несколько фрагментов DDL.
"""
import contextlib
import logging
import re
import sqlite3
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_PG_ONLY = re.compile(r"\{\{pg:(.*?)\}\}", re.S)


def to_pyformat(sql: str) -> str:
    """`:name` → `%(name)s` для psycopg; литеральный `%` экранируется."""
    return _PARAM.sub(r"%(\1)s", sql.replace("%", "%%"))


class _PsycopgDriver:
    """Боевой драйвер PostgreSQL. Импортируется лениво, чтобы тесты на SQLite не требовали psycopg."""

    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.IntegrityError = psycopg.errors.IntegrityError
        self.OperationalError = psycopg.OperationalError

    def connect(self, url: str):
        return self._psycopg.connect(url, row_factory=self._dict_row)


class Tx:
    """Открытая транзакция. Методы принимают SQL с параметрами `:name`."""

    def __init__(self, conn, dialect: str):
        self._conn = conn
        self.dialect = dialect

    def _run(self, sql: str, params: dict | None):
        params = dict(params or {})
        if self.dialect == "postgresql":
            return self._conn.execute(to_pyformat(sql), params)
        return self._conn.execute(sql, params)

    def execute(self, sql: str, params: dict | None = None) -> int:
        """Выполнить изменение, вернуть число затронутых строк."""
        return self._run(sql, params).rowcount

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        cur = self._run(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: dict | None = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def lock(self, table: str, column: str, value) -> None:
        """Заблокировать строку до конца транзакции.

        В PostgreSQL — SELECT … FOR UPDATE. В SQLite транзакция открывается как
        BEGIN IMMEDIATE и уже сериализует всех писателей, поэтому ничего не делаем.
        """
        if self.dialect == "postgresql":
            self._run(f"SELECT 1 FROM {table} WHERE {column} = :v FOR UPDATE", {"v": value})


class Database:
    def __init__(self, url: str, driver=None):
        self.url = url
        scheme = urlparse(url).scheme
        if scheme.startswith("postgres"):
            self.dialect = "postgresql"
            self._driver = driver or _PsycopgDriver()
            self.integrity_errors = (self._driver.IntegrityError,)
        elif scheme == "sqlite":
            self.dialect = "sqlite"
            self._path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url[len("sqlite://"):]
            self.integrity_errors = (sqlite3.IntegrityError,)
            with contextlib.closing(sqlite3.connect(self._path)) as c:
                c.execute("PRAGMA journal_mode=WAL")
        else:
            raise ValueError(f"Неизвестная схема DATABASE_URL: {scheme}")

    # --- транзакции ---------------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self):
        if self.dialect == "postgresql":
            conn = self._driver.connect(self.url)
            try:
                yield Tx(conn, self.dialect)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(self._path, isolation_level=None, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield Tx(conn, self.dialect)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    # --- схема --------------------------------------------------------------------
    def render_schema(self, ddl: str) -> str:
        pg = self.dialect == "postgresql"
        ddl = _PG_ONLY.sub(lambda m: m.group(1) if pg else "", ddl)
        return ddl.replace("{{serial_pk}}", "BIGSERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT")

    def apply_schema(self, ddl: str, attempts: int = 30, delay: float = 2.0) -> None:
        """Создать таблицы. При старте контейнера БД может быть ещё не готова — ждём."""
        statements = [s.strip() for s in self.render_schema(ddl).split(";\n") if s.strip()]
        retry_on = (self._driver.OperationalError,) if self.dialect == "postgresql" else ()
        for attempt in range(1, attempts + 1):
            try:
                with self.transaction() as tx:
                    for stmt in statements:
                        tx.execute(stmt.rstrip(";"))
                return
            except retry_on as exc:  # БД ещё не принимает подключения
                if attempt == attempts:
                    raise
                log.warning("БД недоступна (%s), повтор через %.0f с", exc.__class__.__name__, delay)
                time.sleep(delay)

    def ping(self) -> bool:
        try:
            with self.transaction() as tx:
                tx.query("SELECT 1 AS ok")
            return True
        except Exception:
            return False
