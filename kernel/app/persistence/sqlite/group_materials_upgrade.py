"""One-way upgrade: retire Project memory and isolate existing open Group sessions."""
import json
from uuid import uuid4


def upgrade(connection):
    # Preserve old user data only in a retired table with no repository/API. Empty
    # installations retain no Project memory table at all.
    count = connection.execute("SELECT COUNT(*) FROM shared_memory_records").fetchone()[0]
    if count:
        connection.execute("ALTER TABLE shared_memory_records RENAME TO retired_project_memory_records")
        connection.execute("DROP INDEX IF EXISTS shared_memory_by_project_type")
    else:
        connection.execute("DROP TABLE shared_memory_records")

    # Retire the generic capability from persisted discovery snapshots as well.
    def remove_retired_projection(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("capabilityProjection", "capability_projection") and isinstance(child, dict):
                    child.pop("memory", None)
                remove_retired_projection(child)
        elif isinstance(value, list):
            for child in value:
                remove_retired_projection(child)

    for backend in connection.execute("SELECT id,capabilities_json,capability_snapshot FROM backends").fetchall():
        for column in ("capabilities_json", "capability_snapshot"):
            raw = backend[column]
            if not raw:
                continue
            value = json.loads(raw)
            before = json.dumps(value)
            remove_retired_projection(value)
            after = json.dumps(value)
            if after != before:
                connection.execute(f"UPDATE backends SET {column}=? WHERE id=?", (after, backend["id"]))

    members = connection.execute(
        "SELECT m.* FROM collaboration_members m JOIN collaboration_sessions g "
        "ON g.id=m.collaboration_session_id WHERE g.status IN ('active','minimized')"
    ).fetchall()
    for member in members:
        old = connection.execute("SELECT * FROM conversations WHERE id=?", (member['conversation_id'],)).fetchone()
        if old is None or old['created_by_collaboration_id'] == member['collaboration_session_id']:
            continue
        row = dict(old)
        row.update(id='conversation:' + str(uuid4()), native_session_id=None,
                   native_session_head_id=None, native_session_segments_json='[]', preferred_surface='card',
                   state='idle', origin='group_spawned',
                   visibility='group_only', retention='persistent',
                   created_by_collaboration_id=member['collaboration_session_id'])
        keys = list(row)
        connection.execute(f"INSERT INTO conversations ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(row[k] for k in keys))
        connection.execute("UPDATE collaboration_members SET conversation_id=?,source_conversation_id=?,last_delivered_sequence=NULL WHERE id=?", (row['id'], old['id'], member['id']))
        # No old in-flight continuation may resume against a newly isolated session.
        connection.execute("UPDATE collaboration_sessions SET thread_json=NULL WHERE id=?", (member['collaboration_session_id'],))
