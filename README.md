# dbadmin-mcp

MCP-сервер для эксплуатации баз данных: размеры объектов, блокировки, планы запросов,
диагностика «почему тормозит» и удаление по отбору под контролем.

Поддерживает **PostgreSQL** (psycopg) и **MS SQL Server** (pyodbc), понимает терминологию
**1С:Предприятие** — сопоставляет таблицы `_inforg1444` с объектами «РегистрСведений.ЗамерыВремени».

```
вопрос: «какая таблица самая большая?»          → top_tables
вопрос: «почему тормозит проведение?»           → posting_bottlenecks, locks, slow_queries
вопрос: «удали записи за 2024 год по складу»    → delete_preview → delete_apply | delete_script
```

## Возможности

| Инструмент | Что делает |
|---|---|
| `list_connections` | настроенные базы, драйвер, режим доступа |
| `db_overview` | версия, размер, число таблиц, соединения, наличие `pg_stat_statements` |
| `top_tables` | крупнейшие таблицы: данные, индексы, TOAST, строки, seq/idx scans, статистика |
| `top_indexes` | крупнейшие индексы и число сканов |
| `db_health` | кэш, соединения, откаты, временные файлы, долгие транзакции, мёртвые строки |
| `maintenance_report` | autovacuum-настройки, мёртвые строки, последние vacuum/analyze, xid, bloat |
| `locks` | кто кого блокирует прямо сейчас, с текстами запросов |
| `slow_queries` | топ по суммарному времени или подсказка, как включить `pg_stat_statements` |
| `posting_bottlenecks` | сводка «почему тормозит проведение» и что делать |
| `table_profile` | профиль таблицы: индексы, колонки, vacuum/analyze, FK |
| `run_select` | одиночный SELECT в read-only транзакции |
| `explain` | `EXPLAIN (ANALYZE, BUFFERS)` для SELECT |
| `delete_preview` | сколько строк и объёма затронет удаление, примеры, FK, token |
| `delete_apply` | бэкап во временную таблицу → DELETE → commit, при ошибке rollback |
| `delete_script` | SQL-скрипт удаления для ручного запуска |

При наличии карты 1С (`onec_map`) в `top_tables` и `table_profile` появляется колонка
«объект 1С».

## Установка

```bash
git clone https://github.com/legavaz/dbadmin-mcp.git
cd dbadmin-mcp
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

Для MS SQL дополнительно:

```bash
pip install -e ".[mssql]"
```

и установленный **ODBC Driver 18 for SQL Server**.

## Конфигурация

```bash
copy examples\dbadmin.example.json dbadmin.json
```

```json
{
  "connections": {
    "onec": {
      "title": "1С на PostgreSQL",
      "driver": "postgres",
      "host": "127.0.0.1",
      "port": 5432,
      "dbname": "base_name",
      "user": "readonly_user",
      "password": "***",
      "readonly": true,
      "onec_map": "onec_tables_base_name.json"
    }
  }
}
```

| Поле | Смысл |
|---|---|
| `driver` | `postgres` или `mssql` |
| `readonly` | `true` — PostgreSQL отклоняет запись на уровне сессии (`default_transaction_read_only`) |
| `allow_write` | разрешает `delete_apply` писать в это подключение |
| `odbc_driver` | для MS SQL, по умолчанию `ODBC Driver 18 for SQL Server` |
| `onec_map` | путь к карте «таблица → объект 1С» (относительно конфига) |

Пути переопределяются переменными: `DBADMIN_CONFIG`, `DBADMIN_HOME`, `DBADMIN_LOG`,
`DBADMIN_CACHE`. По умолчанию конфиг ищется в `./dbadmin.json`, журнал — в
`~/.dbadmin/dbadmin_mcp.log`.

## Подключение к MCP-клиенту

**OpenCode** (`opencode.jsonc`):

```jsonc
"dbadmin": {
  "type": "local",
  "command": [
    "E:\\path\\to\\dbadmin-mcp\\.venv\\Scripts\\python.exe",
    "-m", "dbadmin_mcp"
  ],
  "environment": {
    "PYTHONIOENCODING": "utf-8",
    "DBADMIN_CONFIG": "E:\\path\\to\\dbadmin-mcp\\dbadmin.json"
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "dbadmin": {
      "command": "python",
      "args": ["-m", "dbadmin_mcp"],
      "env": { "DBADMIN_CONFIG": "C:\\path\\to\\dbadmin.json" }
    }
  }
}
```

Транспорт — stdio, реализован без внешних MCP-библиотек: обычный JSON-RPC построчно.

## Безопасность

Главное правило: **роль в базе — это граница, флаги сервера — только страховка.**

```sql
create role mcp_reader login password '...';
grant connect on database base_name to mcp_reader;
grant usage on schema public to mcp_reader;
grant select on all tables in schema public to mcp_reader;
alter default privileges in schema public grant select on tables to mcp_reader;
alter role mcp_reader set default_transaction_read_only = on;
```

Удаление выполняется в три шага:

1. `delete_preview` — показывает, сколько строк попадёт под отбор, объём, примеры,
   внешние ключи, предупреждает про таблицы 1С, выдаёт token на 30 минут;
2. `delete_apply(token, confirm="DELETE")` — создаёт `backup.<таблица>_<время>`
   через `create table ... as select`, затем удаляет пачкой с `limit`, затем commit;
   при любой ошибке — rollback;
3. `delete_script(token)` — то же самое в виде SQL-скрипта для запуска человеком.

Условие отбора обязательно. Отклоняются: пустой `where`, `;`, `--`, `/*`, DDL/DML
внутри условия, удаление всей таблицы.

Дополнительно: `run_select` принимает только `SELECT / WITH / VALUES / TABLE / EXPLAIN`,
один запрос, лимит 500 строк, `statement_timeout` 30 с.

## Карта объектов 1С

Имена таблиц 1С (`_inforg1444`) восстанавливаются офлайн, без запуска 1С:

* `params` → файл `DBNames` — это deflate-структура `GUID → код → номер таблицы`;
* `ConfigDumpInfo.xml` из выгрузки конфигурации — `GUID → имя объекта`.

```bash
python -m dbadmin_mcp.onec_map \
  --connection onec \
  --dump "C:\src\cf\ConfigDumpInfo.xml" \
  --out onec_tables_base_name.json
```

```json
{ "tables": { "_inforg1444": "РегистрСведений.ЗамерыВремени" } }
```

Скрипт сам связывает табличные части (`_reference2220_vt2229`) с объектом-владельцем.

## Ограничения

* `top_tables` на базах с 10 000+ таблиц переключается в быстрый режим: размер
  оценивается по `pg_class.relpages` (колонки помечены «≈»), результат кэшируется
  на 15 минут. Точный обход — `exact=true`, он дольше таймаута на больших базах.
* `pg_stat_statements` не включён по умолчанию — без него нет истории запросов.
* `slow_queries` и `posting_bottlenecks` для MS SQL дают сокращённый набор метрик.
* Удаление для MS SQL поддерживается, но протестировано меньше, чем PostgreSQL.

## Разработка

```bash
pip install -e ".[dev]"
pytest
```

```
src/dbadmin_mcp/
  server.py     — MCP-сервер: подключения, инструменты, транспорт
  onec_map.py   — сборка карты «таблица → объект 1С»
tests/
  test_guards.py
examples/
  dbadmin.example.json
```

## Лицензия

MIT — см. [LICENSE](LICENSE).
