"""
Минимальное веб-приложение для практических работ №2 и №4 (ОВС).

ПР №2 (балансировка):
  1. Каждый ответ явно указывает, с какого backend-узла он получен
     (hostname процесса + NODE_ID из окружения, если задан).
  2. Приложение не терминирует TLS/SSL - работает только по HTTP,
     сертификаты и HTTPS настраиваются снаружи (Apache/Nginx).
  3. Пользовательские сессии не реализуются.

ПР №4 (отказоустойчивый кластер PostgreSQL + Patroni + HAProxy):
  - запись идёт через write-endpoint HAProxy (порт 5000, всегда primary),
    чтение - через read-endpoint (порт 5001, реплики);
  - приложение НИКОГДА не обращается к конкретному узлу PostgreSQL;
  - соединение с БД шифруется (sslmode=verify-full по умолчанию);
  - при обрыве соединения/переключении primary запрос повторяется
    с новым соединением в течение PG_RETRY_SECONDS;
  - в каждом ответе видно, какой узел БД обслужил запрос
    (pg_is_in_recovery, адрес сервера, факт использования SSL).

Веб-часть использует только стандартную библиотеку Python; для работы
с PostgreSQL нужен драйвер psycopg2 (см. requirements.txt). Без драйвера
или без настроенного PG_WRITE_HOST приложение работает как в ПР №2,
а /api/write, /api/read и /dbz возвращают 503.
"""

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    import psycopg2
    import psycopg2.errors
except ImportError:  # приложение остаётся работоспособным без драйвера
    psycopg2 = None

START_TIME = time.monotonic()
START_TIME_ISO = datetime.now(timezone.utc).isoformat()

HOSTNAME = socket.gethostname()
NODE_ID = os.environ.get("NODE_ID", HOSTNAME)
PORT = int(os.environ.get("PORT", "8080"))

# --- Подключение к кластеру PostgreSQL (только через HAProxy) ---------------
PG_WRITE_HOST = os.environ.get("PG_WRITE_HOST", "")  # адрес HAProxy
PG_WRITE_PORT = int(os.environ.get("PG_WRITE_PORT", "5000"))
PG_READ_HOST = os.environ.get("PG_READ_HOST", PG_WRITE_HOST)
PG_READ_PORT = int(os.environ.get("PG_READ_PORT", "5001"))
PG_DB = os.environ.get("PG_DB", "ovs")
PG_USER = os.environ.get("PG_USER", "ovs_app")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_SSLMODE = os.environ.get("PG_SSLMODE", "verify-full")
PG_SSLROOTCERT = os.environ.get("PG_SSLROOTCERT", "")
PG_CONNECT_TIMEOUT = int(os.environ.get("PG_CONNECT_TIMEOUT", "3"))
PG_RETRY_SECONDS = float(os.environ.get("PG_RETRY_SECONDS", "30"))
PG_RETRY_DELAY = float(os.environ.get("PG_RETRY_DELAY", "1"))

# Счётчик запросов хранится только в памяти конкретного процесса и
# используется исключительно для наглядной демонстрации в интерфейсе
# (не является пользовательской сессией и не требует персистентности:
# при перезапуске ноды простое обнуление счётчика не влияет на
# корректность работы приложения).
_request_count = 0
_lock = threading.Lock()


def next_request_number() -> int:
    global _request_count
    with _lock:
        _request_count += 1
        return _request_count


def build_info(request_number: int) -> dict:
    return {
        "node_id": NODE_ID,
        "hostname": HOSTNAME,
        "pid": os.getpid(),
        "port": PORT,
        "request_number": request_number,
        "uptime_seconds": round(time.monotonic() - START_TIME, 3),
        "started_at": START_TIME_ISO,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


class DbUnavailable(Exception):
    """БД недоступна: не настроена, нет драйвера или не вернулась за таймаут."""


def _connect(kind: str):
    host, port = (
        (PG_WRITE_HOST, PG_WRITE_PORT) if kind == "write" else (PG_READ_HOST, PG_READ_PORT)
    )
    if psycopg2 is None:
        raise DbUnavailable("драйвер psycopg2 не установлен")
    if not host:
        raise DbUnavailable("не задан PG_WRITE_HOST (адрес HAProxy)")
    params = dict(
        host=host,
        port=port,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
        sslmode=PG_SSLMODE,
        connect_timeout=PG_CONNECT_TIMEOUT,
        application_name=f"ovs-app-{NODE_ID}",
    )
    if PG_SSLROOTCERT:
        params["sslrootcert"] = PG_SSLROOTCERT
    return psycopg2.connect(**params)


def _server_info(cur) -> dict:
    """Какой узел БД реально обслужил запрос и зашифровано ли соединение."""
    cur.execute(
        "SELECT inet_server_addr()::text, inet_server_port(), pg_is_in_recovery(), "
        "(SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid())"
    )
    addr, port, in_recovery, ssl = cur.fetchone()
    return {
        "server_addr": addr,
        "server_port": port,
        "role": "replica" if in_recovery else "primary",
        "pg_is_in_recovery": in_recovery,
        "ssl": ssl,
    }


def run_with_retry(kind: str, work):
    """Выполняет work(cursor) в новом соединении; при сбое повторяет.

    Во время failover старые соединения умирают, а HAProxy несколько секунд
    не имеет доступного primary - поэтому повторяем до PG_RETRY_SECONDS.
    Возвращает (результат, число_попыток, затраченное_время).
    """
    started = time.monotonic()
    attempt = 0
    last_error = None
    while True:
        attempt += 1
        conn = None
        try:
            conn = _connect(kind)
            with conn.cursor() as cur:
                result = work(cur)
            conn.commit()
            return result, attempt, round(time.monotonic() - started, 3)
        except DbUnavailable:
            raise
        except (
            psycopg2.OperationalError,
            psycopg2.InterfaceError,
            psycopg2.errors.ReadOnlySqlTransaction,  # HAProxy ещё вёл на бывший primary
        ) as e:
            last_error = str(e).strip()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if time.monotonic() - started + PG_RETRY_DELAY > PG_RETRY_SECONDS:
            raise DbUnavailable(
                f"{kind}-endpoint недоступен после {attempt} попыток: {last_error}"
            )
        time.sleep(PG_RETRY_DELAY)


def db_write(note: str) -> dict:
    def work(cur):
        cur.execute(
            "CREATE TABLE IF NOT EXISTS ovs_log ("
            "id bigserial PRIMARY KEY, node_id text NOT NULL, note text, "
            "created_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute(
            "INSERT INTO ovs_log (node_id, note) VALUES (%s, %s) RETURNING id, created_at",
            (NODE_ID, note),
        )
        row_id, created_at = cur.fetchone()
        return {"id": row_id, "created_at": created_at.isoformat(), "db": _server_info(cur)}

    result, attempts, elapsed = run_with_retry("write", work)
    result.update(attempts=attempts, elapsed_seconds=elapsed)
    return result


def db_read(limit: int) -> dict:
    def work(cur):
        cur.execute(
            "SELECT id, node_id, note, created_at FROM ovs_log ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = [
            {"id": r[0], "node_id": r[1], "note": r[2], "created_at": r[3].isoformat()}
            for r in cur.fetchall()
        ]
        cur.execute("SELECT count(*) FROM ovs_log")
        return {"total": cur.fetchone()[0], "rows": rows, "db": _server_info(cur)}

    result, attempts, elapsed = run_with_retry("read", work)
    result.update(attempts=attempts, elapsed_seconds=elapsed)
    return result


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>OVS Практика 4 — нода {node_id}</title>
<style>
  body {{ font-family: sans-serif; background: #101418; color: #e6e6e6;
          display: flex; align-items: center; justify-content: center;
          height: 100vh; margin: 0; }}
  .card {{ background: #1b2129; padding: 2rem 3rem; border-radius: 12px;
           box-shadow: 0 4px 20px rgba(0,0,0,.4); }}
  h1 {{ margin-top: 0; }}
  .node {{ font-size: 2rem; font-weight: bold; color: #7cd3ff; }}
  table {{ margin-top: 1rem; border-collapse: collapse; }}
  td {{ padding: .25rem .75rem; }}
  td.key {{ color: #9aa4af; text-align: right; }}
  footer {{ margin-top: 1.5rem; font-size: .8rem; color: #6b7684; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Ответ получен с ноды:</h1>
    <div class="node">{node_id}</div>
    <table>
      <tr><td class="key">hostname</td><td>{hostname}</td></tr>
      <tr><td class="key">pid</td><td>{pid}</td></tr>
      <tr><td class="key">port</td><td>{port}</td></tr>
      <tr><td class="key">номер запроса на этой ноде</td><td>{request_number}</td></tr>
      <tr><td class="key">uptime, сек</td><td>{uptime_seconds}</td></tr>
      <tr><td class="key">запущена в (UTC)</td><td>{started_at}</td></tr>
      <tr><td class="key">время сервера (UTC)</td><td>{server_time}</td></tr>
    </table>
    <footer>JSON-версия: <a style="color:#7cd3ff" href="/api/info">/api/info</a>
      · проверка живости: <a style="color:#7cd3ff" href="/healthz">/healthz</a>
      · БД: <a style="color:#7cd3ff" href="/api/read">/api/read</a>,
      <a style="color:#7cd3ff" href="/dbz">/dbz</a></footer>
  </div>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OVSPractice4/1.0"

    def log_message(self, fmt, *args):
        # Логируем с указанием узла, чтобы при демонстрации в аудитории
        # было видно, какая нода обработала какой запрос.
        print(f"[{NODE_ID}] {self.address_string()} - {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Node-Id", NODE_ID)
        self.send_header("X-Hostname", HOSTNAME)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _db_call(self, func, *args):
        try:
            payload = func(*args)
        except DbUnavailable as e:
            self._send_json(503, {"node_id": NODE_ID, "error": str(e)})
            return
        except Exception as e:  # ошибки SQL (например, на реплике ещё нет таблицы)
            self._send_json(500, {"node_id": NODE_ID, "error": f"{type(e).__name__}: {e}"})
            return
        payload["node_id"] = NODE_ID
        self._send_json(200, payload)

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path != "/api/write":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 65536)) if length else b""
        note = ""
        if raw:
            try:
                note = str(json.loads(raw).get("note", ""))
            except (ValueError, AttributeError):
                self._send_json(400, {"error": 'ожидается JSON вида {"note": "..."}'})
                return
        note = note or parse_qs(url.query).get("note", [""])[0]
        self._db_call(db_write, note[:1000] or f"запись с ноды {NODE_ID}")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/api/read":
            try:
                limit = max(1, min(int(parse_qs(url.query).get("limit", ["20"])[0]), 500))
            except ValueError:
                limit = 20
            self._db_call(db_read, limit)
        elif url.path == "/dbz":
            # Диагностика БД (в отличие от /healthz зависит от кластера).
            out = {"node_id": NODE_ID}
            for kind in ("write", "read"):
                try:
                    res, attempts, _ = run_with_retry(kind, _server_info)
                    out[kind] = {"ok": True, "attempts": attempts, **res}
                except DbUnavailable as e:
                    out[kind] = {"ok": False, "error": str(e)}
            ok = out["write"]["ok"] and out["read"]["ok"]
            self._send_json(200 if ok else 503, out)
        elif url.path == "/":
            info = build_info(next_request_number())
            body = PAGE_TEMPLATE.format(**info).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
        elif url.path == "/api/info":
            info = build_info(next_request_number())
            self._send_json(200, info)
        elif url.path == "/healthz":
            # Намеренно НЕ зависит от БД: падение PostgreSQL не должно
            # выводить веб-ноду из балансировки.
            self._send(200, b"ok", "text/plain; charset=utf-8")
        elif url.path == "/readyz":
            self._send(200, b"ready", "text/plain; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[{NODE_ID}] listening on 0.0.0.0:{PORT} (hostname={HOSTNAME}, pid={os.getpid()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
