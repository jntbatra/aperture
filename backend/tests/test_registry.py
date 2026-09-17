import pytest

from aperture.registry import Connection, Registry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("APERTURE_HOME_DIR", str(tmp_path))
    from aperture.config import settings

    settings.cache_clear()
    yield Registry.load()
    settings.cache_clear()


def test_adding_a_connection_makes_it_active(registry):
    registry.add(Connection(name="prod", url="sqlite:///prod.db"))
    assert Registry.load().active == "prod"


def test_passwords_are_never_displayed():
    connection = Connection(name="p", url="postgresql+psycopg://user:hunter2@host:5432/db")
    assert "hunter2" not in connection.safe_url
    assert "user" in connection.safe_url


def test_dialect_is_derived_from_the_url():
    assert Connection(name="c", url="sqlite:///x.db").dialect == "sqlite"


def test_switching_connections(registry):
    registry.add(Connection(name="a", url="sqlite:///a.db"))
    registry.add(Connection(name="b", url="sqlite:///b.db"))
    assert Registry.load().active == "b"
    Registry.load().use("a")
    assert Registry.load().active == "a"


def test_removing_the_active_connection_falls_back(registry):
    registry.add(Connection(name="a", url="sqlite:///a.db"))
    registry.add(Connection(name="b", url="sqlite:///b.db"))
    Registry.load().remove("b")
    assert Registry.load().active == "a"


def test_using_an_unknown_name_returns_nothing(registry):
    assert Registry.load().use("nope") is None


def test_unreadable_registry_is_ignored_not_fatal(registry):
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("{not json")
    assert Registry.load().connections == {}
