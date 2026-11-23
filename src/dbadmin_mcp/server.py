"""MCP-сервер «Администратор БД» (stdio).

Отвечает на вопросы эксплуатации баз 1С и аналитики:
  • какая таблица/индекс самые большие;
  • почему тормозит проведение (блокировки, долгие транзакции, планы);
  • что именно удалит запрос с отбором (preview) и удаление под контролем.

Поддерживаются PostgreSQL (psycopg) и MS SQL Server (pyodbc, ленивый импорт).
Доступ по умолчанию — только чтение; запись включается флагом allow_write
в конфигурации конкретного подключения и подтверждается токеном preview.

Конфигурация: файл из DBADMIN_CONFIG, иначе ./dbadmin.json в текущем каталоге.
Журнал: DBADMIN_LOG или ~/.dbadmin/dbadmin_mcp.log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

HOME_DIR = os.environ.get("DBADMIN_HOME") or os.path.join(os.path.expanduser("~"), ".dbadmin")
CONFIG_PATH = os.environ.get("DBADMIN_CONFIG") or os.path.join(os.getcwd(), "dbadmin.json")
LOG_PATH = os.environ.get("DBADMIN_LOG") or os.path.join(HOME_DIR, "dbadmin_mcp.log")

DEFAULT_ROW_LIMIT = 500
DEFAULT_TIMEOUT_MS = 30_000
PREVIEW_TTL_SEC = 1800
PREVIEWS: dict[str, dict] = {}
CACHE_PATH = os.environ.get("DBADMIN_CACHE") or os.path.join(HOME_DIR, "cache.json")
CACHE_TTL_SEC = 900


# ------------------------------- служебное --------------------------------
def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(f"[dbadmin-mcp] {msg}", file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _empty_config() -> dict:
    """Пустая конфигурация: подключения задаются в dbadmin.json."""
    return {"connections": {}}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if cfg.get("connections"):
                return cfg
            log(f"в {CONFIG_PATH} нет connections")
        except (OSError, json.JSONDecodeError) as exc:
            log(f"не удалось прочитать {CONFIG_PATH}: {exc}")
    else:
        log(f"конфиг не найден: {CONFIG_PATH}")
    return _empty_config()


def _cache_load() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_get(conn_name: str, key: str) -> dict | None:
    item = _cache_load().get(f"{conn_name}:{key}")
    if item and time.time() - item.get("ts", 0) < CACHE_TTL_SEC:
        return item
    return None


def _cache_put(conn_name: str, key: str, headers: list[str], rows: list[list]) -> None:
    data = _cache_load()
    data[f"{conn_name}:{key}"] = {"ts": time.time(), "headers": headers, "rows": rows}
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except OSError as exc:
        log(f"кэш не записан: {exc}")


def get_conn_cfg(name: str) -> dict:
    cfg = load_config()["connections"]
    if name not in cfg:
        available = ", ".join(sorted(cfg)) or "—"
        raise ValueError(
            f"Нет подключения «{name}». Доступны: {available}. "
            f"Конфигурация: {CONFIG_PATH} (образец — dbadmin.example.json)."
        )
    item = dict(cfg[name])
    item.setdefault("driver", "postgres")
    item.setdefault("readonly", True)
    item.setdefault("title", name)
    return item


MAP_CACHE: dict[str, dict[str, str]] = {}


def onec_map(conn_name: str) -> dict[str, str]:
    """Таблица PostgreSQL → имя объекта 1С (файл из `onec_map` в конфиге)."""
    if conn_name in MAP_CACHE:
        return MAP_CACHE[conn_name]
    rel = get_conn_cfg(conn_name).get("onec_map")
    if not rel:
        MAP_CACHE[conn_name] = {}
        return {}
    base = os.path.dirname(os.path.abspath(CONFIG_PATH))
    path = rel if os.path.isabs(rel) else os.path.join(base, rel)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        MAP_CACHE[conn_name] = data.get("tables") or {}
    except (OSError, json.JSONDecodeError) as exc:
        log(f"карта 1С не прочитана ({path}): {exc}")
        MAP_CACHE[conn_name] = {}
    return MAP_CACHE[conn_name]


# ---------------------------- форматирование -------------------------------
def _cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{float(value):.4g}"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} байт>"
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text


def render_table(headers: list[str], rows: list[list], max_width: int = 60) -> str:
    if not rows:
        return "_(пусто)_"
    cells = [[_cell(v)[:max_width] for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def line(values):
        return "| " + " | ".join(
            v.ljust(widths[i]) for i, v in enumerate(values)
        ) + " |"

    out = [line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out.extend(line(row) for row in cells)
    return "\n".join(out)


def rows_as_dicts(cur) -> list[dict]:
    names = [d[0] for d in (cur.description or [])]
    return [dict(zip(names, row)) for row in cur.fetchall()]


# ------------------------------ подключение --------------------------------
def _pg_connect(cfg: dict):
    import psycopg

    options = [f"-c statement_timeout={DEFAULT_TIMEOUT_MS}"]
    if cfg.get("readonly", True):
        # настоящая граница: запись отклонит сам PostgreSQL
        options.append("-c default_transaction_read_only=on")
    params = dict(
        host=cfg["host"], port=int(cfg.get("port") or 5432),
        user=cfg["user"], password=cfg.get("password", ""),
        dbname=cfg["dbname"], connect_timeout=int(cfg.get("connect_timeout") or 10),
        options=" ".join(options),
    )
    return psycopg.connect(**params)


def _mssql_connect(cfg: dict):
    try:
        import pyodbc
    except ImportError as exc:  # noqa: F841
        raise RuntimeError(
            "Для MS SQL нужен pyodbc: .venv\\Scripts\\pip install pyodbc "
            "и ODBC Driver 18 for SQL Server."
        ) from None

    driver = cfg.get("odbc_driver") or "ODBC Driver 18 for SQL Server"
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={cfg['host']},{int(cfg.get('port') or 1433)}",
        f"DATABASE={cfg['dbname']}",
        f"UID={cfg['user']}",
        f"PWD={cfg.get('password', '')}",
        "TrustServerCertificate=yes",
        "Encrypt=yes" if cfg.get("encrypt", True) else "Encrypt=no",
    ]
    conn = pyodbc.connect(";".join(parts), timeout=int(cfg.get("connect_timeout") or 10))
    conn.autocommit = False
    return conn


def connect(name: str):
    cfg = get_conn_cfg(name)
    driver = cfg["driver"].lower()
    if driver in ("postgres", "postgresql", "pg"):
        return _pg_connect(cfg), "pg", cfg
    if driver in ("mssql", "sqlserver", "sql-server"):
        return _mssql_connect(cfg), "mssql", cfg
    raise ValueError(f"Неизвестный драйвер: {cfg['driver']}")


def quote_ident(name: str, dialect: str) -> str:
    parts = [p for p in name.replace("[", "").replace("]", "").split(".") if p]
    if dialect == "mssql":
        return ".".join(f"[{p.replace(']', ']]')}]" for p in parts)
    return ".".join(f'"{p.replace(chr(34), chr(34) * 2)}"' for p in parts)


def split_table(name: str, dialect: str) -> tuple[str, str]:
    default_schema = "dbo" if dialect == "mssql" else "public"
    if "." in name:
        schema, table = name.split(".", 1)
        return schema, table
    return default_schema, name


def _check_sql_is_readonly(sql: str) -> str:
    clean = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    clean = re.sub(r"--[^\n]*", " ", clean).strip()
    if not clean:
        raise ValueError("Пустой запрос.")
    statements = [s for s in clean.split(";") if s.strip()]
    if len(statements) > 1:
        raise ValueError("Разрешён только один запрос (несколько инструкций через «;»).")
    head = statements[0].lstrip().lower()
    if not re.match(r"^(select|with|values|table|explain)\b", head):
        raise ValueError("Разрешены только SELECT / WITH / VALUES / EXPLAIN.")
    forbidden = r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|do|vacuum|reindex|refresh)\b"
    if re.search(forbidden, head) and not head.startswith("explain"):
        raise ValueError("В запросе найдена инструкция изменения данных.")
    return statements[0]


def run_query(conn, dialect: str, sql: str, limit: int = DEFAULT_ROW_LIMIT,
              timeout_ms: int = DEFAULT_TIMEOUT_MS, readonly: bool = True):
    """Выполняет запрос и возвращает (columns, rows, truncated)."""
    if readonly:
        sql = _check_sql_is_readonly(sql)
    cur = conn.cursor()
    try:
        if dialect != "pg":
            cur.execute(f"SET LOCK_TIMEOUT {int(timeout_ms)}")
        cur.execute(sql)
        if readonly:
            rows = cur.fetchmany(int(limit) + 1)
        else:
            rows = cur.fetchall()
        truncated = readonly and len(rows) > int(limit)
        if truncated:
            rows = rows[: int(limit)]
        columns = [d[0] for d in (cur.description or [])]
        return columns, rows, truncated
    finally:
        cur.close()


def _fmt_seconds(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    if value < 60:
        return f"{value:.0f} с"
    if value < 3600:
        return f"{value / 60:.1f} мин"
    if value < 86400:
        return f"{value / 3600:.1f} ч"
    return f"{value / 86400:.1f} сут"


# ------------------------------- инструменты --------------------------------
def tool_list_connections(args: dict) -> str:
    cfg = load_config()["connections"]
    if not cfg:
        return (
            f"Подключения не настроены.\n\n"
            f"1. Скопируйте `dbadmin.example.json` в `{CONFIG_PATH}`.\n"
            f"2. Впишите параметры базы (host, port, dbname, user, password).\n"
            f"3. Оставьте `readonly: true`, пока не понадобится удаление.\n\n"
            f"Другой путь к конфигу задаётся переменной `DBADMIN_CONFIG`."
        )
    lines = [f"Конфигурация: `{CONFIG_PATH}`", ""]
    rows = []
    for name, item in sorted(cfg.items()):
        rows.append([
            name, item.get("title", name), item.get("driver", "postgres"),
            f"{item.get('host')}:{item.get('port', 5432)}/{item.get('dbname')}",
            "запись" if (not item.get("readonly", True) and item.get("allow_write")) else "чтение",
        ])
    lines.append(render_table(["id", "Название", "Драйвер", "База", "Режим"], rows))
    return "\n".join(lines)


def tool_db_overview(args: dict) -> str:
    name = args.get("connection") or "analytics"
    conn, dialect, cfg = connect(name)
    try:
        if dialect == "pg":
            cols, rows, _ = run_query(conn, dialect, """
                select current_database() as db, version() as version,
                       pg_size_pretty(pg_database_size(current_database())) as size,
                       (select count(*) from information_schema.tables
                         where table_type='BASE TABLE'
                           and table_schema not in ('pg_catalog','information_schema')) as tables,
                       current_setting('shared_buffers') as shared_buffers,
                       current_setting('work_mem') as work_mem,
                       current_setting('max_connections') as max_connections,
                       (select count(*) from pg_stat_activity) as connections,
                       date_trunc('second', now() - pg_postmaster_start_time()) as uptime,
                       (select count(*) from pg_extension where extname='pg_stat_statements') > 0 as has_pgss
            """, limit=5)
        else:
            cols, rows, _ = run_query(conn, dialect, """
                select db_name() as db, @@version as version,
                       cast(sum(size)/128.0 as decimal(12,1)) as size_mb,
                       (select count(*) from sys.tables) as tables,
                       (select count(*) from sys.dm_exec_sessions
                         where is_user_process = 1) as connections,
                       (select sqlserver_start_time from sys.dm_os_sys_info) as started
                from sys.database_files
            """, limit=5)
        data = [dict(zip(cols, r)) for r in rows][0]
        out = [f"Подключение: **{name}** ({cfg.get('title', name)})", ""]
        out.append("\n".join(f"- {k}: {_cell(v)}" for k, v in data.items()))
        if dialect == "pg" and data.get("has_pgss") is False:
            out.append(
                "\n⚠️ `pg_stat_statements` не установлен — статистики запросов нет. "
                "Нужны `shared_preload_libraries='pg_stat_statements'` и рестарт кластера."
            )
        return "\n".join(out)
    finally:
        conn.close()


def tool_top_tables(args: dict) -> str:
    name = args.get("connection") or "analytics"
    limit = max(1, min(int(args.get("limit") or 20), 100))
    schema = (args.get("schema") or "").strip()
    exact = args.get("exact")
    refresh = bool(args.get("refresh"))
    conn, dialect, _ = connect(name)
    try:
        if dialect != "pg":
            sql = f"""
                select s.name as schema_name, t.name as table_name,
                       cast(sum(p.used_page_count) * 8.0 / 1024 as decimal(12,1)) as total_mb,
                       cast(sum(p.row_count) as bigint) as rows_est,
                       (select count(*) from sys.indexes i
                         where i.object_id = t.object_id and i.index_id > 0) as indexes
                from sys.tables t
                join sys.schemas s on s.schema_id = t.schema_id
                join sys.dm_db_partition_stats p on p.object_id = t.object_id
                     and p.index_id in (0, 1)
                group by s.name, t.name, t.object_id
                order by total_mb desc
                offset 0 rows fetch next {limit} rows only
            """
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()
            return f"Подключение: **{name}**\n\n" + render_table(
                ["схема", "таблица", "МБ", "строк", "инд."], [list(r) for r in rows])

        where = "and n.nspname = %s" if schema else ""
        params: tuple = (schema,) if schema else ()

        cur = conn.cursor()
        cur.execute(f"""
            select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where c.relkind = 'r' and n.nspname not in ('pg_catalog', 'information_schema')
              {where}
        """, params)
        table_count = cur.fetchone()[0]

        use_exact = exact if exact is not None else table_count <= 3000
        if use_exact and table_count > 5000:
            cur.close()
            return (f"Подключение: **{name}**\n\n"
                    f"В этой базе {table_count} таблиц: точный обход файлов займёт "
                    "около двух минут и упрётся в таймаут запроса.\n\n"
                    "Варианты:\n"
                    "- вызвать без `exact` — быстрая оценка по `pg_class.relpages` (≈, 0,5 с);\n"
                    "- сузить `schema` и повторить с `exact=true`;\n"
                    "- разобрать конкретную таблицу через `table_profile`.")
        if not refresh:
            cached = _cache_get(name, "top_tables_exact" if use_exact else "top_tables_fast")
            if cached and cached["rows"]:
                return (f"Подключение: **{name}** · из кэша "
                        f"({int((time.time() - cached['ts']) / 60)} мин назад)\n\n"
                        + render_table(cached["headers"], cached["rows"]))

        if use_exact:
            cur.execute(f"""
                select n.nspname as schema, c.relname as table,
                       pg_size_pretty(pg_total_relation_size(c.oid)) as total,
                       pg_size_pretty(pg_relation_size(c.oid)) as heap,
                       pg_size_pretty(pg_total_relation_size(c.oid)
                                      - pg_relation_size(c.oid)) as idx_toast,
                       (select count(*) from pg_index i where i.indrelid = c.oid) as indexes,
                       case when c.reltuples < 0 then null else c.reltuples::bigint end as rows_est,
                       coalesce(s.n_live_tup, 0) as live_tup,
                       s.seq_scan, s.idx_scan,
                       case when s.last_analyze is null and s.last_autoanalyze is null
                            then 'нет' else 'есть' end as stats
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                left join pg_stat_user_tables s on s.relid = c.oid
                where c.relkind = 'r'
                  and n.nspname not in ('pg_catalog', 'information_schema')
                  {where}
                order by pg_total_relation_size(c.oid) desc
                limit {limit}
            """, params)
            headers = ["схема", "таблица", "всего", "данные", "индексы+toast",
                       "инд.", "строк", "живых", "seq_scan", "idx_scan", "стат."]
            note = ""
        else:
            # Быстрый режим: размер по pg_class.relpages без обхода файлов.
            cur.execute(f"""
                select n.nspname as schema, c.relname as table,
                       pg_size_pretty((c.relpages + coalesce(t.relpages, 0)
                                       + coalesce(ix.pages, 0))::bigint * 8192) as total,
                       pg_size_pretty(c.relpages::bigint * 8192) as heap,
                       pg_size_pretty((coalesce(t.relpages, 0)
                                       + coalesce(ix.pages, 0))::bigint * 8192) as idx_toast,
                       coalesce(ix.cnt, 0) as indexes,
                       case when c.reltuples < 0 then null else c.reltuples::bigint end as rows_est,
                       coalesce(s.n_live_tup, 0) as live_tup,
                       s.seq_scan, s.idx_scan,
                       case when s.last_analyze is null and s.last_autoanalyze is null
                            then 'нет' else 'есть' end as stats
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                left join pg_class t on t.oid = c.reltoastrelid
                left join (
                    select i.indrelid, sum(ic.relpages) as pages, count(*) as cnt
                    from pg_index i join pg_class ic on ic.oid = i.indexrelid
                    group by i.indrelid
                ) ix on ix.indrelid = c.oid
                left join pg_stat_user_tables s on s.relid = c.oid
                where c.relkind = 'r'
                  and n.nspname not in ('pg_catalog', 'information_schema')
                  {where}
                order by (c.relpages + coalesce(t.relpages, 0)
                          + coalesce(ix.pages, 0)) desc
                limit {limit}
            """, params)
            headers = ["схема", "таблица", "≈всего", "≈данные", "≈индексы",
                       "инд.", "строк", "живых", "seq_scan", "idx_scan", "стат."]
            note = (f"\n\n_Быстрый режим: {table_count} таблиц, размер оценён по "
                    "`pg_class.relpages` без обхода файлов. Для точных цифр: "
                    "`exact=true` (на больших базах может занять 1–2 минуты)._")

        rows = [list(r) for r in cur.fetchall()]
        cur.close()
        name_map = onec_map(name)
        if name_map:
            headers = ["объект 1С"] + headers
            rows = [[name_map.get(r[1], "—")] + r for r in rows]
        _cache_put(name, "top_tables_exact" if use_exact else "top_tables_fast",
                   headers, rows)
        return f"Подключение: **{name}**\n\n" + render_table(headers, rows) + note
    except Exception as exc:  # noqa: BLE001
        if "statement timeout" in str(exc) or "QueryCanceled" in str(exc):
            return (f"Подключение: **{name}**\n\n"
                    "Точный обход не уложился в 30 с. Повторите без `exact` "
                    "(быстрая оценка по `pg_class.relpages`) или сузьте `schema`.")
        raise
    finally:
        conn.close()


def tool_top_indexes(args: dict) -> str:
    name = args.get("connection") or "analytics"
    limit = max(1, min(int(args.get("limit") or 15), 50))
    conn, dialect, _ = connect(name)
    try:
        if dialect == "pg":
            sql = f"""
                select n.nspname as schema, c.relname as table_name,
                       i.indexrelid::regclass::text as index_name,
                       pg_size_pretty(pg_relation_size(i.indexrelid)) as size,
                       coalesce(s.idx_scan, 0) as scans,
                       pg_get_indexdef(i.indexrelid) as definition
                from pg_index i
                join pg_class c on c.oid = i.indrelid
                join pg_namespace n on n.oid = c.relnamespace
                left join pg_stat_user_indexes s on s.indexrelid = i.indexrelid
                where n.nspname not in ('pg_catalog', 'information_schema')
                order by pg_relation_size(i.indexrelid) desc
                limit {limit}
            """
            headers = ["схема", "таблица", "индекс", "размер", "сканов", "определение"]
        else:
            sql = f"""
                select s.name as schema_name, t.name as table_name, i.name as index_name,
                       cast(sum(p.used_page_count) * 8.0 / 1024 as decimal(12,1)) as size_mb
                from sys.indexes i
                join sys.tables t on t.object_id = i.object_id
                join sys.schemas s on s.schema_id = t.schema_id
                join sys.dm_db_partition_stats p on p.object_id = i.object_id
                     and p.index_id = i.index_id
                where i.type > 1
                group by s.name, t.name, i.name, i.index_id
                order by size_mb desc
                offset 0 rows fetch next {limit} rows only
            """
            headers = ["схема", "таблица", "индекс", "МБ"]
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
        return f"Подключение: **{name}**\n\n" + render_table(headers, [list(r) for r in rows])
    finally:
        conn.close()


def tool_db_health(args: dict) -> str:
    name = args.get("connection") or "analytics"
    conn, dialect, _ = connect(name)
    try:
        if dialect != "pg":
            return tool_db_health_mssql(conn, name)
        out = [f"Подключение: **{name}**", ""]
        cur = conn.cursor()

        cur.execute("""
            select round(100 * sum(blks_hit) / nullif(sum(blks_hit + blks_read), 0), 2) as cache_hit,
                   round(100 * sum(deadlocks) / nullif(sum(xact_commit + xact_rollback), 0), 3) as deadlock_pct,
                   sum(temp_bytes) as temp_bytes,
                   sum(xact_rollback) as rollbacks
            from pg_stat_database where datname = current_database()
        """)
        health = cur.fetchone()

        cur.execute("select count(*), current_setting('max_connections')::int from pg_stat_activity")
        used, maxc = cur.fetchone()

        cur.execute("""
            select pid, round(extract(epoch from now() - xact_start))::bigint as sec,
                   state, left(regexp_replace(query, '\\s+', ' ', 'g'), 160) as query
            from pg_stat_activity
            where xact_start is not null and now() - xact_start > interval '30 seconds'
              and pid <> pg_backend_pid()
            order by xact_start limit 5
        """)
        long_tx = cur.fetchall()

        cur.execute("""
            select c.relname, s.n_dead_tup, s.n_live_tup,
                   to_char(s.last_autovacuum, 'MM-DD HH24:MI') as last_av,
                   to_char(s.last_autoanalyze, 'MM-DD HH24:MI') as last_an
            from pg_stat_user_tables s
            join pg_class c on c.oid = s.relid
            where s.n_live_tup > 0
            order by s.n_dead_tup desc limit 5
        """)
        dead = cur.fetchall()
        cur.close()

        out.append(f"- попадание в кэш: **{_cell(health[0])}%**")
        out.append(f"- соединения: **{used}** / {maxc}")
        out.append(f"- блокировки насмерть: {_cell(health[1])}% транзакций; откатов: {_cell(health[3])}")
        out.append(f"- временные файлы: {_fmt_bytes(health[2])}")
        out.append("")
        out.append("**Долгие транзакции (>30 с):**")
        out.append(render_table(["pid", "сек", "состояние", "запрос"],
                                [list(r) for r in long_tx]) if long_tx else "_нет_")
        out.append("")
        out.append("**Топ по мёртвым строкам (vacuum):**")
        out.append(render_table(["таблица", "мёртвых", "живых", "autovacuum", "autoanalyze"],
                                [list(r) for r in dead]))
        return "\n".join(out)
    finally:
        conn.close()


def tool_db_health_mssql(conn, name: str) -> str:
    cur = conn.cursor()
    out = [f"Подключение: **{name}** (MS SQL)", ""]
    cur.execute("""
        select cast(sum(size) * 8.0 / 1024 as decimal(12,1)) as db_mb
        from sys.database_files
    """)
    out.append(f"- размер базы: {_cell(cur.fetchone()[0])} МБ")
    cur.execute("""
        select top 5 wait_type, wait_time_ms/1000 as sec
        from sys.dm_os_wait_stats
        where wait_type not in ('CLR_SEMAPHORE','LAZYWRITER_SLEEP','SLEEP_TASK',
              'SLEEP_SYSTEMTASK','SQLTRACE_BUFFER_FLUSH','WAITFOR','BROKER_TASK_STOP',
              'HADR_FILESTREAM_IOMGR_IOCOMPLETION','CHECKPOINT_QUEUE','XE_TIMER_EVENT')
        order by wait_time_ms desc
    """)
    out.append("")
    out.append("**Топ ожиданий:**")
    out.append(render_table(["wait_type", "сек"], [list(r) for r in cur.fetchall()]))
    cur.execute("""
        select r.session_id, r.blocking_session_id,
               datediff(second, r.start_time, getdate()) as sec,
               left(regexp_replace(st.text, '\\s+', ' '), 140) as query
        from sys.dm_exec_requests r
        cross apply sys.dm_exec_sql_text(r.sql_handle) st
        where r.session_id <> @@spid and r.blocking_session_id <> 0
        order by sec desc
    """)
    blocked = cur.fetchall()
    cur.close()
    out.append("")
    out.append("**Заблокированные запросы:**")
    out.append(render_table(["sid", "блокирует", "сек", "запрос"],
                            [list(r) for r in blocked]) if blocked else "_нет_")
    return "\n".join(out)


def _fmt_bytes(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ПБ"


def tool_locks(args: dict) -> str:
    name = args.get("connection") or "analytics"
    conn, dialect, _ = connect(name)
    try:
        if dialect == "pg":
            cur = conn.cursor()
            cur.execute("""
                select w.pid as waiting_pid, b.pid as blocking_pid,
                       w.wait_event_type, w.wait_event,
                       round(extract(epoch from now() - w.query_start))::bigint as wait_sec,
                       left(regexp_replace(w.query, '\\s+', ' ', 'g'), 200) as waiting_query,
                       left(regexp_replace(b.query, '\\s+', ' ', 'g'), 120) as blocking_query
                from pg_stat_activity w
                join pg_stat_activity b on b.pid = any(pg_blocking_pids(w.pid))
                where cardinality(pg_blocking_pids(w.pid)) > 0
                order by wait_sec desc
            """)
            rows = cur.fetchall()
            cur.close()
            if not rows:
                return f"Сейчас блокировок нет (подключение **{name}**)."
            return f"Подключение: **{name}**\n\n" + render_table(
                ["ждал pid", "блокировал pid", "тип", "событие", "сек", "запрос ждущего", "запрос источника"],
                [list(r) for r in rows], max_width=70)
        cur = conn.cursor()
        cur.execute("""
            select session_id, blocking_session_id,
                   datediff(second, start_time, getdate()) as sec,
                   left(regexp_replace(st.text, '\\s+', ' '), 200) as query
            from sys.dm_exec_requests r
            cross apply sys.dm_exec_sql_text(r.sql_handle) st
            where session_id <> @@spid and blocking_session_id <> 0
        """)
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return f"Сейчас блокировок нет (подключение **{name}**)."
        return f"Подключение: **{name}**\n\n" + render_table(
            ["sid", "блокирует", "сек", "запрос"], [list(r) for r in rows], max_width=70)
    finally:
        conn.close()


def tool_slow_queries(args: dict) -> str:
    name = args.get("connection") or "analytics"
    limit = max(1, min(int(args.get("limit") or 10), 50))
    conn, dialect, _ = connect(name)
    try:
        cur = conn.cursor()
        if dialect == "pg":
            cur.execute("select count(*) from pg_extension where extname='pg_stat_statements'")
            if cur.fetchone()[0] == 0:
                cur.close()
                return (
                    f"Подключение: **{name}**\n\n"
                    "`pg_stat_statements` не установлен — истории запросов нет.\n\n"
                    "Что делать:\n"
                    "1. `shared_preload_libraries = 'pg_stat_statements'` в postgresql.conf;\n"
                    "2. рестарт кластера;\n"
                    "3. `CREATE EXTENSION pg_stat_statements;` в базе.\n\n"
                    "Пока доступны только текущие запросы — см. инструмент `locks` "
                    "и `db_health`."
                )
            cur.execute("""
                select left(regexp_replace(query, '\\s+', ' ', 'g'), 160) as query,
                       calls,
                       round(total_exec_time::numeric, 1) as total_ms,
                       round(mean_exec_time::numeric, 1) as mean_ms,
                       rows
                from pg_stat_statements
                where query not ilike '%%pg_stat_statements%%'
                order by total_exec_time desc limit %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            return f"Подключение: **{name}** · топ по суммарному времени\n\n" + render_table(
                ["запрос", "вызовов", "всего, мс", "средне, мс", "строк"],
                [list(r) for r in rows], max_width=70)

        cur.execute(f"""
            select top {limit}
                   left(regexp_replace(st.text, '\\s+', ' '), 160) as query,
                   qs.execution_count,
                   cast(qs.total_elapsed_time / 1000.0 as decimal(12,1)) as total_ms,
                   cast(qs.total_elapsed_time / nullif(qs.execution_count, 0) / 1000.0 as decimal(12,1)) as mean_ms,
                   qs.total_rows
            from sys.dm_exec_query_stats qs
            cross apply sys.dm_exec_sql_text(qs.sql_handle) st
            order by qs.total_elapsed_time desc
        """)
        rows = cur.fetchall()
        cur.close()
        return f"Подключение: **{name}** · топ по суммарному времени\n\n" + render_table(
            ["запрос", "вызовов", "всего, мс", "средне, мс", "строк"],
            [list(r) for r in rows], max_width=70)
    finally:
        conn.close()


def tool_posting_bottlenecks(args: dict) -> str:
    """Сводка «почему тормозит проведение»: блокировки, транзакции, тяжёлые таблицы."""
    name = args.get("connection") or "onec_task"
    conn, dialect, _ = connect(name)
    try:
        if dialect != "pg":
            return ("Полная диагностика проведения реализована для PostgreSQL. "
                    "Для MS SQL используйте `locks`, `slow_queries`, `db_health`.")
        cur = conn.cursor()
        report: list[str] = [f"## Диагностика проведения · **{name}**", ""]

        cur.execute("""
            select count(*) from pg_stat_activity w
            where cardinality(pg_blocking_pids(w.pid)) > 0
        """)
        blocked = cur.fetchone()[0]

        cur.execute("""
            select round(extract(epoch from now() - xact_start))::bigint as sec, pid, state,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 140) as query
            from pg_stat_activity
            where xact_start is not null and now() - xact_start > interval '30 seconds'
              and pid <> pg_backend_pid()
            order by xact_start limit 5
        """)
        long_tx = cur.fetchall()

        cur.execute("""
            select c.relname,
                   pg_size_pretty(pg_total_relation_size(c.oid)) as size,
                   (select count(*) from pg_index i where i.indrelid = c.oid) as idx,
                   s.last_analyze is null and s.last_autoanalyze is null as no_stats,
                   coalesce(s.seq_scan, 0) as seq_scan
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'public'
            left join pg_stat_user_tables s on s.relid = c.oid
            where c.relkind = 'r' and pg_total_relation_size(c.oid) > 500 * 1024 * 1024
            order by pg_total_relation_size(c.oid) desc limit 10
        """)
        heavy = cur.fetchall()

        cur.execute("select current_setting('max_connections')::int, count(*) from pg_stat_activity")
        maxc, used = cur.fetchone()
        cur.execute("select count(*) from pg_extension where extname='pg_stat_statements'")
        has_pgss = cur.fetchone()[0] > 0
        cur.execute("show deadlock_timeout")
        deadlock_timeout = cur.fetchone()[0]
        cur.close()

        report.append(f"**Блокировки сейчас:** {'⚠️ ' + str(blocked) + ' ждут' if blocked else 'нет'}")
        report.append(f"**Соединения:** {used} / {maxc}")
        report.append(f"**Статистика запросов:** {'есть' if has_pgss else '❌ pg_stat_statements выключен'}")
        report.append(f"**deadlock_timeout:** {deadlock_timeout}")
        report.append("")
        report.append("**Долгие транзакции (>30 с):**")
        report.append(render_table(["сек", "pid", "состояние", "запрос"],
                                   [list(r) for r in long_tx], max_width=60) if long_tx else "_нет_")
        report.append("")
        report.append("**Крупные таблицы (>500 МБ) и их обслуживание:**")
        report.append(render_table(["таблица", "размер", "индексов", "нет статистики", "seq_scan"],
                                   [list(r) for r in heavy], max_width=45))
        report.append("")

        advice: list[str] = []
        if blocked:
            advice.append("Есть ожидающие блокировок — смотрите `locks`, чтобы найти держателя.")
        if long_tx:
            advice.append("Долгая транзакция держит снимок и мешает vacuum; проверьте, не завис ли сеанс 1С.")
        no_stats = [r[0] for r in heavy if r[3]]
        if no_stats:
            advice.append("Нет статистики у " + ", ".join(f"`{t}`" for t in no_stats[:5])
                          + " — планировщик строит планы «на глаз»; нужен ANALYZE.")
        no_idx = [r[0] for r in heavy if r[2] == 0]
        if no_idx:
            advice.append("Без индексов: " + ", ".join(f"`{t}`" for t in no_idx[:5])
                          + " — любой доступ читает всю таблицу.")
        if not has_pgss:
            advice.append("Включите `pg_stat_statements`, иначе нельзя найти «главный» тяжёлый запрос.")
        if advice:
            report.append("**Что делать:**")
            report.extend(f"- {a}" for a in advice)
        return "\n".join(report)
    finally:
        conn.close()


def tool_table_profile(args: dict) -> str:
    table = (args.get("table") or "").strip()
    if not table:
        return "Укажите table (например, public._inforg1444)."
    name = args.get("connection") or "onec_task"
    conn, dialect, _ = connect(name)
    try:
        cur = conn.cursor()
        if dialect != "pg":
            return ("Профиль таблицы реализован для PostgreSQL. "
                    "Для MS SQL используйте `run_select` по системным представлениям.")
        schema, tbl = split_table(table, "pg")
        qualified = quote_ident(f"{schema}.{tbl}", "pg")

        cur.execute("""
            select c.oid, pg_size_pretty(pg_total_relation_size(c.oid)),
                   pg_size_pretty(pg_relation_size(c.oid)),
                   pg_size_pretty(pg_total_relation_size(c.oid) - pg_relation_size(c.oid)),
                   c.reltuples::bigint, c.relpages,
                   obj_description(c.oid, 'pg_class')
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = %s and c.relname = %s
        """, (schema, tbl))
        row = cur.fetchone()
        if not row:
            cur.close()
            return f"Таблица `{schema}.{tbl}` не найдена в базе **{name}**."
        oid = row[0]

        cur.execute("""
            select coalesce(s.n_live_tup, 0), coalesce(s.n_dead_tup, 0), s.seq_scan, s.idx_scan,
                   to_char(s.last_vacuum, 'YYYY-MM-DD HH24:MI'), to_char(s.last_autovacuum, 'YYYY-MM-DD HH24:MI'),
                   to_char(s.last_analyze, 'YYYY-MM-DD HH24:MI'), to_char(s.last_autoanalyze, 'YYYY-MM-DD HH24:MI')
            from pg_stat_user_tables s where s.relid = %s
        """, (oid,))
        stats = cur.fetchone() or (0, 0, None, None, None, None, None, None)

        cur.execute("""
            select i.indexrelid::regclass::text, pg_size_pretty(pg_relation_size(i.indexrelid)),
                   coalesce(s.idx_scan, 0), pg_get_indexdef(i.indexrelid)
            from pg_index i left join pg_stat_user_indexes s on s.indexrelid = i.indexrelid
            where i.indrelid = %s order by pg_relation_size(i.indexrelid) desc
        """, (oid,))
        indexes = cur.fetchall()

        cur.execute("""
            select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
            from pg_attribute a
            where a.attrelid = %s and a.attnum > 0 and not a.attisdropped
            order by a.attnum limit 40
        """, (oid,))
        columns = cur.fetchall()

        cur.execute("""
            select conrelid::regclass::text, conname, confdeltype
            from pg_constraint
            where confrelid = %s and contype = 'f' limit 10
        """, (oid,))
        children = cur.fetchall()
        cur.close()

        out = [f"## `{schema}.{tbl}` · база **{name}**", "",
               f"- объект 1С: {onec_map(name).get(tbl, '—')}",
               f"- размер: {row[1]} (данные {row[2]}, индексы/toast {row[3]})",
               f"- строк (оценка): {_cell(row[4])}, страниц: {_cell(row[5])}",
               f"- жилых/мёртвых: {stats[0]} / {stats[1]}",
               f"- seq_scan / idx_scan: {_cell(stats[2])} / {_cell(stats[3])}",
               f"- vacuum: {_cell(stats[4])} (auto {_cell(stats[5])})",
               f"- analyze: {_cell(stats[6])} (auto {_cell(stats[7])})", ""]
        out.append("**Индексы** — нет" if not indexes else "**Индексы:**")
        if indexes:
            out.append(render_table(["индекс", "размер", "сканов", "определение"],
                                    [list(r) for r in indexes], max_width=70))
        out.append("")
        out.append("**Колонки (первые 40):**")
        out.append(render_table(["колонка", "тип", "not null"],
                                [[c[0], c[1], c[2]] for c in columns]))
        if children:
            out.append("")
            out.append("**На таблицу ссылаются (FK):**")
            out.append(render_table(["таблица", "ограничение", "on delete"],
                                    [[c[0], c[1], {"a": "no action", "r": "restrict",
                                                   "c": "cascade", "n": "set null",
                                                   "d": "set default"}.get(c[2], c[2])]
                                     for c in children]))
        return "\n".join(out)
    finally:
        conn.close()


def tool_run_select(args: dict) -> str:
    sql = (args.get("sql") or "").strip()
    if not sql:
        return "Укажите sql (SELECT-запрос)."
    name = args.get("connection") or "analytics"
    limit = max(1, min(int(args.get("limit") or 100), DEFAULT_ROW_LIMIT))
    conn, dialect, _ = connect(name)
    try:
        started = time.time()
        cols, rows, truncated = run_query(conn, dialect, sql, limit=limit, readonly=True)
        elapsed = int((time.time() - started) * 1000)
        log(f"run_select {name} {elapsed} ms, строк={len(rows)}")
        note = f"\n\n_Показано {len(rows)} строк" + (" (обрезано по лимиту)" if truncated else "") + f", {elapsed} мс._"
        return f"Подключение: **{name}**\n\n" + render_table(cols, [list(r) for r in rows]) + note
    except Exception as exc:  # noqa: BLE001
        return f"Ошибка запроса: {exc}"
    finally:
        conn.close()


def tool_explain(args: dict) -> str:
    sql = (args.get("sql") or "").strip()
    if not sql:
        return "Укажите sql (SELECT-запрос для EXPLAIN)."
    name = args.get("connection") or "analytics"
    analyze = bool(args.get("analyze", True))
    conn, dialect, _ = connect(name)
    try:
        if dialect != "pg":
            return "EXPLAIN реализован для PostgreSQL. Для MS SQL используйте SET SHOWPLAN_ALL через run_select."
        clean = _check_sql_is_readonly(sql)
        prefix = "explain (analyze, buffers, verbose, format text) " if analyze else "explain "
        cur = conn.cursor()
        cur.execute(prefix + clean)
        plan = "\n".join(str(r[0]) for r in cur.fetchall())
        cur.close()
        return f"План запроса · **{name}** (analyze={analyze})\n\n```\n{plan}\n```"
    except Exception as exc:  # noqa: BLE001
        return f"Ошибка EXPLAIN: {exc}"
    finally:
        conn.close()


# --------------------------------- удаление ---------------------------------
def _clean_where(where: str) -> str:
    text = (where or "").strip()
    if not text:
        raise ValueError("Нужно условие отбора (where). Удаление всей таблицы запрещено.")
    if ";" in text or "--" in text or "/*" in text:
        raise ValueError("В условии недопустимы «;», «--», «/*».")
    if re.search(r"\b(delete|drop|truncate|update|insert)\b", text, re.I):
        raise ValueError("Условие не должно содержать инструкции изменения данных.")
    return text


def tool_delete_preview(args: dict) -> str:
    name = args.get("connection") or "onec_task"
    table = (args.get("table") or "").strip()
    if not table:
        return "Укажите table (например, public._inforg1444)."
    try:
        where = _clean_where(args.get("where") or "")
    except ValueError as exc:
        return f"Отклонено: {exc}"
    limit = max(1, min(int(args.get("limit") or 10_000), 1_000_000))
    conn, dialect, cfg = connect(name)
    try:
        schema, tbl = split_table(table, dialect)
        qualified = quote_ident(f"{schema}.{tbl}", dialect)
        cur = conn.cursor()

        cur.execute("set local statement_timeout = 120000" if dialect == "pg" else
                    "SET LOCK_TIMEOUT 120000")
        cur.execute(f"select count(*) from {qualified} where {where}")
        total = cur.fetchone()[0]

        if dialect == "pg":
            cur.execute(f"""
                select pg_size_pretty(coalesce(sum(pg_column_size(t.*)), 0)),
                       count(*)
                from (select * from {qualified} where {where} limit {min(limit, total)}) t
            """)
            size, sampled = cur.fetchone()
            cur.execute(f"select * from {qualified} where {where} limit 5")
            columns = [d[0] for d in cur.description]
            sample = [list(r) for r in cur.fetchall()]
            cur.execute("""
                select conrelid::regclass::text, conname
                from pg_constraint where confrelid = %s::regclass and contype = 'f' limit 10
            """, (f"{schema}.{tbl}",))
            children = [r[0] for r in cur.fetchall()]
        else:
            size, sampled = "—", min(limit, total)
            cur.execute(f"select top 5 * from {qualified} where {where}")
            columns = [d[0] for d in cur.description]
            sample = [list(r) for r in cur.fetchall()]
            children = []

        cur.close()
        conn.rollback()

        token = uuid.uuid4().hex[:12]
        PREVIEWS[token] = {
            "connection": name, "table": f"{schema}.{tbl}", "where": where,
            "limit": limit, "count": total, "created": time.time(),
            "writer": bool(cfg.get("allow_write")) and not cfg.get("readonly", True),
        }

        out = [f"## Предпросмотр удаления · **{name}**", "",
               f"- таблица: `{schema}.{tbl}`",
               f"- отбор: `{where}`",
               f"- под отбор попадает: **{total}** строк (лимит выполнения {limit})",
               f"- оценка объёма выборки: ~{size}",
               f"- token: **{token}** (действует {PREVIEW_TTL_SEC // 60} мин)",
               "",
               "**Пример строк:**",
               render_table(columns, sample, max_width=40) if sample else "_нет строк, удалять нечего_"]
        if children:
            out.append("")
            out.append("⚠️ На таблицу ссылаются по FK: " + ", ".join(f"`{c}`" for c in children)
                       + " — удаление может оставить битые ссылки.")
        if tbl.startswith("_"):
            out.append("")
            out.append("⚠️ Таблица 1С: удалять напрямую опасно — нужны удаление движений "
                       "и пересчёт итогов регистров, иначе база останется логически битой.")
        out.append("")
        out.append(f"Запись {'разрешена' if PREVIEWS[token]['writer'] else 'НЕ разрешена'} "
                   f"для этого подключения. "
                   + ("Для выполнения: `delete_apply(token=\"%s\", confirm=\"DELETE\")`." % token
                      if PREVIEWS[token]["writer"] else
                      "Можно получить скрипт: `delete_script(token=\"%s\")`." % token))
        return "\n".join(out)
    except Exception as exc:  # noqa: BLE001
        return f"Ошибка предпросмотра: {exc}"
    finally:
        conn.close()


def _preview_by_token(token: str) -> dict:
    item = PREVIEWS.get(token)
    if not item:
        raise ValueError("Неизвестный или устаревший token. Сделайте preview заново.")
    if time.time() - item["created"] > PREVIEW_TTL_SEC:
        PREVIEWS.pop(token, None)
        raise ValueError("Token истёк. Сделайте preview заново.")
    return item


def tool_delete_apply(args: dict) -> str:
    token = (args.get("token") or "").strip()
    if (args.get("confirm") or "").upper() != "DELETE":
        return "Нужно подтверждение: confirm=\"DELETE\"."
    try:
        item = _preview_by_token(token)
    except ValueError as exc:
        return str(exc)
    if not item["writer"]:
        return ("Подключение доступно только на чтение. "
                f"Сформируйте скрипт: delete_script(token=\"{token}\").")

    conn, dialect, _ = connect(item["connection"])
    qualified = quote_ident(item["table"], dialect)
    backup_name = f"backup.{item['table'].split('.')[-1]}_{datetime.now():%Y%m%d_%H%M%S}"
    backup_qualified = quote_ident(backup_name, dialect)
    try:
        cur = conn.cursor()
        if dialect == "pg":
            cur.execute("create schema if not exists backup")
            cur.execute(f"create table {backup_qualified} as "
                        f"select * from {qualified} where {item['where']}")
            cur.execute("select count(*) from " + backup_qualified)
            saved = cur.fetchone()[0]
            cur.execute(f"""
                delete from {qualified}
                where ctid in (select ctid from {qualified} where {item['where']} limit {item['limit']})
            """)
            deleted = cur.rowcount
        else:
            cur.execute(f"select top ({item['limit']}) * into {backup_qualified} "
                        f"from {qualified} where {item['where']}")
            saved = cur.rowcount
            cur.execute(f"delete top ({item['limit']}) from {qualified} where {item['where']}")
            deleted = cur.rowcount
        conn.commit()
        cur.close()
        PREVIEWS.pop(token, None)
        log(f"delete_apply {item['connection']} {item['table']} удалено={deleted} бэкап={backup_name}")
        return (f"Удалено **{deleted}** строк из `{item['table']}`.\n"
                f"Резервная копия: `{backup_name}` ({saved} строк).\n"
                f"Откат: `insert into {item['table']} select * from {backup_name};`")
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        return f"Удаление отменено (rollback): {exc}"
    finally:
        conn.close()


def tool_delete_script(args: dict) -> str:
    token = (args.get("token") or "").strip()
    try:
        item = _preview_by_token(token)
    except ValueError as exc:
        return str(exc)
    cfg = get_conn_cfg(item["connection"])
    dialect = cfg["driver"].lower()
    qualified = quote_ident(item["table"], dialect)
    backup = quote_ident(f"backup.{item['table'].split('.')[-1]}_{datetime.now():%Y%m%d_%H%M%S}", dialect)
    if dialect.startswith("postgres") or dialect == "pg":
        script = f"""-- Удаление по отбору, подключение {item['connection']}
-- Ожидается строк: {item['count']}, лимит: {item['limit']}
\\set ON_ERROR_STOP on
begin;
create schema if not exists backup;
create table {backup} as select * from {qualified} where {item['where']};
select count(*) as saved from {backup};
delete from {qualified}
 where ctid in (select ctid from {qualified} where {item['where']} limit {item['limit']});
commit;
-- Проверка: select count(*) from {qualified} where {item['where']};
-- Откат: insert into {qualified} select * from {backup};
"""
    else:
        script = f"""-- Удаление по отбору, подключение {item['connection']}
begin transaction;
select top ({item['limit']}) * into {backup} from {qualified} where {item['where']};
delete top ({item['limit']}) from {qualified} where {item['where']};
commit;
"""
    return f"Скрипт для ручного запуска (подключение **{item['connection']}**):\n\n```sql\n{script}```"


# ------------------------------ описание MCP --------------------------------
TOOLS = [
    {
        "name": "list_connections",
        "description": "Список настроенных подключений к базам (PostgreSQL / MS SQL), драйвер, база, режим доступа.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "db_overview",
        "description": "Обзор базы: версия, размер, число таблиц, shared_buffers, соединения, наличие pg_stat_statements.",
        "inputSchema": {
            "type": "object",
            "properties": {"connection": {"type": "string", "description": "id подключения"}},
        },
    },
    {
        "name": "top_tables",
        "description": "Самые большие таблицы: размер, данные, индексы, число индексов, строк, seq/index scans, есть ли статистика. На больших базах — быстрый режим с оценкой; exact=true для точного обхода.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection": {"type": "string"},
                "limit": {"type": "integer", "description": "сколько строк вернуть (по умолчанию 20)"},
                "schema": {"type": "string", "description": "ограничить схемой (PostgreSQL)"},
                "exact": {"type": "boolean", "description": "точный размер через pg_total_relation_size (медленно на 10k+ таблиц)"},
                "refresh": {"type": "boolean", "description": "игнорировать кэш 15 минут"},
            },
        },
    },
    {
        "name": "top_indexes",
        "description": "Самые большие индексы с числом сканов и определением (PostgreSQL).",
        "inputSchema": {
            "type": "object",
            "properties": {"connection": {"type": "string"},
                           "limit": {"type": "integer"}},
        },
    },
    {
        "name": "db_health",
        "description": "Здоровье базы: попадание в кэш, соединения, откаты, временные файлы, долгие транзакции, мёртвые строки.",
        "inputSchema": {
            "type": "object",
            "properties": {"connection": {"type": "string"}},
        },
    },
    {
        "name": "locks",
        "description": "Кто кого блокирует прямо сейчас: ожидающий и блокирующий pid, тип ожидания, длительность, запросы.",
        "inputSchema": {
            "type": "object",
            "properties": {"connection": {"type": "string"}},
        },
    },
    {
        "name": "slow_queries",
        "description": "Топ запросов по суммарному времени (pg_stat_statements / dm_exec_query_stats) или подсказка, как его включить.",
        "inputSchema": {
            "type": "object",
            "properties": {"connection": {"type": "string"}, "limit": {"type": "integer"}},
        },
    },
    {
        "name": "posting_bottlenecks",
        "description": "Диагностика «почему тормозит проведение»: блокировки, долгие транзакции, крупные таблицы без статистики и индексов, выводы.",
        "inputSchema": {
            "type": "object",
            "properties": {"connection": {"type": "string",
                                          "description": "подключение к базе 1С (по умолчанию onec_task)"}},
        },
    },
    {
        "name": "table_profile",
        "description": "Профиль таблицы: размер, оценки строк, индексы, колонки, vacuum/analyze, seq/index scans, внешние ключи.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection": {"type": "string"},
                "table": {"type": "string", "description": "schema.table или table"},
            },
            "required": ["table"],
        },
    },
    {
        "name": "run_select",
        "description": "Выполнить одиночный SELECT в транзакции только для чтения: лимит строк, statement_timeout. Запись невозможна.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection": {"type": "string"},
                "sql": {"type": "string"},
                "limit": {"type": "integer", "description": "максимум строк (по умолчанию 100, не более 500)"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "explain",
        "description": "План запроса PostgreSQL: EXPLAIN (ANALYZE, BUFFERS) для SELECT, в read-only транзакции.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection": {"type": "string"},
                "sql": {"type": "string"},
                "analyze": {"type": "boolean", "description": "выполнять запрос (по умолчанию да)"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "delete_preview",
        "description": "Предпросмотр удаления по отбору: сколько строк, объём, примеры, FK-ссылки, token. Ничего не удаляет.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connection": {"type": "string"},
                "table": {"type": "string"},
                "where": {"type": "string", "description": "SQL-условие без инструкций"},
                "limit": {"type": "integer", "description": "сколько строк удалять за раз (по умолчанию 10000)"},
            },
            "required": ["table", "where"],
        },
    },
    {
        "name": "delete_apply",
        "description": "Выполнить удаление по токену preview: сначала резервная копия в схему backup, потом DELETE, при ошибке rollback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "confirm": {"type": "string", "description": "строка DELETE"},
            },
            "required": ["token", "confirm"],
        },
    },
    {
        "name": "delete_script",
        "description": "Сгенерировать SQL-скрипт удаления для ручного запуска (с бэкапом и проверкой).",
        "inputSchema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    },
]

HANDLERS = {
    "list_connections": tool_list_connections,
    "db_overview": tool_db_overview,
    "top_tables": tool_top_tables,
    "top_indexes": tool_top_indexes,
    "db_health": tool_db_health,
    "locks": tool_locks,
    "slow_queries": tool_slow_queries,
    "posting_bottlenecks": tool_posting_bottlenecks,
    "table_profile": tool_table_profile,
    "run_select": tool_run_select,
    "explain": tool_explain,
    "delete_preview": tool_delete_preview,
    "delete_apply": tool_delete_apply,
    "delete_script": tool_delete_script,
}


# ------------------------------- транспорт --------------------------------
def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    msg_id = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dbadmin", "version": "1.0.0"},
            },
        })
        return
    if method == "notifications/initialized":
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"Неизвестный инструмент: {name}"}})
            return
        try:
            text = handler(args)
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"content": [{"type": "text", "text": text}]}})
        except Exception as exc:  # noqa: BLE001
            log(f"ошибка {name}: {exc}")
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"content": [{"type": "text",
                                          "text": f"Ошибка: {exc}"}],
                             "isError": True}})
        return

    if msg_id is not None:
        send({"jsonrpc": "2.0", "id": msg_id,
              "error": {"code": -32601, "message": f"Метод не поддержан: {method}"}})


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    log(f"сервер запущен, конфиг: {CONFIG_PATH}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log("не JSON, пропуск")
            continue
        handle(msg)


if __name__ == "__main__":
    main()
