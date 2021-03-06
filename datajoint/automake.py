"""Defines Subclass of Autopopulate called Automake
"""

import re
import pandas
import collections
import warnings
import sys
import numpy as np

from .table import FreeTable
from .autopopulate import AutoPopulate
from .expression import AndList
from .utils import ClassProperty
from .errors import DataJointError
from .settings_table import Settingstable

if sys.version_info[1] < 6:
    dict = collections.OrderedDict

Sequence = (collections.MutableSequence, tuple, set)


class AutoMake(AutoPopulate):
    """
    AutoMake is a mixin class for autocomputed and autoimported tables.
    It adds a settings table upstream and make method.
    """

    _settings_table = None
    _settings = None

    def make_compatible(self, data):
        """function that can be defined for each class to transform data after
        computation.
        """
        return data

    def populate(self, settings_name, *restrictions, **kwargs):
        """
        rel.populate() calls rel.make(key) for every primary key in self.key_source
        for which there is not already a tuple in rel.
        :param settings_name: name of settings to use for autopopulation from SettingsTable.
        :param restrictions: a list of restrictions each restrict (rel.key_source - target.proj())
        :param suppress_errors: if True, do not terminate execution.
        :param return_exception_objects: return error objects instead of just error messages
        :param reserve_jobs: if true, reserves job to populate in asynchronous fashion
        :param order: "original"|"reverse"|"random"  - the order of execution
        :param display_progress: if True, report progress_bar
        :param limit: if not None, checks at most that many keys
        :param max_calls: if not None, populates at max that many keys
        """

        setting_restrict = {'settings_name': settings_name}

        settings = (self.settings_table & setting_restrict).fetch1()
        settings['fetch_tables'] = (
            settings['fetch_tables'] & AndList(restrictions)
        )
        self._settings = settings

        if settings['restrictions'] is not None:
            restrictions = [settings['restrictions']] + list(restrictions)

        super().populate(
            setting_restrict, *restrictions,
            **kwargs
        )

    def make(self, key):
        """automated make method
        """

        table = self._settings['fetch_tables'] & key

        if 'fetch1' in self._settings['fetch_method']:
            entry = getattr(
                table,
                self._settings['fetch_method']
            )()

        else:
            if len(table) == 0:
                raise DataJointError(
                    'empty joined table for key {}'.format(key)
                )

            entry = getattr(
                table,
                self._settings['fetch_method']
            )(format='frame').to_dict('list')

            for column, value in entry.items():
                if column in self._settings['parse_unique']:
                    # TODO check if unique?
                    entry[column] = value[0]

                else:
                    entry[column] = np.array(value)

        args, kwargs = self._create_kwargs(
            entry,
            self._settings['entry_settings'],
            self._settings['global_settings'],
            self._settings['args'],
            self._settings['kwargs']
        )

        func = self._settings['func']
        output = func(*args, **kwargs)

        output = self.make_compatible(output)

        if output is None:
            warnings.warn('output of function is None for key {}'.format(key))
            output = {}

        #Test if dict or dataframe, convert to dataframe if necessary
        if isinstance(output, np.recarray):
            output = pandas.DataFrame(output)

        if (
            self.has_part_tables
            and not isinstance(output, (pandas.DataFrame, dict))
        ):
            raise DataJointError(
                "output must be dataframe or dict for table with part tables."
            )

        elif not self.has_part_tables and not isinstance(output, dict):
            raise DataJointError(
                "ouput must be dict for table without part tables."
            )

        # settings name - add to output
        output['settings_name'] = self._settings['settings_name']

        # add columns that are missing in the output
        for column in (set(self.heading) & set(entry) - set(output)):
            if entry[column] is None or pandas.isnull(entry[column]):
                continue
            output[column] = entry[column]

        # insert into table and part_table
        if self.has_part_tables:
            self.insert1p(output)
        else:
            self.insert1(output)

    @staticmethod
    def _create_kwargs(
        entry, entry_settings, global_settings,
        settings_args, settings_kwargs
    ):
        """create args and kwargs to pass to function
        """
        args = []
        kwargs = global_settings.copy()
        # substitute entry settings
        for kw, arg in entry_settings.items():
            if isinstance(arg, str):
                kwargs[kw] = entry[arg]
            elif isinstance(arg, Sequence):
                kwargs[kw] = arg.__class__(
                    [entry[iarg] for iarg in arg]
                )
            elif isinstance(arg, collections.Mapping):
                kwargs[kw] = {
                    key: entry[iarg] for key, iarg in arg.items()
                }
            else:
                raise DataJointError(
                    "argument in entry settings must be "
                    "str, tuple, or list, but is "
                    f"{type(arg)} for {kw}"
                )

        if settings_args is not None:
            args.extend(kwargs.pop(settings_args))
        if settings_kwargs is not None:
            kw = kwargs.pop(settings_kwargs)
            kwargs.update(kw)
        #
        return args, kwargs

    @ClassProperty
    def settings_table(cls):
        """return settings table
        """

        if cls._settings_table is None:
            # dynamically assign settings table

            settings_table_name = cls.name + 'Settings'
            child_table = cls

            class Settings(Settingstable):

                @ClassProperty
                def name(cls):
                    return settings_table_name

                @ClassProperty
                def child_table(cls):
                    return child_table

            cls._settings_table = Settings

        return cls._settings_table

    @classmethod
    def set_true_definition(cls):
        """add settings table attribute if not in definition
        """

        settings_table_attribute = '-> {}'.format(cls.settings_table.name)

        if isinstance(cls.definition, property):
            pass
        elif settings_table_attribute not in cls.definition:

            definition = re.split(r'\s*\n\s*', cls.definition.strip())

            in_key_index = None

            for line_index, line in enumerate(definition):
                if line.startswith('---') or line.startswith('___'):
                    in_key_index = line_index
                    break

            if in_key_index is None:
                definition.insert(-1, settings_table_attribute)
            else:
                definition.insert(in_key_index, settings_table_attribute)

            cls.definition = '\n'.join(definition)

        return cls

    def primary_parents(self, columns, restrictions=None):
        """returns joined parent tables excluding settings table.
        Uses columns to select what to project and restrictions for
        each table individually.
        """

        joined_primary_parents = None

        if self.target.full_table_name not in self.connection.dependencies:
            self.connection.dependencies.load()

        for parent_name, fk_props in self.target.parents(primary=True).items():

            if parent_name == self.settings_table.full_table_name:
                continue

            elif not parent_name.isdigit():  # simple foreign key
                freetable = FreeTable(self.connection, parent_name)

            else:
                grandparent = list(
                    self.connection.dependencies.in_edges(parent_name)
                )[0][0]
                freetable = FreeTable(self.connection, grandparent)

            proj_columns = list(set(freetable.heading.names) & set(columns))
            proj_table = freetable.proj(*proj_columns)
            if restrictions is not None:
                proj_table = proj_table & restrictions

            if joined_primary_parents is None:
                joined_primary_parents = proj_table
            else:
                joined_primary_parents = joined_primary_parents * proj_table

        return joined_primary_parents
