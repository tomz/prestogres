import plpy
import presto_client
from collections import namedtuple
import time

def run_presto_as_temp_table(server, user, catalog, schema, table_name, query):
    try:
        client = presto_client.Client(server=server, user=user, catalog=catalog, schema=schema)

        create_sql = 'create temp table ' + plpy.quote_ident(table_name) + ' (\n  '
        insert_sql = 'insert into ' + plpy.quote_ident(table_name) + ' (\n  '
        values_types = []

        q = client.query(query)
        try:
            columns = q.columns()
            column_num = len(columns)

            first = True
            for column in columns:
                if column.type == "varchar":
                    pg_column_type = "text"
                elif column.type == "bigint":
                    pg_column_type = "bigint"
                elif column.type == "boolean":
                    pg_column_type = "boolean"
                elif column.type == "double":
                    pg_column_type = "double precision"
                else:
                    raise Exception, "unknown column type: " + plpy.quote_ident(column.type)

                if first:
                    first = False
                else:
                    create_sql += ",\n  "
                    insert_sql += ",\n  "

                create_sql += plpy.quote_ident(column.name) + ' ' + pg_column_type
                insert_sql += plpy.quote_ident(column.name)
                values_types.append(pg_column_type)

            create_sql += '\n)'
            #if trait:
            #    create_sql += ' '
            #    create_sql += trait
            create_sql += ';'

            insert_sql += '\n) values\n'
            values_sql_format = '(' + ', '.join(['${}'] * column_num) + ')'

            #plpy.execute("drop table if exists "+plpy.quote_ident(table_name))
            plpy.execute(create_sql)

            batch = []
            for row in q.results():
                batch.append(row)
                if len(batch) > 10:
                    batch_len = len(batch)
                    # format string 'values ($1, $2), ($3, $4) ...'
                    values_sql = (', '.join([values_sql_format] * batch_len)).format(*range(1, batch_len * column_num + 1))
                    batch_insert_sql = insert_sql + values_sql
                    # flatten rows into an array
                    params = [item for sublist in batch for item in sublist]
                    plpy.execute(plpy.prepare(batch_insert_sql, values_types * batch_len), params)
                    del batch[:]

            if batch:
                batch_len = len(batch)
                # format string 'values ($1, $2), ($3, $4) ...'
                values_sql = (', '.join([values_sql_format] * batch_len)).format(*range(1, batch_len * column_num + 1))
                batch_insert_sql = insert_sql + values_sql
                # flatten rows into an array
                params = [item for sublist in batch for item in sublist]
                plpy.execute(plpy.prepare(batch_insert_sql, values_types * batch_len), params)

        finally:
            q.close()

    except (plpy.SPIError, presto_client.PrestoException) as e:
        # PL/Python converts an exception object in Python to an error message in PostgreSQL
        # using exception class name if exc.__module__ is either of "builtins", "exceptions",
        # or "__main__". Otherwise using "module.name" format. Set __module__ = "__module__"
        # to generate pretty messages.
        e.__class__.__module__ = "__main__"
        raise

Column = namedtuple('Column', ('name', 'type', 'nullable'))

cache_expire_times = {}

def presto_create_tables(server, user, catalog):
    try:
        client = presto_client.Client(server=server, user=user, catalog=catalog, schema="default")

        cache_key = "%s:%s.%s" % (server, user, catalog)
        expire_time = cache_expire_times.get(cache_key)
        if expire_time is not None and time.time() - expire_time < 10:
            # TODO scan cache_expire_times and remove expired cache entries if it is large
            return

        schemas = {}

        columns, rows = client.run("select table_schema, table_name, column_name, is_nullable, data_type from information_schema.columns")

        if rows is None:
            return

        for row in rows:
            schema_name = row[0]
            table_name = row[1]
            column_name = row[2]
            is_nullable = row[3]
            column_type = row[4]

            tables = schemas.setdefault(schema_name, {})
            columns = tables.setdefault(table_name, [])
            columns.append(Column(column_name, column_type, is_nullable))

        # delete all schemas excepting pgpool_catalog and prestogres_catalog
        for row in plpy.cursor("select n.nspname as schema_name from pg_catalog.pg_namespace n"
                               " where n.nspname not in ('pgpool_catalog', 'prestogres_catalog', 'pg_catalog', 'information_schema', 'public')"
                               " and n.nspname !~ '^pg_toast'"):
            plpy.execute("drop schema %s cascade" % plpy.quote_ident(row["schema_name"]))

        # delete all tables in pgpool_catalog and prestogres_catalog
        for row in plpy.cursor("select n.nspname as schema_name, c.relname as table_name from pg_catalog.pg_class c"
                               " left join pg_catalog.pg_namespace n on n.oid = c.relnamespace"
                               " where c.relkind in ('r')"  # take only tables and skip views, indexes, etc.
                               " and n.nspname in ('pgpool_catalog', 'prestogres_catalog')"
                               " and n.nspname !~ '^pg_toast'"):
            plpy.execute("drop table %s.%s" % (plpy.quote_ident(row["schema_name"]), plpy.quote_ident(row["table_name"])))

        for schema_name, tables in schemas.items():
            if schema_name == "sys" or schema_name == "information_schema":
                # skip system schemas
                continue

            # create schema
            try:
                plpy.execute("create schema %s" % plpy.quote_ident(schema_name))
            except:
                # ignore error
                pass

            for table_name, columns in tables.items():
                create_sql = "create table %s.%s (\n  " % (plpy.quote_ident(schema_name), plpy.quote_ident(table_name))

                first = True
                for column in columns:
                    if column.type == "varchar":
                        pg_column_type = "varchar(100)"
                    elif column.type == "bigint":
                        pg_column_type = "bigint"
                    elif column.type == "boolean":
                        pg_column_type = "boolean"
                    elif column.type == "double":
                        pg_column_type = "double precision"
                    else:
                        raise Exception("unknown column type: " + plpy.quote_ident(column.type))

                    if first:
                        first = False
                    else:
                        create_sql += ",\n  "

                    create_sql += plpy.quote_ident(column.name) + " " + pg_column_type
                    if not column.nullable:
                        create_sql += " not null"

                create_sql += "\n)"

                plpy.execute("drop table if exists %s.%s" % (plpy.quote_ident(schema_name), plpy.quote_ident(table_name)))
                plpy.execute(create_sql)

        cache_expire_times[cache_key] = time.time()

    except (plpy.SPIError, presto_client.PrestoException) as e:
        # Set __module__ = "__module__" to generate pretty messages.
        e.__class__.__module__ = "__main__"
        raise

