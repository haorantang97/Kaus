from app.persistence.sqlite.codec import dump_json, load_mapping


class SqliteAppSettingsRepository:
    def __init__(self, database):
        self._db = database

    async def get(self, key):
        row = self._db.query_one("SELECT value_json FROM app_settings WHERE key = ?", (key,))
        return load_mapping(row["value_json"]) if row is not None else None

    async def save(self, key, value):
        self._db.run("INSERT INTO app_settings(key, value_json) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json", (key, dump_json(value)))
