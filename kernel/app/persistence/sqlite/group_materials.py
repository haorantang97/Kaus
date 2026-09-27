"""Atomic compare-and-swap for a group's selected reference material."""
import json
from app.collaboration.materials import GroupMaterials, MaterialConflict
from app.persistence.sqlite.database import SqliteDatabase


class SqliteGroupMaterialsRepository:
    def __init__(self, database: SqliteDatabase):
        self._db = database

    async def get(self, group_id: str) -> GroupMaterials:
        row = self._db.query_one("SELECT * FROM group_materials WHERE group_id = ?", (group_id,))
        if row is None:
            return GroupMaterials(group_id=group_id)
        return GroupMaterials(group_id=group_id, revision=row["revision"],
                              items=json.loads(row["items_json"]))

    async def save(self, bundle: GroupMaterials, *, expected_revision: int) -> GroupMaterials:
        saved = bundle.evolve(revision=expected_revision + 1)
        payload = json.dumps([x.model_dump(mode="json") for x in saved.items], ensure_ascii=False)
        with self._db.transaction() as connection:
            row = connection.execute("SELECT revision FROM group_materials WHERE group_id = ?", (bundle.group_id,)).fetchone()
            if (row["revision"] if row else 0) != expected_revision:
                raise MaterialConflict("资料已更新，请刷新后重试")
            connection.execute(
                "INSERT INTO group_materials(group_id, revision, items_json) VALUES (?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET revision=excluded.revision, items_json=excluded.items_json",
                (bundle.group_id, saved.revision, payload),
            )
        return saved
