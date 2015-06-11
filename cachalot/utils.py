# coding: utf-8

from __future__ import unicode_literals
from hashlib import sha1
from time import time

import django
from django.db import connections
from django.db.models.sql import Query
from django.db.models.sql.compiler import (
    SQLInsertCompiler, SQLUpdateCompiler, SQLDeleteCompiler)
from django.db.models.sql.where import ExtraWhere, SubqueryConstraint
DJANGO_GTE_1_7 = django.VERSION[:2] >= (1, 7)
if DJANGO_GTE_1_7:
    from django.utils.module_loading import import_string
else:
    from django.utils.module_loading import import_by_path as import_string

from .settings import cachalot_settings
from .signals import post_invalidation


WRITE_COMPILERS = (SQLInsertCompiler, SQLUpdateCompiler, SQLDeleteCompiler)


class DontCacheQuery(Exception):
    """The given query should not be cached."""


class RandomQueryException(DontCacheQuery):
    """The query contains a random selector."""


class BlacklistedTable(DontCacheQuery):
    """The query is on a blacklisted table."""


def get_query_cache_key(compiler):
    """
    Generates a cache key from a SQLCompiler.

    This cache key is specific to the SQL query and its context
    (which database is used).  The same query in the same context
    (= the same database) must generate the same cache key.

    :arg compiler: A SQLCompiler that will generate the SQL query
    :type compiler: django.db.models.sql.compiler.SQLCompiler
    :return: A cache key
    :rtype: str
    """
    sql, params = compiler.as_sql()
    cache_key = '%s:%s:%s' % (compiler.using, sql, params)
    return sha1(cache_key.encode('utf-8')).hexdigest()


def get_table_cache_key(db_alias, table):
    """
    Generates a cache key from a SQL table.

    :arg db_alias: Alias of the used database
    :type db_alias: str or unicode
    :arg table: Name of the SQL table
    :type table: str or unicode
    :return: A cache key
    :rtype: str
    """
    cache_key = '%s:%s' % (db_alias, table)
    return sha1(cache_key.encode('utf-8')).hexdigest()


def _get_query_cache_key(compiler):
    return import_string(cachalot_settings.CACHALOT_QUERY_KEYGEN)(compiler)


def _get_table_cache_key(db_alias, table):
    return import_string(cachalot_settings.CACHALOT_TABLE_KEYGEN)(db_alias, table)


def _get_tables_from_sql(connection, lowercased_sql):
    return [t for t in connection.introspection.django_table_names()
            if t in lowercased_sql]


def _find_subqueries(children):
    for child in children:
        if isinstance(child, SubqueryConstraint):
            if isinstance(child.query_object, Query):
                yield child.query_object
            else:
                yield child.query_object.query
        else:
            rhs = None
            if DJANGO_GTE_1_7:
                if hasattr(child, 'rhs'):
                    rhs = child.rhs
            elif isinstance(child, tuple):
                rhs = child[-1]
            if isinstance(rhs, Query):
                yield rhs
            elif hasattr(rhs, 'query'):
                yield rhs.query
        if hasattr(child, 'children'):
            for grand_child in _find_subqueries(child.children):
                yield grand_child


def _get_tables(query, db_alias):
    if '?' in query.order_by and not cachalot_settings.CACHALOT_CACHE_RANDOM:
        raise RandomQueryException

    tables = set(query.table_map)
    tables.add(query.get_meta().db_table)
    subquery_constraints = _find_subqueries(query.where.children
                                            + query.having.children)
    for subquery in subquery_constraints:
        tables.update(_get_tables(subquery, db_alias))
    if query.extra_select or hasattr(query, 'subquery') \
            or any(isinstance(c, ExtraWhere) for c in query.where.children):
        sql = query.get_compiler(db_alias).as_sql()[0].lower()
        additional_tables = _get_tables_from_sql(connections[db_alias], sql)
        tables.update(additional_tables)

    blacklist = cachalot_settings.CACHALOT_BLACKLIST
    if blacklist:
        blacklisted_tables = blacklist.intersection(tables)
        if blacklisted_tables:
            raise BlacklistedTable()

    return tables

def _get_table_cache_keys(compiler):
    db_alias = compiler.using
    tables = _get_tables(compiler.query, db_alias)
    return [_get_table_cache_key(db_alias, t) for t in tables]


def _invalidate_table_cache_keys(cache, table_cache_keys):
    if hasattr(cache, 'to_be_invalidated'):
        cache.to_be_invalidated.update(table_cache_keys)
    now = time()
    d = {}
    for k in table_cache_keys:
        d[k] = now
    cache.set_many(d, None)


def _invalidate_tables(cache, write_compiler):
    db_alias = write_compiler.using

    table = write_compiler.query.get_meta().db_table
    table_cache_key = _get_table_cache_key(db_alias, table)
    _invalidate_table_cache_keys(cache, (table_cache_key,))

    post_invalidation.send(table, db_alias=db_alias)
