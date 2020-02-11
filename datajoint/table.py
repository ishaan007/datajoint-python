import collections
import itertools
import inspect
import platform
import warnings
import numpy as np
import pandas
import logging
import uuid
from pathlib import Path
from .settings import config
from .declare import declare, alter
from .expression import QueryExpression
from . import blob
from .utils import user_choice, to_camel_case
from .heading import Heading
from .errors import DuplicateError, AccessError, DataJointError, UnknownAttributeError
from .version import __version__ as version

logger = logging.getLogger(__name__)


class _RenameMap(tuple):
    """ for internal use """
    pass


class Table(QueryExpression):
    """
    Table is an abstract class that represents a base relation, i.e. a table in the schema.
    To make it a concrete class, override the abstract properties specifying the connection,
    table name, database, and definition.
    A Relation implements insert and delete methods in addition to inherited relational operators.
    """
    _heading = None
    database = None
    _log_ = None
    declaration_context = None
    _part_tables = None

    # -------------- required by QueryExpression ----------------- #
    @property
    def heading(self):
        """
        Returns the table heading. If the table is not declared, attempts to declare it and return heading.
        :return: table heading
        """
        if self._heading is None:
            self._heading = Heading()  # instance-level heading
        if not self._heading and self.connection is not None:  # lazy loading of heading
            self._heading.init_from_database(
                self.connection, self.database, self.table_name, self.declaration_context)
        return self._heading

    def declare(self, context=None):
        """
        Declare the table in the schema based on self.definition.
        :param context: the context for foreign key resolution. If None, foreign keys are not allowed.
        """
        if self.connection.in_transaction:
            raise DataJointError('Cannot declare new tables inside a transaction, '
                                 'e.g. from inside a populate/make call')
        sql, external_stores = declare(self.full_table_name, self.definition, context)
        sql = sql.format(database=self.database)
        try:
            # declare all external tables before declaring main table
            for store in external_stores:
                self.connection.schemas[self.database].external[store]
            self.connection.query(sql)
        except AccessError:
            # skip if no create privilege
            pass
        else:
            self._log('Declared ' + self.full_table_name)

    def alter(self, prompt=True, context=None):
        """
        Alter the table definition from self.definition
        """
        if self.connection.in_transaction:
            raise DataJointError('Cannot update table declaration inside a transaction, '
                                 'e.g. from inside a populate/make call')
        if context is None:
            frame = inspect.currentframe().f_back
            context = dict(frame.f_globals, **frame.f_locals)
            del frame
        old_definition = self.describe(context=context, printout=False)
        sql, external_stores = alter(self.definition, old_definition, context)
        if not sql:
            if prompt:
                print('Nothing to alter.')
        else:
            sql = "ALTER TABLE {tab}\n\t".format(tab=self.full_table_name) + ",\n\t".join(sql)
            if not prompt or user_choice(sql + '\n\nExecute?') == 'yes':
                try:
                    # declare all external tables before declaring main table
                    for store in external_stores:
                        self.connection.schemas[self.database].external[store]
                    self.connection.query(sql)
                except AccessError:
                    # skip if no create privilege
                    pass
                else:
                    if prompt:
                        print('Table altered')
                    self._log('Altered ' + self.full_table_name)

    @property
    def from_clause(self):
        """
        :return: the FROM clause of SQL SELECT statements.
        """
        return self.full_table_name

    def get_select_fields(self, select_fields=None):
        """
        :return: the selected attributes from the SQL SELECT statement.
        """
        return '*' if select_fields is None else self.heading.project(select_fields).as_sql

    def parents(self, primary=None):
        """
        :param primary: if None, then all parents are returned. If True, then only foreign keys composed of
            primary key attributes are considered.  If False, the only foreign keys including at least one non-primary
            attribute are considered.
        :return: dict of tables referenced with self's foreign keys
        """
        return self.connection.dependencies.parents(self.full_table_name, primary)

    @property
    def part_tables(self):
        """a tuple of all part tables
        :return: tuple of part tables of self
        """

        if self._part_tables is None:

            # TODO general load checking
            self.connection.dependencies.load()

            part_tables = []
            self_table_name = self.full_table_name.replace('`', '')

            for child_table_name, child_info in self.children().items():

                if child_info['aliased']:
                    aliased_children = self.connection.dependencies.children(
                        child_table_name
                    )
                    # there should only be one aliased child
                    child_table_name = list(aliased_children)[0]

                child_table_name = child_table_name.replace('`', '')

                if child_table_name.startswith(self_table_name + '__'):

                    part_table_name = child_table_name.replace(
                        self_table_name + '__', ''
                    )

                    try:
                        part_table = getattr(
                            self,
                            to_camel_case(part_table_name)
                        )
                    except AttributeError:
                        part_table = FreeTable(
                            self.connection, child_table_name
                        )

                    part_tables.append(part_table)

            self._part_tables = tuple(part_tables)

        return self._part_tables

    @property
    def has_part_tables(self):
        """True if self has part tables.
        """

        return bool(self.part_tables)

    def children(self, primary=None):
        """
        :param primary: if None, then all children are returned. If True, then only foreign keys composed of
            primary key attributes are considered.  If False, the only foreign keys including at least one non-primary
            attribute are considered.
        :return: dict of tables with foreign keys referencing self
        """
        return self.connection.dependencies.children(self.full_table_name, primary)

    def descendants(self):
        return self.connection.dependencies.descendants(self.full_table_name)

    def ancestors(self):
        return self.connection.dependencies.ancestors(self.full_table_name)

    @property
    def is_declared(self):
        """
        :return: True is the table is declared in the schema.
        """
        return self.connection.query(
            'SHOW TABLES in `{database}` LIKE "{table_name}"'.format(
                database=self.database, table_name=self.table_name)).rowcount > 0

    @property
    def full_table_name(self):
        """
        :return: full table name in the schema
        """
        return r"`{0:s}`.`{1:s}`".format(self.database, self.table_name)

    @property
    def _log(self):
        if self._log_ is None:
            self._log_ = Log(self.connection, database=self.database)
        return self._log_

    @property
    def external(self):
        return self.connection.schemas[self.database].external

    def insert1p(self, rows, **kwargs):
        """
        Insert data records or Mapping for one entry and its part tables.
        It does not check if extra fields are defined.
        Only tests if master primary keys are unique.

        :param rows: a pandas DataFrame, numpy.recarray, or dict
        """

        if isinstance(rows, np.recarray):
            rows = pandas.DataFrame(rows)
        elif isinstance(rows, dict):
            rows = pandas.DataFrame([rows])
        elif not isinstance(rows, pandas.DataFrame):
            raise DataJointError(
                'rows must be of type pandas dataframe, numpy recarray, '
                'or dict for insert1p, but is of type {}'.format(type(rows)))

        columns = set(self.heading) & set(rows.columns)
        assert not (set(self.primary_key) - columns), \
            'not all master primary key in dataframe for insert1p.'
        # select self columns
        master_input = rows[list(columns)]
        # test if self primary is unique
        assert len(master_input[self.primary_key].drop_duplicates()) == 1, \
            'master primary keys are not unique.'
        # convert master input to dictionary
        master_input = master_input.iloc[0].dropna()
        master_input = master_input.to_dict()

        def helper():
            # insert into self
            self.insert1(master_input, **kwargs)

            # insert into part tables
            for part_table in self.part_tables:
                part_columns = set(part_table.heading) & set(rows.columns)
                if not part_columns:
                    continue
                # select correct columns and drop duplicates
                part_input = rows[list(part_columns)].drop_duplicates(
                    part_table.primary_key
                )
                part_input = part_input.to_dict('records')
                # try to insert into part table
                part_table.insert(part_input, **kwargs)

        # if in transaction skip context
        if self.connection.in_transaction:
            helper()
        else:
            with self.connection.transaction:
                helper()

    def insert1(self, row, **kwargs):
        """
        Insert one data record or one Mapping (like a dict).
        :param row: a numpy record, a dict-like object, or an ordered sequence to be inserted as one row.
        For kwargs, see insert()
        """
        self.insert((row,), **kwargs)

    def insert(self, rows, replace=False, skip_duplicates=False, ignore_extra_fields=False, allow_direct_insert=None):
        """
        Insert a collection of rows.

        :param rows: An iterable where an element is a numpy record, a dict-like object, a pandas.DataFrame, a sequence,
            or a query expression with the same heading as table self.
        :param replace: If True, replaces the existing tuple.
        :param skip_duplicates: If True, silently skip duplicate inserts.
        :param ignore_extra_fields: If False, fields that are not in the heading raise error.
        :param allow_direct_insert: applies only in auto-populated tables. If False (default), insert are allowed only from inside the make callback.

        Example::
        >>> relation.insert([
        >>>     dict(subject_id=7, species="mouse", date_of_birth="2014-09-01"),
        >>>     dict(subject_id=8, species="mouse", date_of_birth="2014-09-02")])
        """

        if isinstance(rows, pandas.DataFrame):
            rows = rows.to_records()

        # prohibit direct inserts into auto-populated tables
        if not allow_direct_insert and not getattr(self, '_allow_insert', True):  # allow_insert is only used in AutoPopulate
            raise DataJointError(
                'Inserts into an auto-populated table can only done inside its make method during a populate call.'
                ' To override, set keyword argument allow_direct_insert=True.')

        heading = self.heading
        if inspect.isclass(rows) and issubclass(rows, QueryExpression):   # instantiate if a class
            rows = rows()
        if isinstance(rows, QueryExpression):
            # insert from select
            if not ignore_extra_fields:
                try:
                    raise DataJointError(
                        "Attribute %s not found. To ignore extra attributes in insert, set ignore_extra_fields=True." %
                        next(name for name in rows.heading if name not in heading))
                except StopIteration:
                    pass
            fields = list(name for name in rows.heading if name in heading)
            query = '{command} INTO {table} ({fields}) {select}{duplicate}'.format(
                command='REPLACE' if replace else 'INSERT',
                fields='`' + '`,`'.join(fields) + '`',
                table=self.full_table_name,
                select=rows.make_sql(select_fields=fields),
                duplicate=(' ON DUPLICATE KEY UPDATE `{pk}`={table}.`{pk}`'.format(
                    table=self.full_table_name, pk=self.primary_key[0])
                           if skip_duplicates else ''))
            self.connection.query(query)
            return

        if heading.attributes is None:
            logger.warning('Could not access table {table}'.format(table=self.full_table_name))
            return

        field_list = None  # ensures that all rows have the same attributes in the same order as the first row.

        def make_row_to_insert(row):
            """
            :param row:  A tuple to insert
            :return: a dict with fields 'names', 'placeholders', 'values'
            """

            nonlocal field_list
            row_to_insert = self._make_row_to_insert(row, ignore_extra_fields, field_list)
            if field_list is None:
                # first row sets the composition of the field list
                field_list = row_to_insert['names']
            else:
                #  reorder attributes in row_to_insert to match field_list
                order = list(row_to_insert['names'].index(field) for field in field_list)
                row_to_insert['names'] = list(row_to_insert['names'][i] for i in order)
                row_to_insert['placeholders'] = list(row_to_insert['placeholders'][i] for i in order)
                row_to_insert['values'] = list(row_to_insert['values'][i] for i in order)

            return row_to_insert

        rows = list(make_row_to_insert(row) for row in rows)
        if rows:
            try:
                query = "{command} INTO {destination}(`{fields}`) VALUES {placeholders}{duplicate}".format(
                    command='REPLACE' if replace else 'INSERT',
                    destination=self.from_clause,
                    fields='`,`'.join(field_list),
                    placeholders=','.join('(' + ','.join(row['placeholders']) + ')' for row in rows),
                    duplicate=(' ON DUPLICATE KEY UPDATE `{pk}`=`{pk}`'.format(pk=self.primary_key[0])
                               if skip_duplicates else ''))
                self.connection.query(query, args=list(
                    itertools.chain.from_iterable((v for v in r['values'] if v is not None) for r in rows)))
            except UnknownAttributeError as err:
                raise err.suggest('To ignore extra fields in insert, set ignore_extra_fields=True') from None
            except DuplicateError as err:
                raise err.suggest('To ignore duplicate entries in insert, set skip_duplicates=True') from None

    def _make_row_to_insert(self, row, ignore_extra_fields, field_list):
        """
        :param row:  A tuple to insert
        :return: a dict with fields 'names', 'placeholders', 'values'
        """

        if isinstance(row, np.void):  # np.array
            self._check_fields(row.dtype.fields, field_list, ignore_extra_fields)
            attributes = [self._make_placeholder(name, row[name], ignore_extra_fields)
                          for name in self.heading if name in row.dtype.fields]
        elif isinstance(row, collections.abc.Mapping):  # dict-based
            self._check_fields(row, field_list, ignore_extra_fields)
            attributes = [self._make_placeholder(name, row[name], ignore_extra_fields)
                          for name in self.heading if name in row]
        else:  # positional
            try:
                if len(row) != len(self.heading):
                    raise DataJointError(
                        'Invalid insert argument. Incorrect number of attributes: '
                        '{given} given; {expected} expected'.format(
                            given=len(row), expected=len(self.heading)))
            except TypeError:
                raise DataJointError('Datatype %s cannot be inserted' % type(row))
            else:
                attributes = [self._make_placeholder(name, value, ignore_extra_fields)
                              for name, value in zip(self.heading, row)]
        if ignore_extra_fields:
            attributes = [a for a in attributes if a is not None]

        assert len(attributes), 'Empty tuple'
        return dict(zip(('names', 'placeholders', 'values'), zip(*attributes)))

    def _check_fields(self, fields, field_list, ignore_extra_fields):
        """
        Validates that all items in `fields` are valid attributes in the heading
        :param fields: field names of a tuple
        """
        if field_list is None:
            if not ignore_extra_fields:
                for field in fields:
                    if field not in self.heading:
                        raise KeyError(u'`{0:s}` is not in the table heading'.format(field))
        elif set(field_list) != set(fields).intersection(self.heading.names):
            raise DataJointError('Attempt to insert rows with different fields')

    def _make_placeholder(self, name, value, ignore_extra_fields):
        """
        For a given attribute `name` with `value`, return its processed value or value placeholder
        as a string to be included in the query and the value, if any, to be submitted for
        processing by mysql API.
        :param name:  name of attribute to be inserted
        :param value: value of attribute to be inserted
        """
        if ignore_extra_fields and name not in self.heading:
            return None
        attr = self.heading[name]

        if attr.adapter:
            try:
                value = attr.adapter.put(value)
            except NotImplementedError:
                raise DataJointError('put not implemented for adapted attribute "{}" with adapted type "{}"'.format(attr.name, attr.type))
        if value is None or (attr.numeric and (value == '' or np.isnan(np.float(value)))):
            # set default value
            placeholder, value = 'DEFAULT', None
        else:  # not NULL
            placeholder = '%s'
            if attr.uuid:
                if not isinstance(value, uuid.UUID):
                    try:
                        value = uuid.UUID(value)
                    except (AttributeError, ValueError):
                        raise DataJointError(
                            'badly formed UUID value {v} for attribute `{n}`'.format(v=value, n=name)) from None
                value = value.bytes
            elif attr.is_blob:
                value = blob.pack(value)
                value = self.external[attr.store].put(value).bytes if attr.is_external else value
            elif attr.is_attachment:
                attachment_path = Path(value)
                if attr.is_external:
                    # value is hash of contents
                    value = self.external[attr.store].upload_attachment(attachment_path).bytes
                else:
                    # value is filename + contents
                    value = str.encode(attachment_path.name) + b'\0' + attachment_path.read_bytes()
            elif attr.is_filepath:
                value = self.external[attr.store].upload_filepath(value).bytes
            elif attr.numeric:
                value = str(int(value) if isinstance(value, bool) else value)
        return name, placeholder, value

    def delete_quick(self, get_count=False):
        """
        Deletes the table without cascading and without user prompt.
        If this table has populated dependent tables, this will fail.
        """
        query = 'DELETE FROM ' + self.full_table_name + self.where_clause
        self.connection.query(query)
        count = self.connection.query("SELECT ROW_COUNT()").fetchone()[0] if get_count else None
        self._log(query[:255])
        return count

    def _delete(self, verbose=True, force=False):
        """delete helper function. Called in delete.
        """
        # message and commit transaction are returned as well as connection
        message = ''
        commit_transaction = False

        # copied from old delete method
        conn = self.connection
        already_in_transaction = conn.in_transaction
        safe = config['safemode']
        if already_in_transaction and safe:
            raise DataJointError('Cannot delete within a transaction in safemode. '
                                 'Set dj.config["safemode"] = False or complete the ongoing transaction first.')
        graph = conn.dependencies
        graph.load()
        delete_list = collections.OrderedDict(
            (name, _RenameMap(next(iter(graph.parents(name).items()))) if name.isdigit() else FreeTable(conn, name))
            for name in graph.descendants(self.full_table_name))

        # construct restrictions for each relation
        restrict_by_me = set()
        # restrictions: Or-Lists of restriction conditions for each table.
        # Uncharacteristically of Or-Lists, an empty entry denotes "delete everything".
        restrictions = collections.defaultdict(list)
        # restrict by self
        if self.restriction:
            restrict_by_me.add(self.full_table_name)
            restrictions[self.full_table_name].append(self.restriction)  # copy own restrictions
        # restrict by renamed nodes
        restrict_by_me.update(table for table in delete_list if table.isdigit())  # restrict by all renamed nodes
        # restrict by secondary dependencies
        for table in delete_list:
            restrict_by_me.update(graph.children(table, primary=False))   # restrict by any non-primary dependents

        # compile restriction lists
        for name, table in delete_list.items():
            for dep in graph.children(name):
                # if restrict by me, then restrict by the entire relation otherwise copy restrictions
                restrictions[dep].extend([table] if name in restrict_by_me else restrictions[name])

        # apply restrictions
        for name, table in delete_list.items():
            if not name.isdigit() and restrictions[name]:  # do not restrict by an empty list
                table.restrict([
                    r.proj() if isinstance(r, FreeTable) else (
                        delete_list[r[0]].proj(**{a: b for a, b in r[1]['attr_map'].items()})
                        if isinstance(r, _RenameMap) else r)
                    for r in restrictions[name]])
        if safe:
            message += 'About to delete'

        if not already_in_transaction:
            conn.start_transaction()
        total = 0
        try:
            for name, table in reversed(list(delete_list.items())):
                if not name.isdigit():
                    count = table.delete_quick(get_count=True)
                    total += count
                    if (verbose or safe) and count:
                        message += '\n{table}: {count} items'.format(table=name, count=count)
        except:
            # Delete failed, perhaps due to insufficient privileges. Cancel transaction.
            if not already_in_transaction:
                conn.cancel_transaction()
            raise
        else:
            assert not (already_in_transaction and safe)
            if not total:
                message = ('Nothing to delete')
                if not already_in_transaction:
                    conn.cancel_transaction()
            else:
                if already_in_transaction:
                    if verbose:
                        message = ('\nThe delete is pending within the ongoing transaction.')
                else:
                    commit_transaction = not safe or force

        return message, commit_transaction, conn

    def delete(self, verbose=True, force=False):
        """
        Deletes the contents of the table and its dependent tables, recursively.
        User is prompted for confirmation if config['safemode'] is set to True.
        """
        safe = config['safemode']
        message, commit_transaction, conn = self._delete(verbose, force)

        print(message)

        if commit_transaction or user_choice("Proceed?", default='no') == 'yes':
            conn.commit_transaction()
            if verbose or safe:
                print('Commited.')
        else:
            conn.cancel_transaction()
            if verbose or safe:
                message = ('\nCancelled deletes.')

    def drop_quick(self):
        """
        Drops the table associated with this relation without cascading and without user prompt.
        If the table has any dependent table(s), this call will fail with an error.
        """
        if self.is_declared:
            query = 'DROP TABLE %s' % self.full_table_name
            self.connection.query(query)
            logger.info("Dropped table %s" % self.full_table_name)
            self._log(query[:255])
        else:
            logger.info("Nothing to drop: table %s is not declared" % self.full_table_name)

    def drop(self):
        """
        Drop the table and all tables that reference it, recursively.
        User is prompted for confirmation if config['safemode'] is set to True.
        """
        if self.restriction:
            raise DataJointError('A relation with an applied restriction condition cannot be dropped.'
                                 ' Call drop() on the unrestricted Table.')
        self.connection.dependencies.load()
        do_drop = True
        tables = [table for table in self.connection.dependencies.descendants(self.full_table_name)
                  if not table.isdigit()]
        if config['safemode']:
            for table in tables:
                print(table, '(%d tuples)' % len(FreeTable(self.connection, table)))
            do_drop = user_choice("Proceed?", default='no') == 'yes'
        if do_drop:
            for table in reversed(tables):
                FreeTable(self.connection, table).drop_quick()
            print('Tables dropped.  Restart kernel.')

    @property
    def size_on_disk(self):
        """
        :return: size of data and indices in bytes on the storage device
        """
        ret = self.connection.query(
            'SHOW TABLE STATUS FROM `{database}` WHERE NAME="{table}"'.format(
                database=self.database, table=self.table_name), as_dict=True).fetchone()
        return ret['Data_length'] + ret['Index_length']

    def show_definition(self):
        raise AttributeError('show_definition is deprecated. Use the describe method instead.')

    def describe(self, context=None, printout=True):
        """
        :return:  the definition string for the relation using DataJoint DDL.
        """
        if context is None:
            frame = inspect.currentframe().f_back
            context = dict(frame.f_globals, **frame.f_locals)
            del frame
        if self.full_table_name not in self.connection.dependencies:
            self.connection.dependencies.load()
        parents = self.parents()
        in_key = True
        definition = ('# ' + self.heading.table_info['comment'] + '\n'
                      if self.heading.table_info['comment'] else '')
        attributes_thus_far = set()
        attributes_declared = set()
        indexes = self.heading.indexes.copy()
        for attr in self.heading.attributes.values():
            if in_key and not attr.in_key:
                definition += '---\n'
                in_key = False
            attributes_thus_far.add(attr.name)
            do_include = True
            for parent_name, fk_props in list(parents.items()):  # need list() to force a copy
                if attr.name in fk_props['attr_map']:
                    do_include = False
                    if attributes_thus_far.issuperset(fk_props['attr_map']):
                        parents.pop(parent_name)
                        # foreign key properties
                        try:
                            index_props = indexes.pop(tuple(fk_props['attr_map']))
                        except KeyError:
                            index_props = ''
                        else:
                            index_props = [k for k, v in index_props.items() if v]
                            index_props = ' [{}]'.format(', '.join(index_props)) if index_props else ''

                        if not parent_name.isdigit():
                            # simple foreign key
                            definition += '->{props} {class_name}\n'.format(
                                props=index_props,
                                class_name=lookup_class_name(parent_name, context) or parent_name)
                        else:
                            # projected foreign key
                            parent_name = list(self.connection.dependencies.in_edges(parent_name))[0][0]
                            lst = [(attr, ref) for attr, ref in fk_props['attr_map'].items() if ref != attr]
                            definition += '->{props} {class_name}.proj({proj_list})\n'.format(
                                attr_list=', '.join(r[0] for r in lst),
                                props=index_props,
                                class_name=lookup_class_name(parent_name, context) or parent_name,
                                proj_list=','.join('{}="{}"'.format(a,b) for a, b in lst))
                            attributes_declared.update(fk_props['attr_map'])
            if do_include:
                attributes_declared.add(attr.name)
                definition += '%-20s : %-28s %s\n' % (
                    attr.name if attr.default is None else '%s=%s' % (attr.name, attr.default),
                    '%s%s' % (attr.type, ' auto_increment' if attr.autoincrement else ''),
                    '# ' + attr.comment if attr.comment else '')
        # add remaining indexes
        for k, v in indexes.items():
            definition += '{unique}INDEX ({attrs})\n'.format(
                unique='UNIQUE ' if v['unique'] else '',
                attrs=', '.join(k))
        if printout:
            print(definition)
        return definition

    def _update(self, attrname, value=None):
        """
            Updates a field in an existing tuple. This is not a datajoyous operation and should not be used
            routinely. Relational database maintain referential integrity on the level of a tuple. Therefore,
            the UPDATE operator can violate referential integrity. The datajoyous way to update information is
            to delete the entire tuple and insert the entire update tuple.

            Safety constraints:
               1. self must be restricted to exactly one tuple
               2. the update attribute must not be in primary key

            Example:
            >>> (v2p.Mice() & key).update('mouse_dob', '2011-01-01')
            >>> (v2p.Mice() & key).update( 'lens')   # set the value to NULL
        """
        if len(self) != 1:
            raise DataJointError('Update is only allowed on one tuple at a time')
        if attrname not in self.heading:
            raise DataJointError('Invalid attribute name')
        if attrname in self.heading.primary_key:
            raise DataJointError('Cannot update a key value.')

        attrname, placeholder, value = self._make_placeholder(attrname, value, False)

        command = "UPDATE {full_table_name} SET `{attrname}`={placeholder} {where_clause}".format(
            full_table_name=self.from_clause,
            attrname=attrname,
            placeholder=placeholder,
            where_clause=self.where_clause)
        self.connection.query(command, args=(value, ) if value is not None else ())

    def _check_downstream_autopopulated(self, reload=True, error='raise'):
        """
            Check if downstream autopopulated tables include entry.
        """

        if reload or not self.connection.dependencies:
            self.connection.dependencies.load()

        children = self.children()

        for child_name, child in children.items():
            if child['aliased']:
                aliased_children = self.connection.dependencies.children(
                    child_name
                )
                # there should only be one aliased child
                child_name = list(aliased_children)[0]
                child = aliased_children[child_name]

            child_table = FreeTable(self.connection, child_name)
            attr_map = {
                map_to: map_from
                for map_to, map_from in child['attr_map'].items()
                if (
                    map_from in self.heading.primary_key
                    and (map_to != map_from)
                )
            }
            restricted_table = (
                child_table
                & self.proj(**attr_map)
            )
            # recursive
            restricted_table._check_downstream_autopopulated(False, error)

            is_autopopulated = child_name.split('.')[-1].strip('`').startswith(
                ('__', '_', '_#', '#_')
            )

            if is_autopopulated and len(restricted_table) > 0:
                    message = (
                        "Save update failure: Entries of downstream "
                        "autopopulated tables depend on self. "
                        "Delete appropriate entries in dependent autopopulated"
                        " tables before editing entries in self."
                    )
                    if error == 'ignore':
                        pass
                    elif error == 'warn':
                        warnings.warn(message)
                    else:
                        raise DataJointError(message)
                    return False

        return True

    def save_update(self, attrname, value=None, reload=True, error='raise'):
        """
            Updates a field in an existing tuple, but only if no descendant
            tables are autopopulated tables with that entry.
        """

        truth = self._check_downstream_autopopulated(reload, error)

        if truth:
            self._update(attrname, value)

    def save_updates(self, updates, reload=True, error='raise'):
        """
            Updates multiple fields in an existing tuple, but only if no
            descendant tables are autopopulated tables with that entry.

            Safety constraints:
               1. self must be restricted to exactly one tuple
               2. the update attribute must not be in primary key

        """

        if len(self) != 1:
            raise DataJointError('Update is only allowed on one tuple at a time')
        if set(updates) & set(self.primary_key):
            raise DataJointError('Cannot update a key value.')
        if set(updates) - set(self.heading):
            raise DataJointError('Invalid attribute names.')

        truth = self._check_downstream_autopopulated(reload, error)

        if not truth:
            return

        row_to_insert = self._make_row_to_insert(updates, False, None)

        if not row_to_insert:
            return

        set_statement = [
            "`{0}`={1}".format(name, placeholder)
            for name, placeholder
            in zip(row_to_insert['names'], row_to_insert['placeholders'])
        ]
        set_statement = ', '.join(set_statement)

        command = "UPDATE {full_table_name} SET {set_statement} {where_clause}".format(
            full_table_name=self.from_clause,
            set_statement=set_statement,
            where_clause=self.where_clause)
        self.connection.query(
            command,
            args=[
                value
                for value in row_to_insert['values'] if value is not None
            ]
        )


def lookup_class_name(name, context, depth=3):
    """
    given a table name in the form `schema_name`.`table_name`, find its class in the context.
    :param name: `schema_name`.`table_name`
    :param context: dictionary representing the namespace
    :param depth: search depth into imported modules, helps avoid infinite recursion.
    :return: class name found in the context or None if not found
    """
    # breadth-first search
    nodes = [dict(context=context, context_name='', depth=depth)]
    while nodes:
        node = nodes.pop(0)
        for member_name, member in node['context'].items():
            if not member_name.startswith('_'):  # skip IPython's implicit variables
                if inspect.isclass(member) and issubclass(member, Table):
                    if member.full_table_name == name:   # found it!
                        return '.'.join([node['context_name'],  member_name]).lstrip('.')
                    try:  # look for part tables
                        parts = member._ordered_class_members
                    except AttributeError:
                        pass  # not a UserTable -- cannot have part tables.
                    else:
                        for part in (getattr(member, p) for p in parts if p[0].isupper() and hasattr(member, p)):
                            if inspect.isclass(part) and issubclass(part, Table) and part.full_table_name == name:
                                return '.'.join([node['context_name'], member_name, part.__name__]).lstrip('.')
                elif node['depth'] > 0 and inspect.ismodule(member) and member.__name__ != 'datajoint':
                    try:
                        nodes.append(
                            dict(context=dict(inspect.getmembers(member)),
                                 context_name=node['context_name'] + '.' + member_name,
                                 depth=node['depth']-1))
                    except ImportError:
                        pass  # could not import, so do not attempt
    return None


class FreeTable(Table):
    """
    A base relation without a dedicated class. Each instance is associated with a table
    specified by full_table_name.
    :param arg:  a dj.Connection or a dj.FreeTable
    """

    def __init__(self, arg, full_table_name=None):
        super().__init__()
        if isinstance(arg, FreeTable):
            # copy constructor
            self.database = arg.database
            self._table_name = arg._table_name
            self._connection = arg._connection
        else:
            self.database, self._table_name = (s.strip('`') for s in full_table_name.split('.'))
            self._connection = arg

    def __repr__(self):
        return "FreeTable(`%s`.`%s`)" % (self.database, self._table_name)

    @property
    def table_name(self):
        """
        :return: the table name in the schema
        """
        return self._table_name


class Log(Table):
    """
    The log table for each schema.
    Instances are callable.  Calls log the time and identifying information along with the event.
    """

    def __init__(self, arg, database=None):
        super().__init__()

        if isinstance(arg, Log):
            # copy constructor
            self.database = arg.database
            self._connection = arg._connection
            self._definition = arg._definition
            self._user = arg._user
            return

        self.database = database
        self._connection = arg
        self._definition = """    # event logging table for `{database}`
        id       :int unsigned auto_increment     # event order id
        ---
        timestamp = CURRENT_TIMESTAMP : timestamp # event timestamp
        version  :varchar(12)                     # datajoint version
        user     :varchar(255)                    # user@host
        host=""  :varchar(255)                    # system hostname
        event="" :varchar(255)                    # event message
        """.format(database=database)

        if not self.is_declared:
            self.declare()
        self._user = self.connection.get_user()

    @property
    def definition(self):
        return self._definition

    @property
    def table_name(self):
        return '~log'

    def __call__(self, event):
        try:
            self.insert1(dict(
                user=self._user,
                version=version + 'py',
                host=platform.uname().node,
                event=event), skip_duplicates=True, ignore_extra_fields=True)
        except DataJointError:
            logger.info('could not log event in table ~log')

    def delete(self):
        """bypass interactive prompts and cascading dependencies"""
        self.delete_quick()

    def drop(self):
        """bypass interactive prompts and cascading dependencies"""
        self.drop_quick()
