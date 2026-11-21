import os
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", "postgresql://planner:planner@localhost:5433/planner_test")
from app import app


@pytest.fixture(autouse=True)
def clean():
    with psycopg.connect(os.environ["DATABASE_URL"]) as con:
        con.execute("DROP TABLE IF EXISTS meetings, rooms CASCADE")
    with TestClient(app):
        yield
    with psycopg.connect(os.environ["DATABASE_URL"]) as con:
        con.execute("DROP TABLE IF EXISTS meetings, rooms CASCADE")


def room(c):
    response = c.post("/rooms", json={"capacity": 5, "equipment": ["screen"]})
    assert response.status_code == 201
    return response.json()["id"]


def meeting(c, room_id, person="p", start="2026-01-05T10:00:00+00:00"):
    return c.post("/meetings", json={"room_id": room_id, "start": start, "duration_minutes": 60, "participants": [person]} )


def test_room_race_and_participant_race():
    with TestClient(app) as c:
        room_id = room(c)
        def book_same_room():
            with TestClient(app) as client:
                return meeting(client, room_id).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: book_same_room(), range(2)))
        assert sorted(statuses) == [201, 409]
        second_room_id = room(c)
        def book_same_person(room_id):
            with TestClient(app) as client:
                return meeting(client, room_id, start="2026-01-06T10:00:00+00:00").status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(book_same_person, [room_id, second_room_id]))
        assert sorted(statuses) == [201, 409]


def test_series_is_atomic():
    with TestClient(app) as c:
        room_id = room(c)
        assert c.post("/meetings", json={"room_id": room_id, "start": "2026-01-12T10:00:00+00:00", "duration_minutes": 60, "participants": ["x"]}).status_code == 201
        result = c.post("/meetings", json={"room_id": room_id, "start": "2026-01-05T10:00:00+00:00", "duration_minutes": 60, "participants": ["y"], "occurrences": 2})
        assert result.status_code == 409


def test_search_then_create_and_move_version():
    with TestClient(app) as c:
        room_id = room(c)
        search = c.post("/search", json={"from_time": "2026-01-05T10:00:00+00:00", "to_time": "2026-01-05T12:00:00+00:00", "duration_minutes": 60, "participants": [], "min_capacity": 1, "equipment": []})
        assert search.status_code == 200
        found = search.json()[0]
        assert meeting(c, room_id, start=found["start"]).status_code == 201
        assert meeting(c, room_id, person="q", start=found["start"]).status_code == 409
        created = meeting(c, room_id, person="q", start="2026-01-05T13:00:00+00:00").json()["meetings"][0]
        move = c.put("/meetings/" + created["id"], json={"start": "2026-01-05T10:30:00+00:00", "duration_minutes": 60, "expected_version": 1})
        assert move.status_code == 409
        assert c.put("/meetings/" + created["id"], json={"start": "2026-01-05T12:00:00+00:00", "duration_minutes": 60, "expected_version": 1}).status_code == 200
        assert c.put("/meetings/" + created["id"], json={"start": "2026-01-05T13:00:00+00:00", "duration_minutes": 60, "expected_version": 1}).status_code == 409


def test_offsets_and_touching_meetings():
    with TestClient(app) as c:
        room_id = room(c)
        assert meeting(c, room_id, start="2026-01-05T12:00:00+02:00").status_code == 201
        assert meeting(c, room_id, person="q", start="2026-01-05T10:00:00Z").status_code == 409
        assert meeting(c, room_id, person="r", start="2026-01-05T11:00:00Z").status_code == 201
