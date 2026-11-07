from sqlalchemy import inspect, text

from prm.config import RuntimeEnv
from prm.db import init_db, make_engine, make_session_factory
from prm.models import Repo


def test_make_session_factory_from_runtime_env_creates_file_db(tmp_path):
    env = RuntimeEnv(db_path=str(tmp_path / "prm.db"))
    sf = make_session_factory(env)
    with sf() as s:
        s.add(Repo(full_name="acme/widgets"))
        s.commit()
    assert (tmp_path / "prm.db").exists()
    with make_session_factory(env.database_url)() as s:
        assert s.query(Repo).one().full_name == "acme/widgets"


def test_database_url_override_wins(tmp_path):
    env = RuntimeEnv(db_path="ignored.db", database_url_override=f"sqlite:///{tmp_path / 'x.db'}")
    make_session_factory(env)
    assert (tmp_path / "x.db").exists()
    assert not (tmp_path / "ignored.db").exists()


def test_all_plan_tables_exist():
    engine = make_engine("sqlite://")
    init_db(engine)
    init_db(engine)  # idempotent
    expected = {"repos", "traders", "markets", "beliefs", "trades", "positions", "events", "league_runs"}
    assert expected <= set(inspect(engine).get_table_names())


def test_sqlite_foreign_keys_are_on():
    engine = make_engine("sqlite://")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_in_memory_db_is_shared_across_sessions_and_threads():
    import threading

    sf = make_session_factory("sqlite://")
    with sf() as s:
        s.add(Repo(full_name="a/b"))
        s.commit()
    seen: list[int] = []

    def read() -> None:
        with sf() as s:
            seen.append(s.query(Repo).count())

    t = threading.Thread(target=read)
    t.start()
    t.join()
    assert seen == [1]


def test_make_session_factory_accepts_an_engine():
    engine = make_engine("sqlite://")
    sf = make_session_factory(engine)
    assert sf.kw["bind"] is engine
