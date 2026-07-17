-- SQLite schema for the lightweight Kanban application.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'done')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_updated_at
    ON tasks (status, updated_at DESC);

CREATE TRIGGER IF NOT EXISTS tasks_set_updated_at
AFTER UPDATE ON tasks
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE tasks
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id;
END;
