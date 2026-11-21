import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://planner:planner@db:5432/planner")
app = FastAPI()


class RoomIn(BaseModel):
    capacity: int = Field(gt=0)
    equipment: list[str] = []


class SearchIn(BaseModel):
    from_time: datetime
    to_time: datetime
    duration_minutes: int = Field(gt=0)
    participants: list[str] = []
    min_capacity: int = Field(ge=0)
    equipment: list[str] = []


class MeetingIn(BaseModel):
    room_id: str
    start: datetime
    duration_minutes: int = Field(gt=0)
    participants: list[str] = []
    occurrences: int = Field(default=1, ge=1, le=8)


class MoveIn(BaseModel):
    start: datetime
    duration_minutes: int = Field(gt=0)
    expected_version: int = Field(ge=1)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "date must contain UTC offset")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def setup():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS rooms (id text PRIMARY KEY, capacity integer NOT NULL, equipment text[] NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS meetings (
            id uuid PRIMARY KEY, room_id text NOT NULL REFERENCES rooms(id),
            start_at timestamp NOT NULL, end_at timestamp NOT NULL,
            participants text[] NOT NULL, series_id uuid, version integer NOT NULL DEFAULT 1
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS meetings_room_time ON meetings(room_id, start_at, end_at)")
        con.execute("CREATE INDEX IF NOT EXISTS rooms_equipment_idx ON rooms USING GIN (equipment)")


@app.on_event("startup")
def startup():
    setup()


@app.get("/health")
def health():
    try:
        with db() as con:
            con.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "database unavailable")


@app.post("/rooms", status_code=201)
def create_room(room: RoomIn):
    room_id = str(uuid.uuid4())
    try:
        with db() as con:
            row = con.execute(
                "INSERT INTO rooms (id, capacity, equipment) VALUES (%s, %s, %s) RETURNING id, capacity, equipment",
                (room_id, room.capacity, room.equipment),
            ).fetchone()
        return row
    except psycopg.Error:
        raise HTTPException(503, "database unavailable")


def busy(con, room_id, participants, start, end, exclude=None):
    sql = "SELECT id FROM meetings WHERE (room_id = %s OR participants && %s) AND start_at < %s AND end_at > %s"
    args = [room_id, participants, end, start]
    if exclude:
        sql += " AND id != %s"
        args.append(exclude)
    return con.execute(sql, args).fetchone()


@app.post("/search")
def search(data: SearchIn):
    start = utc(data.from_time)
    finish = utc(data.to_time)
    if finish <= start:
        raise HTTPException(422, "invalid range")
    step = timedelta(minutes=15)
    first = start.replace(second=0, microsecond=0)
    if first < start:
        first += timedelta(minutes=1)
    minutes = first.hour * 60 + first.minute
    first += timedelta(minutes=(15 - minutes % 15) % 15)
    length = timedelta(minutes=data.duration_minutes)
    result = []
    try:
        with db() as con:
            rooms = con.execute(
                """
                SELECT id, capacity, equipment
                FROM rooms
                WHERE capacity >= %s
                  AND equipment @> %s::text[]
                ORDER BY capacity, id
                """,
                (data.min_capacity, data.equipment),
            ).fetchall()
            current = first
            while current + length <= finish and len(result) < 5:
                for room in rooms:
                    if not busy(con, room["id"], data.participants, current, current + length):
                        result.append({"room_id": room["id"], "start": current.isoformat() + "Z", "end": (current + length).isoformat() + "Z"})
                        if len(result) == 5:
                            break
                current += step
    except psycopg.Error:
        raise HTTPException(503, "database unavailable")
    return result


def locks(room_id, participants):
    return sorted(set(["room:" + room_id] + ["user:" + x for x in participants]))


def lock_resources(con, room_id, participants):
    for key in locks(room_id, participants):
        con.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def meeting_json(row):
    return {"id": str(row["id"]), "room_id": row["room_id"], "start": row["start_at"].isoformat() + "Z", "end": row["end_at"].isoformat() + "Z", "participants": row["participants"], "version": row["version"]}


@app.post("/meetings", status_code=201)
def create_meeting(data: MeetingIn):
    start = utc(data.start)
    length = timedelta(minutes=data.duration_minutes)
    participants = sorted(set(data.participants))
    series = uuid.uuid4() if data.occurrences > 1 else None
    try:
        with db() as con:
            lock_resources(con, data.room_id, participants)
            room = con.execute("SELECT id FROM rooms WHERE id=%s", (data.room_id,)).fetchone()
            if not room:
                raise HTTPException(404, "room not found")
            starts = [start + timedelta(days=7 * n) for n in range(data.occurrences)]
            for item in starts:
                conflict = busy(con, data.room_id, participants, item, item + length)
                if conflict:
                    raise HTTPException(409, {"conflict_start": item.isoformat() + "Z"})
            rows = []
            for item in starts:
                row = con.execute("INSERT INTO meetings VALUES (%s,%s,%s,%s,%s,%s,1) RETURNING id,room_id,start_at,end_at,participants,version", (uuid.uuid4(), data.room_id, item, item + length, participants, series)).fetchone()
                rows.append(meeting_json(row))
            return {"series_id": str(series) if series else None, "meetings": rows}
    except HTTPException:
        raise
    except psycopg.Error:
        raise HTTPException(503, "database unavailable")


@app.put("/meetings/{meeting_id}")
def move_meeting(meeting_id: uuid.UUID, data: MoveIn):
    start = utc(data.start)
    try:
        with db() as con:
            row = con.execute("SELECT id,room_id,start_at,end_at,participants,version FROM meetings WHERE id=%s FOR UPDATE", (meeting_id,)).fetchone()
            if not row:
                raise HTTPException(404, "meeting not found")
            if row["version"] != data.expected_version:
                raise HTTPException(409, "version conflict")
            lock_resources(con, row["room_id"], row["participants"])
            end = start + timedelta(minutes=data.duration_minutes)
            if busy(con, row["room_id"], row["participants"], start, end, str(meeting_id)):
                raise HTTPException(409, {"conflict_start": start.isoformat() + "Z"})
            updated = con.execute("UPDATE meetings SET start_at=%s,end_at=%s,version=version+1 WHERE id=%s RETURNING id,room_id,start_at,end_at,participants,version", (start, end, meeting_id)).fetchone()
            return meeting_json(updated)
    except HTTPException:
        raise
    except psycopg.Error:
        raise HTTPException(503, "database unavailable")


@app.delete("/meetings/{meeting_id}", status_code=204)
def cancel_meeting(meeting_id: uuid.UUID):
    try:
        with db() as con:
            if con.execute("DELETE FROM meetings WHERE id=%s", (meeting_id,)).rowcount == 0:
                raise HTTPException(404, "meeting not found")
    except HTTPException:
        raise
    except psycopg.Error:
        raise HTTPException(503, "database unavailable")
