"""Минимальный драйвер PostgreSQL для тестов (протокол v3, simple query).

Нужен только потому, что в среде, где писались тесты, нет psycopg. Позволяет прогнать
те же тесты на настоящем PostgreSQL: включается переменной JAMSLOT_TEST_PG
(например, postgresql://postgres@127.0.0.1:55432/postgres). Сервисы в docker-compose
работают через psycopg — этот модуль там не используется.
"""
import re
import socket
import struct
from decimal import Decimal
from urllib.parse import unquote, urlparse

_PY = re.compile(r"%\((\w+)\)s")


class PgError(Exception):
    def __init__(self, fields: dict):
        self.sqlstate = fields.get("C", "")
        super().__init__(f"{self.sqlstate}: {fields.get('M', '')}")


class IntegrityError(PgError):
    pass


class OperationalError(Exception):
    pass


def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


class Cursor:
    def __init__(self, rows, tag):
        self._rows = rows
        nums = [int(x) for x in tag.split() if x.isdigit()]
        self.rowcount = nums[-1] if nums else -1

    def fetchall(self):
        return self._rows


class Connection:
    def __init__(self, url: str):
        u = urlparse(url)
        try:
            self.sock = socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=10)
        except OSError as exc:
            raise OperationalError(str(exc))
        params = {"user": unquote(u.username or "postgres"), "database": u.path.lstrip("/") or "postgres",
                  "client_encoding": "UTF8"}
        body = struct.pack("!i", 196608) + b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in params.items()) + b"\0"
        self.sock.sendall(struct.pack("!i", len(body) + 4) + body)
        self.password = unquote(u.password or "")
        self._read_until_ready()
        self.in_tx = False

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OperationalError("соединение закрыто")
            buf += chunk
        return buf

    def _msg(self):
        t = self._recv(1)
        (ln,) = struct.unpack("!i", self._recv(4))
        return t, self._recv(ln - 4)

    def _send(self, t: bytes, payload: bytes):
        self.sock.sendall(t + struct.pack("!i", len(payload) + 4) + payload)

    def _read_until_ready(self):
        fields, rows, tag, error = [], [], "", None
        while True:
            t, data = self._msg()
            if t == b"R":
                (code,) = struct.unpack("!i", data[:4])
                if code == 3:
                    self._send(b"p", self.password.encode() + b"\0")
                elif code != 0:
                    raise OperationalError(f"метод аутентификации {code} не поддерживается")
            elif t == b"T":
                (n,) = struct.unpack("!h", data[:2])
                pos, fields = 2, []
                for _ in range(n):
                    end = data.index(b"\0", pos)
                    name = data[pos:end].decode()
                    pos = end + 1
                    _tbl, _col, oid, _sz, _mod, _fmt = struct.unpack("!ihihih", data[pos:pos + 18])
                    pos += 18
                    fields.append((name, oid))
            elif t == b"D":
                (n,) = struct.unpack("!h", data[:2])
                pos, row = 2, {}
                for i in range(n):
                    (ln,) = struct.unpack("!i", data[pos:pos + 4])
                    pos += 4
                    if ln == -1:
                        val = None
                    else:
                        raw = data[pos:pos + ln].decode()
                        pos += ln
                        oid = fields[i][1]
                        val = int(raw) if oid in (20, 21, 23) else (raw == "t") if oid == 16 else \
                            float(raw) if oid in (700, 701, 1700) else raw
                    row[fields[i][0]] = val
                rows.append(row)
            elif t == b"C":
                tag = data.rstrip(b"\0").decode()
            elif t == b"E":
                parts = {}
                for item in data.split(b"\0"):
                    if item:
                        parts[chr(item[0])] = item[1:].decode(errors="replace")
                error = parts
            elif t == b"Z":
                if error:
                    cls = IntegrityError if error.get("C", "").startswith("23") else PgError
                    raise cls(error)
                return rows, tag

    def _query(self, sql: str):
        self._send(b"Q", sql.encode() + b"\0")
        return self._read_until_ready()

    def execute(self, sql: str, params=None):
        if params is not None:
            sql = _PY.sub(lambda m: literal(params[m.group(1)]), sql).replace("%%", "%")
        if not self.in_tx:
            self._query("BEGIN")
            self.in_tx = True
        return Cursor(*self._query(sql))

    def commit(self):
        if self.in_tx:
            self._query("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            self._query("ROLLBACK")
            self.in_tx = False

    def close(self):
        try:
            self._send(b"X", b"")
            self.sock.close()
        except OSError:
            pass


class Driver:
    IntegrityError = IntegrityError
    OperationalError = OperationalError

    @staticmethod
    def connect(url: str):
        return Connection(url)


def admin(url: str, sql: str):
    """Выполнить команду вне транзакции (CREATE/DROP DATABASE)."""
    c = Connection(url)
    try:
        return c._query(sql)
    finally:
        c.close()
