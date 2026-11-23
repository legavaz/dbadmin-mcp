"""Сопоставление таблиц PostgreSQL с объектами 1С.

Источники:
  1) таблица `params` в базе 1С (файл DBNames) — GUID объекта → номер таблицы;
  2) выгрузка конфигурации в XML (ConfigDumpInfo.xml) — GUID → имя объекта.

Результат: JSON `{ "tables": { "_inforg1444": "РегистрСведений.ЗамерыВремени" } }`.

Пример:
    python -m dbadmin_mcp.onec_map ^
        --connection onec_task ^
        --dump "C:\\src\\cf\\ConfigDumpInfo.xml" ^
        --out onec_tables_task.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zlib
from datetime import datetime

TYPE_RU = {
    "Catalog": "Справочник",
    "Document": "Документ",
    "DocumentJournal": "ЖурналДокументов",
    "InformationRegister": "РегистрСведений",
    "AccumulationRegister": "РегистрНакопления",
    "AccountingRegister": "РегистрБухгалтерии",
    "CalculationRegister": "РегистрРасчёта",
    "ChartOfAccounts": "ПланСчетов",
    "ChartOfCharacteristicTypes": "ПланВидовХарактеристик",
    "ChartOfCalculationTypes": "ПланВидовРасчёта",
    "ExchangePlan": "ПланОбмена",
    "BusinessProcess": "БизнесПроцесс",
    "Task": "Задача",
    "Enum": "Перечисление",
    "Constant": "Константа",
    "FilterCriterion": "КритерийОтбора",
    "Sequence": "Последовательность",
    "Report": "Отчет",
    "DataProcessor": "Обработка",
    "CommonModule": "ОбщийМодуль",
    "TabularSection": "ТабличнаяЧасть",
    "Attribute": "Реквизит",
    "Resource": "Ресурс",
    "Dimension": "Измерение",
}

# коды DBNames → префикс имени таблицы в PostgreSQL
TABLE_PREFIX = {
    "Reference": "reference",
    "ReferenceChngR": "referencechngr",
    "Document": "document",
    "InfoRg": "inforg",
    "InfoRgChngR": "inforgchngr",
    "AccumRg": "accumrg",
    "AccumRgT": "accumrgt",
    "AccumRgChngR": "accumrgchngr",
    "AccumRgOpt": "accumrgopt",
    "AccumRgTChngR": "accumrgtchngr",
    "Const": "const",
    "ConstChngR": "constchngr",
    "Enum": "enum",
    "Chrc": "chrc",
    "ChrcChngR": "chrcchngr",
    "Accnt": "acc",
    "AccntChngR": "accchngr",
    "AccntRg": "accrg",
    "CalcRg": "calcrg",
    "RefSInf": "refsinf",
    "EDBT": "edbt",
    "EDBTChngR": "edbtchngr",
}


def load_connection(name: str) -> dict:
    from .server import get_conn_cfg

    return get_conn_cfg(name)


def read_dbnames(conn_cfg: dict) -> list[tuple[str, str, int]]:
    import psycopg

    params = dict(
        host=conn_cfg["host"], port=int(conn_cfg.get("port") or 5432),
        user=conn_cfg["user"], password=conn_cfg.get("password", ""),
        dbname=conn_cfg["dbname"], connect_timeout=15,
    )
    with psycopg.connect(**params) as conn, conn.cursor() as cur:
        cur.execute("select binarydata from public.params where filename = 'DBNames'")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("В params нет записи DBNames — это не база 1С?")
    text = zlib.decompress(bytes(row[0]), -15).decode("utf-8-sig")
    return [
        (guid.lower(), code, int(num))
        for guid, code, num in re.findall(r'\{([0-9a-fA-F-]{36}),"([A-Za-z0-9]+)",(\d+)\}', text)
    ]


def read_dump(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return {
        guid.lower(): name
        for name, guid in re.findall(r'<Metadata name="([^"]+)" id="([0-9a-fA-F-]{36})"', text)
    }


def translate(full_name: str) -> str:
    parts = full_name.split(".")
    return ".".join(TYPE_RU.get(p, p) for p in parts)


def build(conn_name: str, dump_path: str) -> dict:
    conn_cfg = load_connection(conn_name)
    dbnames = read_dbnames(conn_cfg)
    by_guid = read_dump(dump_path)

    names: dict[str, str] = {}
    vt_parent: dict[int, str] = {}
    counters = {"root": 0, "vt": 0, "unknown": 0}

    for guid, code, num in dbnames:
        full = by_guid.get(guid)
        prefix = TABLE_PREFIX.get(code)
        if prefix and full:
            names[f"_{prefix}{num}"] = translate(full)
            counters["root"] += 1
        elif code == "VT" and full:
            counters["vt"] += 1
            vt_parent[num] = translate(full)

    return {
        "base": conn_cfg["dbname"],
        "connection": conn_name,
        "dump": os.path.abspath(dump_path),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "counts": counters,
        "tables": dict(sorted(names.items())),
        "vt": dict(sorted(vt_parent.items())),
    }


def resolve_vt(result: dict, existing_tables: set[str]) -> None:
    """Привязывает таблицы ТЧ вида `_reference2220_vt2229` к именам объектов."""
    vt = result.get("vt") or {}
    tables = result["tables"]
    for table in existing_tables:
        match = re.match(r"^(_[a-z]+)(\d+)_vt(\d+)$", table)
        if not match:
            continue
        parent = match.group(1) + match.group(2)
        parent_name = tables.get(parent, parent)
        child = vt.get(int(match.group(3)))
        if child:
            tables[table] = f"{child} (ТЧ {parent_name})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", required=True)
    parser.add_argument("--dump", required=True, help="путь к ConfigDumpInfo.xml")
    parser.add_argument("--out", required=True)
    parser.add_argument("--tables", help="список существующих таблиц (через запятую), чтобы привязать ТЧ")
    args = parser.parse_args()

    result = build(args.connection, args.dump)

    existing: set[str] = set()
    if args.tables:
        existing = {t.strip() for t in args.tables.split(",") if t.strip()}
    else:
        try:
            import psycopg

            cfg = load_connection(args.connection)
            with psycopg.connect(host=cfg["host"], port=int(cfg.get("port") or 5432),
                                 user=cfg["user"], password=cfg.get("password", ""),
                                 dbname=cfg["dbname"]) as conn, conn.cursor() as cur:
                cur.execute("""select c.relname from pg_class c
                               join pg_namespace n on n.oid = c.relnamespace
                               where c.relkind = 'r' and n.nspname = 'public'""")
                existing = {r[0] for r in cur.fetchall()}
        except Exception as exc:  # noqa: BLE001
            print(f"не удалось получить список таблиц: {exc}", file=sys.stderr)

    resolve_vt(result, existing)

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    matched = sum(1 for t in existing if t in result["tables"])
    print(f"объектов в DBNames: {len(result['tables'])}")
    print(f"таблиц в базе: {len(existing)}, из них распознано: {matched}")
    print(f"записано: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
