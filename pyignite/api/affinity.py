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

from typing import Iterable, Union

from pyignite.datatypes import Bool, Int, Long, UUIDObject
from pyignite.datatypes.internal import StructArray, Conditional, Struct
from pyignite.queries import Query
from pyignite.queries.op_codes import OP_CACHE_PARTITIONS
from pyignite.utils import is_iterable
from .result import APIResult


cache_ids = StructArray([
    ('cache_id', Int),
])

cache_config = StructArray([
    ('key_type_id', Int),
    ('affinity_key_field_id', Int),
])

node_partitions = StructArray([
    ('partition_id', Int),
])

node_mapping = StructArray([
    ('node_uuid', UUIDObject),
    ('node_partitions', node_partitions)
])

cache_mapping = StructArray([
    ('cache_id', Int),
    ('cache_config', cache_config),
])

empty_cache_mapping = StructArray([
    ('cache_id', Int)
])

empty_node_mapping = Struct([])

partition_mapping = StructArray([
    ('is_applicable', Bool),

    ('cache_mapping', Conditional(lambda ctx: ctx['is_applicable'] and ctx['is_applicable'].value == 1,
                                  lambda ctx: ctx['is_applicable'],
                                  cache_mapping, empty_cache_mapping)),

    ('node_mapping', Conditional(lambda ctx: ctx['is_applicable'] and ctx['is_applicable'].value == 1,
                                 lambda ctx: ctx['is_applicable'],
                                 node_mapping, empty_node_mapping)),
])


def cache_get_node_partitions(
    conn: 'Connection', caches: Union[int, Iterable[int]],
    query_id: int = None,
) -> APIResult:
    """
    Gets partition mapping for an Ignite cache or a number of caches. See
    “IEP-23: Best Effort Affinity for thin clients”.

    :param conn: connection to Ignite server,
    :param caches: cache ID(s) the mapping is provided for,
    :param query_id: (optional) a value generated by client and returned as-is
     in response.query_id. When the parameter is omitted, a random value
     is generated,
    :return: API result data object.
    """
    query_struct = Query(
        OP_CACHE_PARTITIONS,
        [
            ('cache_ids', cache_ids),
        ],
        query_id=query_id
    )
    if not is_iterable(caches):
        caches = [caches]

    result = query_struct.perform(
        conn,
        query_params={
            'cache_ids': [{'cache_id': cache} for cache in caches],
        },
        response_config=[
            ('version_major', Long),
            ('version_minor', Int),
            ('partition_mapping', partition_mapping),
        ],
    )
    if result.status == 0:
        # tidying up the result
        value = {
            'version': (
                result.value['version_major'],
                result.value['version_minor']
            ),
            'partition_mapping': [],
        }
        for i, partition_map in enumerate(result.value['partition_mapping']):
            cache_id = partition_map['cache_mapping'][0]['cache_id']
            value['partition_mapping'].insert(
                i,
                {
                    'cache_id': cache_id,
                    'is_applicable': partition_map['is_applicable'],
                }
            )
            if partition_map['is_applicable']:
                value['partition_mapping'][i]['cache_config'] = {
                    a['key_type_id']: a['affinity_key_field_id']
                    for a in partition_map['cache_mapping'][0]['cache_config']
                }
                value['partition_mapping'][i]['node_mapping'] = {
                    p['node_uuid']: [
                        x['partition_id'] for x in p['node_partitions']
                    ]
                    for p in partition_map['node_mapping']
                }
        result.value = value

    return result
