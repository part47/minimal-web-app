# Подробная инструкция по запуску

### 1. Предварительные требования

Нужен интерпретатор Python версии 3.8 или новее. Веб-часть использует
только стандартную библиотеку. Для работы с PostgreSQL (ПР №4)
дополнительно нужен драйвер `psycopg2` — см. раздел 8; без него
приложение запускается, но эндпоинты БД отвечают `503`.

Проверить, установлен ли Python:

**Windows (PowerShell):**

```powershell
python --version
```

Если команда не найдена, попробуйте `py --version`. Если и это не
сработало — установите Python с [python.org](https://www.python.org/downloads/)
(при установке отметьте галочку "Add python.exe to PATH").

**Linux / macOS (bash):**

```bash
python3 --version
```

Если Python не установлен — поставьте его через пакетный менеджер
дистрибутива (`apt install python3`, `brew install python3` и т.п.).

Далее в примерах используется команда `python`; на Linux/macOS при
необходимости замените её на `python3`.

### 2. Получение исходников

Скачайте/клонируйте репозиторий и перейдите в его папку:

```powershell
git clone <URL-репозитория>
cd ovs-pr2
```

(Если вы уже находитесь в папке проекта — просто пропустите этот шаг.)

Убедитесь, что в папке есть файл `app.py`:

```powershell
Get-ChildItem
```

### 3. Запуск одного экземпляра (быстрая проверка)

**Windows (PowerShell):**

```powershell
python app.py
```

По умолчанию приложение слушает `0.0.0.0:8080`. В консоли появится
строка вида:

```text
[matebook] listening on 0.0.0.0:8080 (hostname=matebook, pid=12345)
```

Откройте в браузере [http://localhost:8080/](http://localhost:8080/) —
должна открыться страница с указанием hostname/PID/номера запроса.
Остановить сервер: `Ctrl+C` в том же окне терминала.

### 4. Запуск нескольких идентичных экземпляров (эмуляция нескольких backend-нод)

Приложение настраивается через переменные окружения:

- `PORT` — порт, на котором слушает инстанс (по умолчанию `8080`).
- `NODE_ID` — метка ноды, которая отображается на странице/в JSON/в
  заголовках ответа (по умолчанию берётся `hostname` процесса).

Так как все инстансы запускаются на одной машине, каждому нужно дать
свой порт, иначе он не сможет запуститься (порт уже занят).

**Windows (PowerShell) — каждый инстанс в отдельном окне терминала:**

Терминал №1:

```powershell
$env:NODE_ID = "node-a"
$env:PORT = "8081"
python app.py
```

Терминал №2 (откройте новое окно PowerShell):

```powershell
$env:NODE_ID = "node-b"
$env:PORT = "8082"
python app.py
```

При желании поднимите и третий инстанс (`node-c`, порт `8083`) — точно
так же в новом окне терминала.

> Переменные `$env:NODE_ID` / `$env:PORT`, заданные так, действуют
> только в текущем окне PowerShell и только до его закрытия — поэтому
> для второго/третьего инстанса нужны отдельные окна, а не одно и то же.

**Linux / macOS (bash) — можно в одной строке, каждый инстанс в фоне:**

```bash
NODE_ID=node-a PORT=8081 python3 app.py &
NODE_ID=node-b PORT=8082 python3 app.py &
```

Посмотреть запущенные фоновые процессы: `jobs`. Остановить все: `kill %1 %2`
(или `pkill -f app.py`).

### 5. Проверка, что каждая нода отвечает от своего имени

**В браузере:** откройте по очереди
[http://localhost:8081/](http://localhost:8081/) и
[http://localhost:8082/](http://localhost:8082/) — на страницах должны
быть разные значения `node-a` / `node-b` и разные PID.

**Через PowerShell (`Invoke-WebRequest`):**

```powershell
(Invoke-WebRequest http://localhost:8081/api/info).Content
(Invoke-WebRequest http://localhost:8082/api/info).Content
```

**Через curl (Windows 10/11, Linux, macOS):**

```bash
curl http://localhost:8081/api/info
curl http://localhost:8082/api/info
```

**Проверка заголовков, идентифицирующих ноду:**

```bash
curl -sD - -o /dev/null http://localhost:8081/
```

В ответе должны быть строки `X-Node-Id: node-a` и `X-Hostname: ...`.

**Проверка health-эндпоинтов (пригодятся для балансировщика/будущего k8s):**

```bash
curl -i http://localhost:8081/healthz
curl -i http://localhost:8081/readyz
```

Оба должны вернуть `200`.

### 6. Остановка приложения

- Если инстанс запущен в текущем окне терминала (не в фоне) —
  нажмите `Ctrl+C` в этом окне.
- Windows, если окно закрыто, а процесс остался: найдите и завершите
  процесс `python.exe` через `Get-Process python | Stop-Process` или
  Диспетчер задач.
- Linux/macOS, фоновые процессы: `kill %1`, `%2` и т.д. (см. `jobs`)
  либо `pkill -f app.py`.

### 7. Типичные проблемы

- **`OSError: [Errno 98] Address already in use` /
  "порт уже используется"** — на этом порту уже запущен другой
  инстанс (или другое приложение). Укажите другой `PORT` или
  остановите процесс, занявший порт.
- **`python: command not found`** — используйте `python3` (Linux/macOS)
  или `py` (Windows), либо установите Python и добавьте его в PATH.
- **`/api/write` отвечает `503: драйвер psycopg2 не установлен` или
  `не задан PG_WRITE_HOST`** — см. раздел 8.
- **Страница не открывается в браузере, хотя в консоли есть строка
  `listening on...`** — проверьте, что открываете правильный порт
  (`http://localhost:PORT/`, а не `8080` по умолчанию, если задавали
  свой `PORT`), и что порт не блокируется локальным firewall.

### 8. Работа с кластером PostgreSQL (ПР №4)

**Установка драйвера:**

```bash
pip install -r requirements.txt
```

**Запуск с подключением к БД через HAProxy** (в `PG_WRITE_HOST` — адрес
ВМ с HAProxy, а не конкретного узла PostgreSQL):

```bash
PG_WRITE_HOST=haproxy.example.local PG_DB=ovs PG_USER=ovs_app \
PG_PASSWORD=секрет PG_SSLROOTCERT=/etc/ssl/ovs/ca.crt \
python3 app.py
```

```powershell
$env:PG_WRITE_HOST = "haproxy.example.local"
$env:PG_PASSWORD = "секрет"
$env:PG_SSLROOTCERT = "C:\certs\ca.crt"
python app.py
```

Порты по умолчанию: запись `5000`, чтение `5001`. Остальные
переменные — в [README.md](README.md). Для проверки без TLS можно
временно задать `PG_SSLMODE=prefer`, но в итоговом стенде должен быть
`verify-full` (п. 10 задания).

**Проверка:**

```bash
curl http://localhost:8080/dbz                       # роли write/read endpoint
curl -X POST -d '{"note":"test"}' http://localhost:8080/api/write
curl http://localhost:8080/api/read?limit=5
```

В ответе `/api/write` поле `db.role` должно быть `primary`, в
`/api/read` — `replica`, `db.ssl` — `true`.

**Проверка failover:** в одном окне запускайте записи в цикле, в другом
выключите ВМ с primary:

```bash
while true; do
  curl -s -X POST -d '{"note":"loop"}' http://localhost:8080/api/write \
    | grep -E '"(id|server_addr|role|attempts|elapsed_seconds)"' | tr -d '\n'; echo
  sleep 1
done
```

Пока лидер не выбран, `attempts`/`elapsed_seconds` растут; после
переключения `server_addr` меняется на адрес нового primary. Разница
времени между последней успешной записью до отказа и первой после —
фактическое время восстановления; пропуски в `id` — потерянные записи.
