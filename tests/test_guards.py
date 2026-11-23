from datetime import date, datetime
from decimal import Decimal

import pytest

from dbadmin_mcp import onec_map
from dbadmin_mcp.server import (
    _cell,
    _check_sql_is_readonly,
    _clean_where,
    quote_ident,
    render_table,
    split_table,
)


@pytest.mark.parametrize("sql", [
    "select 1",
    "  SELECT * from stg.incidents limit 5",
    "with x as (select 1) select * from x",
    "values (1), (2)",
    "table stg.incidents",
    "explain select 1",
])
def test_select_allowed(sql):
    assert _check_sql_is_readonly(sql).strip()


@pytest.mark.parametrize("sql", [
    "delete from stg.incidents",
    "update stg.incidents set kind = null",
    "select 1; delete from stg.incidents",
    "  /* comment */ drop table stg.incidents",
    "truncate stg.incidents",
    "   ",
])
def test_writes_rejected(sql):
    with pytest.raises(ValueError):
        _check_sql_is_readonly(sql)


@pytest.mark.parametrize("where", [
    "id > 5",
    "created < date '2026-01-01' and kind = 'old'",
    "period >= '2024-01-01'",
])
def test_where_allowed(where):
    assert _clean_where(where) == where


@pytest.mark.parametrize("where", [
    "",
    "   ",
    "1=1;",
    "id = 1 -- всё",
    "id = 1 /* да */",
    "1=1 or delete from t",
])
def test_where_rejected(where):
    with pytest.raises(ValueError):
        _clean_where(where)


def test_quote_ident():
    assert quote_ident("meta.usage_log", "pg") == '"meta"."usage_log"'
    assert quote_ident("public._inforg1444", "pg") == '"public"."_inforg1444"'
    assert quote_ident("dbo.events", "mssql") == "[dbo].[events]"
    assert quote_ident('weird"name', "pg") == '"weird""name"'


def test_split_table():
    assert split_table("public.t", "pg") == ("public", "t")
    assert split_table("t", "pg") == ("public", "t")
    assert split_table("t", "mssql") == ("dbo", "t")
    assert split_table("dbo.t", "mssql") == ("dbo", "t")


def test_cell_formatting():
    assert _cell(None) == "—"
    assert _cell(True) == "да"
    assert _cell(False) == "нет"
    assert _cell(date(2026, 1, 31)) == "2026-01-31"
    assert _cell(datetime(2026, 1, 31, 12, 30)) == "2026-01-31 12:30:00"
    assert _cell(Decimal("12.34567")) == "12.35"
    assert _cell(b"12345") == "<5 байт>"
    assert _cell("две\nстроки") == "две строки"


def test_render_table():
    text = render_table(["a", "b"], [[1, None], ["x", date(2026, 5, 1)]])
    assert "| a " in text
    assert "2026-05-01" in text
    assert render_table(["a"], []) == "_(пусто)_"


def test_translate_1c_types():
    assert onec_map.translate("InformationRegister.ЗамерыВремени") == "РегистрСведений.ЗамерыВремени"
    assert onec_map.translate("Document.ОтчетыОбОшибках") == "Документ.ОтчетыОбОшибках"
    assert (
        onec_map.translate("Catalog.ВерсииХранилища.TabularSection.Состав")
        == "Справочник.ВерсииХранилища.ТабличнаяЧасть.Состав"
    )
    assert onec_map.translate("Unknown.Объект") == "Unknown.Объект"


def test_resolve_tabular_sections():
    result = {
        "tables": {"_reference2220": "Справочник.ВерсииХранилища"},
        "vt": {2229: "Справочник.ВерсииХранилища.ТабличнаяЧасть.Состав"},
    }
    onec_map.resolve_vt(result, {"_reference2220", "_reference2220_vt2229"})
    assert result["tables"]["_reference2220_vt2229"] == (
        "Справочник.ВерсииХранилища.ТабличнаяЧасть.Состав "
        "(ТЧ Справочник.ВерсииХранилища)"
    )
