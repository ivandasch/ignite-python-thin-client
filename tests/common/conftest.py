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
import asyncio

import pytest

from pyignite import Client, AioClient
from tests.util import start_ignite_gen


@pytest.fixture(scope='module', autouse=True)
def server1():
    yield from start_ignite_gen(1)


@pytest.fixture(scope='module', autouse=True)
def server2():
    yield from start_ignite_gen(2)


@pytest.fixture(scope='module', autouse=True)
def server3():
    yield from start_ignite_gen(3)


@pytest.fixture(scope='module')
def client():
    client = Client()
    client.connect('127.0.0.1', 10801)
    yield client
    client.close()


@pytest.fixture(scope='module')
async def async_client(event_loop):
    client = AioClient()
    await client.connect('127.0.0.1', 10801)
    yield client
    await client.close()


@pytest.fixture
async def async_cache(async_client):
    cache = await async_client.create_cache('my_bucket')
    yield cache
    await cache.destroy()


@pytest.fixture
def cache(client):
    cache = client.create_cache('my_bucket')
    yield cache
    cache.destroy()
