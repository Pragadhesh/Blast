"""Shared SQL preprocessing used by diff_parser.py and impact_simulator.py."""

import re

_JINJA_REF = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_JINJA_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_JINJA_STATEMENT_LINE = re.compile(r"^[ \t]*\{[{%].*?[%}]\}[ \t]*$\n?", re.MULTILINE)
_JINJA_ANY = re.compile(r"\{\{.*?\}\}")


def render_jinja_refs(sql: str) -> str:
    """Make dbt model SQL parseable by sqlglot.

    dbt models use Jinja macros (``{{ ref('x') }}``, ``{{ source('s', 't') }}``) that
    sqlglot can't parse as SQL. Diffing and impact simulation only need the SELECT
    column list, not FROM-clause resolution, so it's enough to swap ref()/source()
    calls for their bare table name and drop any other Jinja (e.g. a leading
    ``{{ config(...) }}`` block) rather than fully rendering the template.
    """
    sql = _JINJA_REF.sub(lambda m: m.group(1), sql)
    sql = _JINJA_SOURCE.sub(lambda m: m.group(1), sql)
    sql = _JINJA_STATEMENT_LINE.sub("", sql)
    sql = _JINJA_ANY.sub("1", sql)
    return sql
