# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module contains `Client` class, that lets you communicate with Apache
Ignite cluster node by the means of Ignite binary client protocol.

To start the communication, you may connect to the node of their choice
by instantiating the `Client` object and calling
:py:meth:`~pyignite.connection.Connection.connect` method with proper
parameters.

The whole storage room of Ignite cluster is split up into named structures,
called caches. For accessing the particular cache in key-value style
(a-la Redis or memcached) you should first create
the :class:`~pyignite.cache.Cache` object by calling
:py:meth:`~pyignite.client.Client.create_cache` or
:py:meth:`~pyignite.client.Client.get_or_create_cache()` methods, than call
:class:`~pyignite.cache.Cache` methods. If you wish to create a cache object
without communicating with server, there is also a
:py:meth:`~pyignite.client.Client.get_cache()` method that does just that.

For using Ignite SQL, call :py:meth:`~pyignite.client.Client.sql` method.
It returns a generator with result rows.

:py:meth:`~pyignite.client.Client.register_binary_type` and
:py:meth:`~pyignite.client.Client.query_binary_type` methods operates
the local (class-wise) registry for Ignite Complex objects.
"""

from collections import defaultdict, OrderedDict
import random
import re
from itertools import chain
from typing import Dict, Iterable, List, Optional, Tuple, Type, Union, Any

from .api.binary import get_binary_type, put_binary_type
from .api.cache_config import cache_get_names
from .cursors import SqlFieldsCursor
from .cache import Cache, create_cache, get_cache, get_or_create_cache
from .connection import Connection
from .constants import IGNITE_DEFAULT_HOST, IGNITE_DEFAULT_PORT, PROTOCOL_BYTE_ORDER
from .datatypes import BinaryObject
from .datatypes.internal import tc_map
from .exceptions import BinaryTypeError, CacheError, ReconnectError, connection_errors
from .stream import BinaryStream, READ_BACKWARD
from .utils import (
    cache_id, capitalize, entity_id, schema_id, process_delimiter, status_to_exception, is_iterable, is_wrapped
)
from .binary import GenericObjectMeta


__all__ = ['Client']


class BaseClient:
    # used for Complex object data class names sanitizing
    _identifier = re.compile(r'[^0-9a-zA-Z_.+$]', re.UNICODE)
    _ident_start = re.compile(r'^[^a-zA-Z_]+', re.UNICODE)

    def __init__(self, compact_footer: bool = None, partition_aware: bool = False, **kwargs):
        self._compact_footer = compact_footer
        self._partition_aware = partition_aware
        self._connection_args = kwargs
        self._registry = defaultdict(dict)
        self._nodes = []
        self._current_node = 0
        self._partition_aware = partition_aware
        self.affinity_version = (0, 0)
        self._protocol_version = None

    @property
    def protocol_version(self):
        """
        Returns the tuple of major, minor, and revision numbers of the used
        thin protocol version, or None, if no connection to the Ignite cluster
        was not yet established.

        This method is not a part of the public API. Unless you wish to
        extend the `pyignite` capabilities (with additional testing, logging,
        examining connections, et c.) you probably should not use it.
        """
        return self._protocol_version

    @protocol_version.setter
    def protocol_version(self, value):
        self._protocol_version = value

    @property
    def partition_aware(self):
        return self._partition_aware and self.partition_awareness_supported_by_protocol

    @property
    def partition_awareness_supported_by_protocol(self):
        return self.protocol_version is not None and self.protocol_version >= (1, 4, 0)

    @property
    def compact_footer(self) -> bool:
        """
        This property remembers Complex object schema encoding approach when
        decoding any Complex object, to use the same approach on Complex
        object encoding.

        :return: True if compact schema was used by server or no Complex
         object decoding has yet taken place, False if full schema was used.
        """
        # this is an ordinary object property, but its backing storage
        # is a class attribute

        # use compact schema by default, but leave initial (falsy) backing
        # value unchanged
        return self._compact_footer or self._compact_footer is None

    @compact_footer.setter
    def compact_footer(self, value: bool):
        # normally schema approach should not change
        if self._compact_footer not in (value, None):
            raise Warning('Can not change client schema approach.')
        else:
            self._compact_footer = value

    def _process_get_binary_type_result(self, result):
        if result.status != 0 or not result.value['type_exists']:
            return result

        binary_fields = result.value.pop('binary_fields')
        old_format_schemas = result.value.pop('schema')
        result.value['schemas'] = []
        for s_id, field_ids in old_format_schemas.items():
            result.value['schemas'].append(self._convert_schema(field_ids, binary_fields))
        return result

    @staticmethod
    def _convert_type(tc_type: int):
        try:
            return tc_map(tc_type.to_bytes(1, PROTOCOL_BYTE_ORDER))
        except (KeyError, OverflowError):
            # if conversion to char or type lookup failed,
            # we probably have a binary object type ID
            return BinaryObject

    def _convert_schema(self, field_ids: list, binary_fields: list) -> OrderedDict:
        converted_schema = OrderedDict()
        for field_id in field_ids:
            binary_field = next(x for x in binary_fields if x['field_id'] == field_id)
            converted_schema[binary_field['field_name']] = self._convert_type(binary_field['type_id'])
        return converted_schema

    @staticmethod
    def _create_dataclass(type_name: str, schema: OrderedDict = None) -> Type:
        """
        Creates default (generic) class for Ignite Complex object.

        :param type_name: Complex object type name,
        :param schema: Complex object schema,
        :return: the resulting class.
        """
        schema = schema or {}
        return GenericObjectMeta(type_name, (), {}, schema=schema)

    @classmethod
    def _create_type_name(cls, type_name: str) -> str:
        """
        Creates Python data class name from Ignite binary type name.

        Handles all the special cases found in
        `java.org.apache.ignite.binary.BinaryBasicNameMapper.simpleName()`.
        Tries to adhere to PEP8 along the way.
        """

        # general sanitizing
        type_name = cls._identifier.sub('', type_name)

        # - name ending with '$' (Scala)
        # - name + '$' + some digits (anonymous class)
        # - '$$Lambda$' in the middle
        type_name = process_delimiter(type_name, '$')

        # .NET outer/inner class delimiter
        type_name = process_delimiter(type_name, '+')

        # Java fully qualified class name
        type_name = process_delimiter(type_name, '.')

        # start chars sanitizing
        type_name = capitalize(cls._ident_start.sub('', type_name))

        return type_name

    def _sync_binary_registry(self, type_id: int, type_info: dict):
        """
        Sync binary registry
        :param type_id: Complex object type ID.
        :param type_info: Complex object type info.
        """
        if type_info['type_exists']:
            for schema in type_info['schemas']:
                if not self._registry[type_id].get(schema_id(schema), None):
                    data_class = self._create_dataclass(
                        self._create_type_name(type_info['type_name']),
                        schema,
                    )
                    self._registry[type_id][schema_id(schema)] = data_class

    def _get_from_registry(self, type_id, schema):
        """
        Get binary type info from registry.

        :param type_id: Complex object type ID.
        :param schema: Complex object schema.
        """
        if schema:
            try:
                return self._registry[type_id][schema_id(schema)]
            except KeyError:
                return None
        return self._registry[type_id]


class Client(BaseClient):
    """
    This is a main `pyignite` class, that is build upon the
    :class:`~pyignite.connection.Connection`. In addition to the attributes,
    properties and methods of its parent class, `Client` implements
    the following features:

     * cache factory. Cache objects are used for key-value operations,
     * Ignite SQL endpoint,
     * binary types registration endpoint.
    """

    def __init__(self, compact_footer: bool = None, partition_aware: bool = False, **kwargs):
        """
        Initialize client.

        :param compact_footer: (optional) use compact (True, recommended) or
         full (False) schema approach when serializing Complex objects.
         Default is to use the same approach the server is using (None).
         Apache Ignite binary protocol documentation on this topic:
         https://apacheignite.readme.io/docs/binary-client-protocol-data-format#section-schema
        :param partition_aware: (optional) try to calculate the exact data
         placement from the key before to issue the key operation to the
         server node:
         https://cwiki.apache.org/confluence/display/IGNITE/IEP-23%3A+Best+Effort+Affinity+for+thin+clients
         The feature is in experimental status, so the parameter is `False`
         by default. This will be changed later.
        """
        super().__init__(compact_footer, partition_aware, **kwargs)

    def connect(self, *args):
        """
        Connect to Ignite cluster node(s).

        :param args: (optional) host(s) and port(s) to connect to.
        """
        if len(args) == 0:
            # no parameters − use default Ignite host and port
            nodes = [(IGNITE_DEFAULT_HOST, IGNITE_DEFAULT_PORT)]
        elif len(args) == 1 and is_iterable(args[0]):
            # iterable of host-port pairs is given
            nodes = args[0]
        elif len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], int):
            # host and port are given
            nodes = [args]
        else:
            raise ConnectionError('Connection parameters are not valid.')

        # the following code is quite twisted, because the protocol version
        # is initially unknown

        # TODO: open first node in foreground, others − in background
        for i, node in enumerate(nodes):
            host, port = node
            conn = Connection(self, **self._connection_args)
            conn.host = host
            conn.port = port

            try:
                if self.protocol_version is None or self.partition_aware:
                    # open connection before adding to the pool
                    conn.connect(host, port)

                    # now we have the protocol version
                    if not self.partition_aware:
                        # do not try to open more nodes
                        self._current_node = i

            except connection_errors:
                conn.failed = True
                if self.partition_aware:
                    # schedule the reconnection
                    conn.reconnect()

            self._nodes.append(conn)

        if self.protocol_version is None:
            raise ReconnectError('Can not connect.')

    def close(self):
        for conn in self._nodes:
            conn.close()
        self._nodes.clear()

    @property
    def random_node(self) -> Connection:
        """
        Returns random usable node.

        This method is not a part of the public API. Unless you wish to
        extend the `pyignite` capabilities (with additional testing, logging,
        examining connections, et c.) you probably should not use it.
        """
        if self.partition_aware:
            # if partition awareness is used just pick a random connected node
            return self._get_random_node()
        else:
            # if partition awareness is not used then just return the current
            # node if it's alive or the next usable node if connection with the
            # current is broken
            node = self._nodes[self._current_node]
            if node.alive:
                return node

            # close current (supposedly failed) node
            self._nodes[self._current_node].close()

            # advance the node index
            self._current_node += 1
            if self._current_node >= len(self._nodes):
                self._current_node = 0

            # prepare the list of node indexes to try to connect to
            num_nodes = len(self._nodes)
            for i in chain(range(self._current_node, num_nodes), range(self._current_node)):
                node = self._nodes[i]
                try:
                    node.connect(node.host, node.port)
                except connection_errors:
                    pass
                else:
                    return node

            # no nodes left
            raise ReconnectError('Can not reconnect: out of nodes.')

    def _get_random_node(self, reconnect=True):
        alive_nodes = [n for n in self._nodes if n.alive]
        if alive_nodes:
            return random.choice(alive_nodes)
        elif reconnect:
            for n in self._nodes:
                n.reconnect()

            return self._get_random_node(reconnect=False)
        else:
            # cannot choose from an empty sequence
            raise ReconnectError('Can not reconnect: out of nodes.') from None

    @status_to_exception(BinaryTypeError)
    def get_binary_type(self, binary_type: Union[str, int]) -> dict:
        """
        Gets the binary type information from the Ignite server. This is quite
        a low-level implementation of Ignite thin client protocol's
        `OP_GET_BINARY_TYPE` operation. You would probably want to use
        :py:meth:`~pyignite.client.Client.query_binary_type` instead.

        :param binary_type: binary type name or ID,
        :return: binary type description − a dict with the following fields:

         - `type_exists`: True if the type is registered, False otherwise. In
           the latter case all the following fields are omitted,
         - `type_id`: Complex object type ID,
         - `type_name`: Complex object type name,
         - `affinity_key_field`: string value or None,
         - `is_enum`: False in case of Complex object registration,
         - `schemas`: a list, containing the Complex object schemas in format:
           OrderedDict[field name: field type hint]. A schema can be empty.
        """
        result = get_binary_type(self.random_node, binary_type)
        return self._process_get_binary_type_result(result)

    @status_to_exception(BinaryTypeError)
    def put_binary_type(
        self, type_name: str, affinity_key_field: str = None,
        is_enum=False, schema: dict = None
    ):
        """
        Registers binary type information in cluster. Do not update binary
        registry. This is a literal implementation of Ignite thin client
        protocol's `OP_PUT_BINARY_TYPE` operation. You would probably want
        to use :py:meth:`~pyignite.client.Client.register_binary_type` instead.

        :param type_name: name of the data type being registered,
        :param affinity_key_field: (optional) name of the affinity key field,
        :param is_enum: (optional) register enum if True, binary object
         otherwise. Defaults to False,
        :param schema: (optional) when register enum, pass a dict
         of enumerated parameter names as keys and an integers as values.
         When register binary type, pass a dict of field names: field types.
         Binary type with no fields is OK.
        """
        return put_binary_type(self.random_node, type_name, affinity_key_field, is_enum, schema)

    def register_binary_type(
        self, data_class: Type, affinity_key_field: str = None,
    ):
        """
        Register the given class as a representation of a certain Complex
        object type. Discards autogenerated or previously registered class.

        :param data_class: Complex object class,
        :param affinity_key_field: (optional) affinity parameter.
        """
        if not self.query_binary_type(
            data_class.type_id, data_class.schema_id
        ):
            self.put_binary_type(
                data_class.type_name,
                affinity_key_field,
                schema=data_class.schema,
            )
        self._registry[data_class.type_id][data_class.schema_id] = data_class

    def query_binary_type(
        self, binary_type: Union[int, str], schema: Union[int, dict] = None,
        sync: bool = True
    ):
        """
        Queries the registry of Complex object classes.

        :param binary_type: Complex object type name or ID,
        :param schema: (optional) Complex object schema or schema ID,
        :param sync: (optional) look up the Ignite server for registered
         Complex objects and create data classes for them if needed,
        :return: found dataclass or None, if `schema` parameter is provided,
         a dict of {schema ID: dataclass} format otherwise.
        """
        type_id = entity_id(binary_type)

        result = self._get_from_registry(type_id, schema)
        if not result:
            type_info = self.get_binary_type(type_id)
            self._sync_binary_registry(type_id, type_info)
            return self._get_from_registry(type_id, schema)

        return result

    def unwrap_binary(self, value: Any) -> Any:
        """
        Detects and recursively unwraps Binary Object.

        :param value: anything that could be a Binary Object,
        :return: the result of the Binary Object unwrapping with all other data
         left intact.
        """
        if is_wrapped(value):
            blob, offset = value
            with BinaryStream(self, blob) as stream:
                data_class = BinaryObject.parse(stream)
                return BinaryObject.to_python(stream.read_ctype(data_class, direction=READ_BACKWARD), self)
        return value

    def create_cache(self, settings: Union[str, dict]) -> 'Cache':
        """
        Creates Ignite cache by name. Raises `CacheError` if such a cache is
        already exists.

        :param settings: cache name or dict of cache properties' codes
         and values. All cache properties are documented here:
         :ref:`cache_props`. See also the
         :ref:`cache creation example <sql_cache_create>`,
        :return: :class:`~pyignite.cache.Cache` object.
        """
        return create_cache(self, settings)

    def get_or_create_cache(self, settings: Union[str, dict]) -> 'Cache':
        """
        Creates Ignite cache, if not exist.

        :param settings: cache name or dict of cache properties' codes
         and values. All cache properties are documented here:
         :ref:`cache_props`. See also the
         :ref:`cache creation example <sql_cache_create>`,
        :return: :class:`~pyignite.cache.Cache` object.
        """
        return get_or_create_cache(self, settings)

    def get_cache(self, settings: Union[str, dict]) -> 'Cache':
        """
        Creates Cache object with a given cache name without checking it up
        on server. If such a cache does not exist, some kind of exception
        (most probably `CacheError`) may be raised later.

        :param settings: cache name or cache properties (but only `PROP_NAME`
         property is allowed),
        :return: :class:`~pyignite.cache.Cache` object.
        """
        return get_cache(self, settings)

    @status_to_exception(CacheError)
    def get_cache_names(self) -> list:
        """
        Gets existing cache names.

        :return: list of cache names.
        """
        return cache_get_names(self.random_node)

    def sql(
        self, query_str: str, page_size: int = 1024,
        query_args: Iterable = None, schema: str = 'PUBLIC',
        statement_type: int = 0, distributed_joins: bool = False,
        local: bool = False, replicated_only: bool = False,
        enforce_join_order: bool = False, collocated: bool = False,
        lazy: bool = False, include_field_names: bool = False,
        max_rows: int = -1, timeout: int = 0,
        cache: Union[int, str, Cache] = None
    ):
        """
        Runs an SQL query and returns its result.

        :param query_str: SQL query string,
        :param page_size: (optional) cursor page size. Default is 1024, which
         means that client makes one server call per 1024 rows,
        :param query_args: (optional) query arguments. List of values or
         (value, type hint) tuples,
        :param schema: (optional) schema for the query. Defaults to `PUBLIC`,
        :param statement_type: (optional) statement type. Can be:

         * StatementType.ALL − any type (default),
         * StatementType.SELECT − select,
         * StatementType.UPDATE − update.

        :param distributed_joins: (optional) distributed joins. Defaults
         to False,
        :param local: (optional) pass True if this query should be executed
         on local node only. Defaults to False,
        :param replicated_only: (optional) whether query contains only
         replicated tables or not. Defaults to False,
        :param enforce_join_order: (optional) enforce join order. Defaults
         to False,
        :param collocated: (optional) whether your data is co-located or not.
         Defaults to False,
        :param lazy: (optional) lazy query execution. Defaults to False,
        :param include_field_names: (optional) include field names in result.
         Defaults to False,
        :param max_rows: (optional) query-wide maximum of rows. Defaults to -1
         (all rows),
        :param timeout: (optional) non-negative timeout value in ms.
         Zero disables timeout (default),
        :param cache (optional) Name or ID of the cache to use to infer schema.
         If set, 'schema' argument is ignored,
        :return: generator with result rows as a lists. If
         `include_field_names` was set, the first row will hold field names.
        """

        c_id = cache.cache_id if isinstance(cache, Cache) else cache_id(cache)

        if c_id != 0:
            schema = None

        return SqlFieldsCursor(self, c_id, query_str, page_size, query_args, schema, statement_type, distributed_joins,
                               local, replicated_only, enforce_join_order, collocated, lazy, include_field_names,
                               max_rows, timeout)
