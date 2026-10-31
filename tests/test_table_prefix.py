"""A shared Supabase project: every table and index name can carry the project's prefix."""
import re
from pathlib import Path

import pytest

from app.config import load_settings
from app.store import INDEX_NAMES, TABLE_NAMES, normalize_prefix, prefix_sql

SCHEMA = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()


def test_normalize_prefix():
    assert normalize_prefix("assistant") == "assistant_"
    assert normalize_prefix("assistant_") == "assistant_"
    assert normalize_prefix(" My-App ") == "my_app_"
    assert normalize_prefix("") == "" and normalize_prefix(None) == ""


@pytest.mark.parametrize("bad", ["1abc", "a b", "abc;drop", "a" * 40, "ab__cd", "ÄÖ"])
def test_bad_prefix_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_prefix(bad)


def test_prefix_sql_rewrites_tables_and_indexes_only():
    sql = ("insert into approvals (action_id) values (%s); select * from audit_log where user_id=%s "
           "order by seq; create index if not exists audit_user_idx on audit_log (user_id)")
    out = prefix_sql(sql, "abc_")
    assert "into abc_approvals" in out and "from abc_audit_log" in out and "abc_audit_user_idx on abc_audit_log" in out
    assert "abc_abc_" not in out
    assert prefix_sql(sql, "") == sql
    # words that only contain a table name are left alone
    assert prefix_sql("select plans_total, n_actions from x", "abc_") == "select plans_total, n_actions from x"


def test_every_table_and_index_in_schema_is_covered():
    """If someone adds a table to schema.sql and forgets TABLE_NAMES, this fails."""
    tables = set(re.findall(r"create table if not exists (\w+)", SCHEMA))
    indexes = set(re.findall(r"create (?:unique )?index if not exists (\w+)", SCHEMA))
    assert tables == set(TABLE_NAMES)
    assert indexes == set(INDEX_NAMES)


def test_whole_schema_gets_prefixed():
    out = prefix_sql(SCHEMA, "abc_")
    for t in TABLE_NAMES:
        assert f"abc_{t}" in out
        assert not re.search(rf"(?<!abc_)\b{t}\b", out.replace("--", " ")), t


def test_config_validates_prefix():
    assert load_settings({"TABLE_PREFIX": "assistant"}).validate() == []
    assert any("TABLE_PREFIX" in e for e in load_settings({"TABLE_PREFIX": "9bad!"}).validate())
