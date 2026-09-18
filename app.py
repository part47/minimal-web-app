"""
Минимальное веб-приложение для практической работы №2 (ОВС).

Требования, которым удовлетворяет реализация:
  1. Каждый ответ явно указывает, с какого backend-узла он получен
     (hostname процесса + NODE_ID из окружения, если задан).
  2. Приложение не хранит данные (БД не используется) -> сценарий
     "недоступность одной из нод хранилища" неприменим по design.
  3. Приложение не терминирует TLS/SSL - работает только по HTTP,
     сертификаты и HTTPS настраиваются снаружи (Apache/Nginx).
  4. Пользовательские сессии не реализуются -> хранение сессий
     в RAM/файлах не требуется и не используется.

Используется только стандартная библиотека Python (без Flask/FastAPI
и т.п.), чтобы минимизировать потребление оперативной памяти и число
внешних зависимостей.
"""

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START_TIME = time.monotonic()
START_TIME_ISO = datetime.now(timezone.utc).isoformat()

HOSTNAME = socket.gethostname()
NODE_ID = os.environ.get("NODE_ID", HOSTNAME)
PORT = int(os.environ.get("PORT", "8080"))

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


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>OVS Практика 2 — нода {node_id}</title>
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
      · проверка живости: <a style="color:#7cd3ff" href="/healthz">/healthz</a></footer>
  </div>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OVSPractice2/1.0"

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

    def do_GET(self):
        if self.path == "/":
            info = build_info(next_request_number())
            body = PAGE_TEMPLATE.format(**info).encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
        elif self.path == "/api/info":
            info = build_info(next_request_number())
            body = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        elif self.path == "/readyz":
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
