import argparse
import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import pandas as pd
from aiolimiter import AsyncLimiter
from aiovk import ImplicitSession, API, TokenSession
from aiovk.drivers import HttpDriver
from aiovk.exceptions import VkAPIError, VkAuthError

APP_ID = 51755826

users = {}

G = nx.Graph()


@dataclass
class Settings:
    username: str
    app_id: int
    graph_file: Path
    user_file: Path
    creds_file: Path
    depth: int
    data_csv_file: Path
    no_cache: bool
    skip_users: list[int]


def parse_args() -> Settings:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("-A", "--app-id", dest="app_id", help="app id", default=APP_ID, type=int)
    parser.add_argument("-G", "--graph-file", dest="graph_file", help="file to save(in json) graph",
                        default="graph.json", type=Path)
    parser.add_argument("-U", "--user-file", dest="user_file", help="file to save users(in json)", default="users.json",
                        type=Path)
    parser.add_argument("-C", "--creds-file", dest="creds_file", help="where to save credentials(json)",
                        default="creds.json", type=Path)
    parser.add_argument("-D", "--depth", help="depth of parsing", default=2, type=int)
    parser.add_argument("-d", "--data-csv-file", dest="data_csv_file", help="data to process(csv)", default="data.csv",
                        type=Path)
    parser.add_argument("-N", "--no-cache", dest="no_cache",
                        help="not use cache in already existing files(may be long operation)",
                        default=False, action="store_true")
    parser.add_argument("-S", "--skip-user", dest="skip_users", type=int, action="append",
                        help="users that manually parsed and put in graph-file already")
    return Settings(**vars(parser.parse_args()))


class Implicit2AFSession(ImplicitSession):

    async def enter_confirmation_code(self) -> str:
        return str(input("Code: "))


class LimiterHttpDriver(HttpDriver):

    def __init__(self, timeout=10, loop=None, session=None, limiter=None):
        super().__init__(timeout, loop, session)
        self._limiter = limiter or AsyncLimiter(max_rate=1., time_period=1.)

    async def post_json(self, url, params, headers=None, timeout=None):
        async with self._limiter:
            return await super().post_json(url, params, headers, timeout)

    async def get_bin(self, url, params, headers=None, timeout=None):
        async with self._limiter:
            return await super().get_bin(url, params, headers, timeout)

    async def get_text(self, url, params, headers=None, timeout=None):
        async with self._limiter:
            return await super().get_text(url, params, headers, timeout)

    async def post_text(self, url, data, headers=None, timeout=None):
        async with self._limiter:
            return await super().post_text(url, data, headers, timeout)


async def parse_user(api: API, skip_users: set[int], user_id: int | str, depth: int = 0, max_depth: int = 0):
    if depth >= max_depth:
        return
    if user_id in skip_users and depth == 0:
        friends = list(G.neighbors(user_id))
    else:
        try:
            friends = (await api.friends.get(user_id=user_id, order="random"))["items"]
        except VkAPIError as exc:
            if exc.error_code not in {15, 18}:
                raise
            print("skip", user_id, "exc:", "code:", exc.error_code, "msg:", exc.error_msg)

            return
    tasks = set()
    friends_info = await api.users.get(
        user_ids=",".join(map(str, friends)),
    )
    for friend_info in friends_info:
        G.add_edge(user_id, friend_info["id"])
        users[friend_info["id"]] = friend_info.copy()

    for friend_id in friends:
        tasks.add(asyncio.create_task(parse_user(api, friend_id, depth=depth + 1, max_depth=max_depth)))

    await asyncio.wait(tasks)


def save_token(settings: Settings, username: str, token: str):
    if settings.creds_file.exists():
        with settings.creds_file.open("r") as f:
            prev = json.load(f)
    else:
        prev = {}

    prev[username]["access_token"] = token
    try:
        with settings.creds_file.open("w") as f:
            json.dump(prev, f)
    except OSError:
        return False
    return True


def get_token(settings: Settings, username: str):
    try:
        with settings.creds_file.open("r") as f:
            return json.load(f)[username]["access_token"]
    except (OSError, KeyError):
        return None


def get_password(settings: Settings, username: str):
    try:
        with settings.creds_file.open("r") as f:
            return json.load(f)[username]["password"]
    except (OSError, KeyError):
        return None


@contextlib.asynccontextmanager
async def login(settings: Settings):
    token = get_token(settings, settings.username)
    for _ in range(2):
        driver = LimiterHttpDriver(limiter=AsyncLimiter(max_rate=2., time_period=1.))
        if token is None:
            password = get_password(settings, settings.username)
            if password is None:
                raise ValueError("password is required")
            async with Implicit2AFSession(settings.username, password, APP_ID, ["friends"], driver=driver) as session:
                session: Implicit2AFSession
                await session.authorize()
                save_token(settings, settings.username, session.access_token)
                yield session
        else:
            async with TokenSession(access_token=token, driver=driver) as session:
                try:
                    await API(session).users.get()
                except VkAuthError as exc:
                    print(exc)
                    token = None
                    continue
                yield session
        break


async def resolve_all_names(api: API, names: list[str]) -> list[int]:
    async def resolve_name(name: str) -> int:
        return (await api.utils.resolveScreenName(screen_name=name))['object_id']

    return list(await asyncio.gather(*(resolve_name(name) for name in names)))


async def main():
    settings = parse_args()

    df = pd.read_csv(settings.data_csv_file, header=0, index_col=None)
    df = df.dropna()

    if settings.user_file.exists() and not settings.no_cache:
        with settings.user_file.open("r") as f:
            global users
            users = json.load(f)
    if settings.graph_file.exists() and not settings.no_cache:
        with settings.graph_file.open("r") as f:
            global G
            G = nx.node_link_graph(json.load(f))

    try:
        vkids = []
        for _, (name, vkid) in df[["name", "id"]].iterrows():
            vkids.append(vkid)

        async with login(settings) as session:
            api = API(session)
            if "vkid" not in df.columns:
                true_ids = await resolve_all_names(api, vkids)
                df["vkid"] = pd.Series(true_ids, dtype=int)
                df.to_csv(settings.data_csv_file, index=False)
            else:
                true_ids = df["vkid"].astype(int).tolist()
            await asyncio.gather(
                *(asyncio.create_task(parse_user(api, set(settings.skip_users), id_, depth=0, max_depth=settings.depth))
                  for id_ in true_ids))
    finally:
        with settings.user_file.open("w") as f1, settings.graph_file.open("w") as f2:
            json.dump(users, f1)
            json.dump(nx.node_link_data(G), f2)


if __name__ == '__main__':
    asyncio.run(main())
