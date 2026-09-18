"""数据模型 — 封装数据库 CRUD 操作"""
import json
from datetime import datetime
from .database import get_db
from .crypto import encrypt, decrypt


class PluginModel:
    @staticmethod
    def get_all():
        db = get_db()
        rows = db.execute("SELECT * FROM plugins WHERE enabled=1 ORDER BY id").fetchall()
        db.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_name(name):
        db = get_db()
        row = db.execute("SELECT * FROM plugins WHERE name=?", (name,)).fetchone()
        db.close()
        return dict(row) if row else None

    @staticmethod
    def get_by_id(pid):
        db = get_db()
        row = db.execute("SELECT * FROM plugins WHERE id=?", (pid,)).fetchone()
        db.close()
        return dict(row) if row else None

    @staticmethod
    def create(data):
        db = get_db()
        db.execute(
            "INSERT INTO plugins (name, display_name, description, version, plugin_type, author, builtin, form_schema) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (data["name"], data["display_name"], data.get("description", ""),
             data.get("version", "1.0"), data.get("plugin_type", "http"),
             data.get("author", ""), data.get("builtin", 0),
             json.dumps(data.get("form_schema", []), ensure_ascii=False)))
        db.commit()
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return pid

    @staticmethod
    def delete(pid):
        db = get_db()
        db.execute("DELETE FROM plugins WHERE id=?", (pid,))
        db.commit()
        db.close()


class TaskModel:
    @staticmethod
    def get_all():
        db = get_db()
        rows = db.execute(
            "SELECT t.*, p.display_name as plugin_display_name, p.plugin_type "
            "FROM tasks t LEFT JOIN plugins p ON t.plugin_id=p.id "
            "ORDER BY t.id"
        ).fetchall()
        db.close()
        results = []
        for r in rows:
            d = dict(r)
            d["password"] = decrypt(d["password"])
            d["cookie"] = decrypt(d["cookie"])
            d["params"] = json.loads(d["params"])
            d["last_extra"] = json.loads(d["last_extra"])
            results.append(d)
        return results

    @staticmethod
    def get_by_id(tid):
        db = get_db()
        row = db.execute(
            "SELECT t.*, p.display_name as plugin_display_name, p.plugin_type, p.form_schema "
            "FROM tasks t LEFT JOIN plugins p ON t.plugin_id=p.id "
            "WHERE t.id=?", (tid,)
        ).fetchone()
        db.close()
        if not row:
            return None
        d = dict(row)
        d["password"] = decrypt(d["password"])
        d["cookie"] = decrypt(d["cookie"])
        d["params"] = json.loads(d["params"])
        d["last_extra"] = json.loads(d["last_extra"])
        return d

    @staticmethod
    def create(data):
        db = get_db()
        password = encrypt(data.get("password", ""))
        cookie = encrypt(data.get("cookie", ""))
        params = json.dumps(data.get("params", {}), ensure_ascii=False)
        db.execute(
            "INSERT INTO tasks (plugin_id, name, site_url, username, password, cookie, "
            "cron_expr, enabled, params) VALUES (?,?,?,?,?,?,?,?,?)",
            (data["plugin_id"], data["name"], data.get("site_url", ""),
             data.get("username", ""), password, cookie,
             data.get("cron_expr", "0 9 * * *"), data.get("enabled", 1), params))
        db.commit()
        tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return tid

    @staticmethod
    def update(tid, data):
        db = get_db()
        fields = []
        values = []
        for k in ["name", "site_url", "username", "cron_expr", "enabled", "plugin_id", "max_fail_alert"]:
            if k in data:
                fields.append(f"{k}=?")
                values.append(data[k])
        if "password" in data:
            fields.append("password=?")
            values.append(encrypt(data["password"]))
        if "cookie" in data:
            fields.append("cookie=?")
            values.append(encrypt(data["cookie"]))
        if "params" in data:
            fields.append("params=?")
            values.append(json.dumps(data["params"], ensure_ascii=False))
        if "status" in data:
            fields.append("status=?")
            values.append(data["status"])
            if "status_message" in data:
                fields.append("status_message=?")
                values.append(data["status_message"])
        fields.append("updated_at=datetime('now','localtime')")
        values.append(tid)
        db.execute(f"UPDATE tasks SET {','.join(fields)} WHERE id=?", values)
        db.commit()
        db.close()

    @staticmethod
    def update_run_result(tid, result, message, extra, duration_ms=0, error_trace=""):
        db = get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = datetime.now().strftime("%Y-%m-%d")

        new_status = "normal" if result == "success" else "error"
        db.execute(
            "UPDATE tasks SET last_result=?, last_message=?, last_extra=?, last_run_at=?, "
            "status=?, status_message=CASE WHEN ?='success' THEN '' ELSE ? END, "
            "consecutive_fail=CASE WHEN ?='success' THEN 0 ELSE consecutive_fail+1 END, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (result, message, json.dumps(extra, ensure_ascii=False), now,
             new_status, result, message, result, tid))

        # 同一天同一任务只保留一条记录，更新而非新增
        existing = db.execute(
            "SELECT id FROM checkin_logs WHERE task_id=? AND checkin_date=?",
            (tid, today)
        ).fetchone()

        if existing:
            db.execute(
                "UPDATE checkin_logs SET checkin_time=?, result=?, message=?, extra_info=?, "
                "duration_ms=?, error_trace=?, created_at=datetime('now','localtime') WHERE id=?",
                (now.split()[1] if " " in now else "", result, message,
                 json.dumps(extra, ensure_ascii=False), duration_ms, error_trace, existing["id"]))
        else:
            db.execute(
                "INSERT INTO checkin_logs (task_id, checkin_date, checkin_time, result, message, extra_info, duration_ms, error_trace) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tid, today, now.split()[1] if " " in now else "", result, message,
                 json.dumps(extra, ensure_ascii=False), duration_ms, error_trace))
        db.commit()
        db.close()

    @staticmethod
    def delete(tid):
        """删除任务及其从属记录。

        ⚠️ 必须**先删子表再删主表**。数据库开了 `PRAGMA foreign_keys=ON`，
        而 `checkin_logs.task_id` / `task_notify.task_id` 的外键都没写
        `ON DELETE CASCADE` —— 直接删 tasks 会抛
        `sqlite3.IntegrityError: FOREIGN KEY constraint failed`。
        症状是「只要这个任务跑过一次，就再也删不掉」，且接口只回 500 不带原因，
        从界面完全看不出问题。改动前务必保留这里的删除顺序。
        """
        db = get_db()
        try:
            db.execute("DELETE FROM checkin_logs WHERE task_id=?", (tid,))
            db.execute("DELETE FROM task_notify WHERE task_id=?", (tid,))
            db.execute("DELETE FROM tasks WHERE id=?", (tid,))
            db.commit()
        finally:
            db.close()

    @staticmethod
    def update_cookie(tid, cookie):
        """签到登录成功后回写最新 Cookie（加密存储），下次直接复用"""
        db = get_db()
        db.execute(
            "UPDATE tasks SET cookie=?, updated_at=datetime('now','localtime') WHERE id=?",
            (encrypt(cookie), tid))
        db.commit()
        db.close()


class CheckinLogModel:
    @staticmethod
    def get_by_task(tid, limit=365):
        db = get_db()
        rows = db.execute(
            "SELECT * FROM checkin_logs WHERE task_id=? ORDER BY checkin_date DESC LIMIT ?",
            (tid, limit)
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_calendar(tid, year=None, month=None):
        """返回某年某月的签到日历数据"""
        if not year:
            year = datetime.now().year
        if not month:
            month = datetime.now().month
        db = get_db()
        rows = db.execute(
            "SELECT checkin_date, result FROM checkin_logs "
            "WHERE task_id=? AND checkin_date LIKE ? "
            "ORDER BY checkin_date",
            (tid, f"{year}-{month:02d}-%")
        ).fetchall()
        db.close()
        return {r["checkin_date"]: r["result"] for r in rows}


class HarTemplateModel:
    """HAR 通用签到模板 CRUD

    entries / variables 在库里是 JSON 文本，出库时自动反序列化成 Python 对象 ——
    调用方拿到的永远是 **已解析** 的结构，不要再 json.loads 一次。
    """

    @staticmethod
    def get_all():
        db = get_db()
        rows = db.execute("SELECT * FROM har_templates ORDER BY id").fetchall()
        db.close()
        return [HarTemplateModel._decode(dict(r)) for r in rows]

    @staticmethod
    def get_by_id(tid):
        db = get_db()
        row = db.execute("SELECT * FROM har_templates WHERE id=?", (tid,)).fetchone()
        db.close()
        return HarTemplateModel._decode(dict(row)) if row else None

    @staticmethod
    def _decode(row):
        for key in ("entries", "variables"):
            try:
                row[key] = json.loads(row.get(key) or "[]")
            except (json.JSONDecodeError, TypeError):
                row[key] = []
        return row

    @staticmethod
    def create(data):
        db = get_db()
        db.execute(
            "INSERT INTO har_templates (name, host, note, entries, variables, builtin) "
            "VALUES (?,?,?,?,?,?)",
            (data.get("name") or "未命名模板",
             data.get("host", ""),
             data.get("note", ""),
             json.dumps(data.get("entries", []), ensure_ascii=False),
             json.dumps(data.get("variables", []), ensure_ascii=False),
             int(data.get("builtin", 0))))
        db.commit()
        tid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return tid

    @staticmethod
    def update(tid, data):
        db = get_db()
        fields, values = [], []
        for k in ("name", "host", "note"):
            if k in data:
                fields.append(f"{k}=?")
                values.append(data[k])
        for k in ("entries", "variables"):
            if k in data:
                fields.append(f"{k}=?")
                values.append(json.dumps(data[k], ensure_ascii=False))
        if not fields:
            db.close()
            return
        fields.append("updated_at=datetime('now','localtime')")
        values.append(tid)
        db.execute(f"UPDATE har_templates SET {','.join(fields)} WHERE id=?", values)
        db.commit()
        db.close()

    @staticmethod
    def delete(tid):
        db = get_db()
        db.execute("DELETE FROM har_templates WHERE id=?", (tid,))
        db.commit()
        db.close()

    @staticmethod
    def count_tasks_using(tid):
        """有多少任务正在用这个模板（删除前提醒用）"""
        db = get_db()
        rows = db.execute("SELECT id, name, params FROM tasks").fetchall()
        db.close()
        used = []
        for r in rows:
            try:
                params = json.loads(r["params"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if str(params.get("template_id")) == str(tid):
                used.append({"id": r["id"], "name": r["name"]})
        return used


class NotifyModel:
    @staticmethod
    def get_all():
        db = get_db()
        rows = db.execute("SELECT * FROM notify_configs ORDER BY id").fetchall()
        db.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_enabled():
        db = get_db()
        rows = db.execute("SELECT * FROM notify_configs WHERE enabled=1").fetchall()
        db.close()
        return [dict(r) for r in rows]

    @staticmethod
    def create(data):
        db = get_db()
        db.execute(
            "INSERT INTO notify_configs (notify_type, name, config, enabled) VALUES (?,?,?,?)",
            (data["notify_type"], data.get("name", ""),
             json.dumps(data.get("config", {}), ensure_ascii=False),
             data.get("enabled", 1)))
        db.commit()
        nid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return nid

    @staticmethod
    def update(nid, data):
        db = get_db()
        fields = []
        values = []
        for k in ["notify_type", "name", "enabled"]:
            if k in data:
                fields.append(f"{k}=?")
                values.append(data[k])
        if "config" in data:
            fields.append("config=?")
            values.append(json.dumps(data["config"], ensure_ascii=False))
        values.append(nid)
        db.execute(f"UPDATE notify_configs SET {','.join(fields)} WHERE id=?", values)
        db.commit()
        db.close()

    @staticmethod
    def delete(nid):
        db = get_db()
        db.execute("DELETE FROM notify_configs WHERE id=?", (nid,))
        db.commit()
        db.close()

    @staticmethod
    def get_for_task(tid):
        db = get_db()
        rows = db.execute(
            "SELECT nc.*, tn.on_success, tn.on_failure FROM task_notify tn "
            "JOIN notify_configs nc ON tn.notify_id=nc.id "
            "WHERE tn.task_id=? AND nc.enabled=1", (tid,)
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    @staticmethod
    def set_task_notify(tid, notify_ids, on_success=True, on_failure=True):
        db = get_db()
        db.execute("DELETE FROM task_notify WHERE task_id=?", (tid,))
        for nid in notify_ids:
            db.execute(
                "INSERT INTO task_notify (task_id, notify_id, on_success, on_failure) VALUES (?,?,?,?)",
                (tid, nid, int(on_success), int(on_failure)))
        db.commit()
        db.close()
