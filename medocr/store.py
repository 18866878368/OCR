import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.root / "medocr.sqlite3", timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS batches(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, start TEXT, end TEXT,
          population TEXT NOT NULL DEFAULT 'submitted', created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cases(
          id TEXT PRIMARY KEY, episode_key TEXT UNIQUE, admission_complete INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          path TEXT NOT NULL, file_hash TEXT UNIQUE NOT NULL, page_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_cases(
          batch_id TEXT REFERENCES batches(id), case_id TEXT REFERENCES cases(id),
          PRIMARY KEY(batch_id,case_id));
        CREATE TABLE IF NOT EXISTS pages(
          document_id TEXT REFERENCES documents(id), page INTEGER,
          text TEXT DEFAULT '', category TEXT DEFAULT 'unknown', stage TEXT DEFAULT 'pending',
          engine TEXT DEFAULT '', signature TEXT DEFAULT '', image_path TEXT DEFAULT '',
          error TEXT DEFAULT '', payload TEXT DEFAULT '{}', updated TEXT,
          PRIMARY KEY(document_id,page));
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), kind TEXT NOT NULL,
          event_time TEXT, record_time TEXT, attributes TEXT NOT NULL,
          document_id TEXT REFERENCES documents(id), page INTEGER NOT NULL,
          quote TEXT NOT NULL, origin TEXT NOT NULL, verified INTEGER DEFAULT 0,
          active INTEGER DEFAULT 1, updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(
          batch_id TEXT REFERENCES batches(id), case_id TEXT REFERENCES cases(id),
          metric_id TEXT, item_key TEXT, denominator INTEGER, numerator INTEGER,
          reason TEXT NOT NULL, evidence TEXT NOT NULL, reviewer TEXT NOT NULL,
          updated TEXT NOT NULL, PRIMARY KEY(batch_id,case_id,metric_id,item_key));
        CREATE TABLE IF NOT EXISTS measures(
          batch_id TEXT REFERENCES batches(id), metric_id TEXT, measure TEXT,
          value TEXT NOT NULL, source TEXT NOT NULL, reviewer TEXT NOT NULL, updated TEXT,
          PRIMARY KEY(batch_id,metric_id,measure));
        CREATE TABLE IF NOT EXISTS coverage(
          batch_id TEXT REFERENCES batches(id), case_id TEXT REFERENCES cases(id), metric_id TEXT,
          complete INTEGER NOT NULL, evidence TEXT NOT NULL, reviewer TEXT NOT NULL, updated TEXT,
          PRIMARY KEY(batch_id,case_id,metric_id));
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT NOT NULL,
          before_json TEXT, after_json TEXT, reviewer TEXT, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS extraction_runs(
          case_id TEXT REFERENCES cases(id), chunk_id TEXT, signature TEXT,
          status TEXT NOT NULL, error_code TEXT, updated TEXT,
          PRIMARY KEY(case_id,chunk_id));
        CREATE TABLE IF NOT EXISTS extraction_outputs(
          case_id TEXT REFERENCES cases(id), chunk_id TEXT, signature TEXT,
          event_ids TEXT NOT NULL, updated TEXT NOT NULL,
          PRIMARY KEY(case_id,chunk_id,signature));
        CREATE TABLE IF NOT EXISTS extraction_plans(
          run_id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), mode TEXT NOT NULL,
          manifest TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          batch_id TEXT REFERENCES batches(id), source_id TEXT, path TEXT NOT NULL,
          case_id TEXT REFERENCES cases(id), stage TEXT NOT NULL, error_code TEXT,
          updated TEXT NOT NULL, PRIMARY KEY(batch_id,source_id));
        CREATE TABLE IF NOT EXISTS rule_approvals(
          batch_id TEXT REFERENCES batches(id), metric_id TEXT, rule_hash TEXT,
          reviewer TEXT NOT NULL, note TEXT NOT NULL, updated TEXT NOT NULL,
          PRIMARY KEY(batch_id,metric_id));
        CREATE TABLE IF NOT EXISTS batch_workers(
          batch_id TEXT PRIMARY KEY REFERENCES batches(id), pid INTEGER, state TEXT,
          stop_requested INTEGER DEFAULT 0, error_code TEXT DEFAULT '', updated TEXT);
        CREATE TABLE IF NOT EXISTS batch_processing(
          batch_id TEXT PRIMARY KEY REFERENCES batches(id), mode TEXT NOT NULL, updated TEXT NOT NULL);
        """)
        # Existing local databases are upgraded without discarding cases or human reviews.
        columns = {r[1] for r in self.conn.execute('PRAGMA table_info(jobs)')}
        for name in ('config_signature','progress'):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT DEFAULT ''")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def all(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args)]

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def batch(self, name, start=None, end=None, population="submitted"):
        if not name.strip():
            raise ValueError('批次名称必填')
        if population not in {"submitted", "hospital_confirmed_complete"}:
            raise ValueError("无效的样本范围")
        for value in (start, end):
            if value:
                datetime.strptime(value, "%Y-%m-%d")
        if start and end and start > end:
            raise ValueError("开始日期晚于结束日期")
        bid = digest(json.dumps([name, start, end, population], ensure_ascii=False))
        self.conn.execute("INSERT OR IGNORE INTO batches VALUES(?,?,?,?,?,?)",
                          (bid, name, start, end, population, stamp()))
        self.conn.commit()
        return bid

    def cases(self, batch_id):
        return self.all("SELECT c.* FROM cases c JOIN batch_cases b ON c.id=b.case_id WHERE b.batch_id=? ORDER BY c.id", (batch_id,))

    def pages(self, case_id):
        return self.all("SELECT p.*,d.path,d.case_id FROM pages p JOIN documents d ON p.document_id=d.id WHERE d.case_id=? ORDER BY p.document_id,p.page", (case_id,))

    def events(self, case_id, active=True):
        rows = self.all("SELECT * FROM events WHERE case_id=?" + (" AND active=1" if active else "") + " ORDER BY document_id,page,id", (case_id,))
        for row in rows:
            row["attributes"] = json.loads(row["attributes"])
        return rows

    def audit(self, kind, oid, before, after, reviewer):
        self.conn.execute("INSERT INTO audit(kind,object_id,before_json,after_json,reviewer,created) VALUES(?,?,?,?,?,?)",
                          (kind, oid, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), reviewer, stamp()))

    def save_event(self, event):
        event = dict(event)
        eid = event.get("id") or digest(json.dumps([event["case_id"], event["kind"], event["document_id"], event["page"], event["quote"], event.get("event_time"), event.get("record_time")], ensure_ascii=False))
        # A rerun must not overwrite a reviewed event.
        if self.one("SELECT id FROM events WHERE id=?", (eid,)):
            return eid
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            eid, event["case_id"], event["kind"], event.get("event_time"), event.get("record_time"),
            json.dumps(event.get("attributes", {}), ensure_ascii=False), event["document_id"], event["page"],
            event["quote"], event.get("origin", "manual"), int(event.get("verified", False)), 1, stamp()))
        self.conn.commit()
        return eid

    def review_event(self, eid, updates, reviewer):
        if not reviewer.strip():
            raise ValueError("请填写复核人")
        before = self.one("SELECT * FROM events WHERE id=?", (eid,))
        if not before:
            raise ValueError("事件不存在")
        after = {**before, **updates, "updated": stamp()}
        attrs = after["attributes"]
        if isinstance(attrs, dict):
            attrs = json.dumps(attrs, ensure_ascii=False)
        self.conn.execute("UPDATE events SET event_time=?,record_time=?,attributes=?,verified=?,active=?,updated=? WHERE id=?", (
            after["event_time"], after["record_time"], attrs, int(after["verified"]), int(after["active"]), after["updated"], eid))
        self.audit("event_review", eid, before, after, reviewer)
        self.conn.commit()

    def save_extracted_event(self, event):
        eid = self.save_event(event)
        existing = self.one('SELECT * FROM events WHERE id=?',(eid,))
        # Add semantic attributes to the same regex fact, while protecting all human edits.
        if not existing['verified'] and not self.one("SELECT id FROM audit WHERE object_id=? AND kind='event_review'",(eid,)):
            attrs = {**json.loads(existing['attributes']),**event.get('attributes',{}),'_stale':False}
            with self.conn:
                self.conn.execute('UPDATE events SET attributes=?,origin=?,updated=? WHERE id=?',
                                  (json.dumps(attrs,ensure_ascii=False),event.get('origin',existing['origin']),stamp(),eid))
        return eid

    def merge_cases(self, source, target, reviewer):
        if source == target or not reviewer.strip():
            raise ValueError("请选择不同的住院人次并填写复核人")
        if not all(self.one("SELECT id FROM cases WHERE id=?", (x,)) for x in (source, target)):
            raise ValueError("住院人次不存在")
        # Avoid silently discarding conflicting human assessments.
        if self.one("SELECT case_id FROM reviews WHERE case_id IN (?,?)", (source,target)) or self.one("SELECT case_id FROM coverage WHERE case_id IN (?,?)", (source,target)):
            raise ValueError("待合并病例已有人工指标判定或完整性核查；请先导出并人工整理，避免旧判定用于扩大后的资料范围")
        with self.conn:
            self.conn.execute("UPDATE documents SET case_id=? WHERE case_id=?", (target, source))
            self.conn.execute("UPDATE events SET case_id=? WHERE case_id=?", (target, source))
            self.conn.execute("UPDATE jobs SET case_id=? WHERE case_id=?", (target, source))
            self.conn.execute("DELETE FROM extraction_runs WHERE case_id IN (?,?)", (source,target))
            self.conn.execute("INSERT OR IGNORE INTO extraction_outputs SELECT ?,chunk_id,signature,event_ids,updated FROM extraction_outputs WHERE case_id=?", (target,source))
            self.conn.execute("DELETE FROM extraction_outputs WHERE case_id=?", (source,))
            self.conn.execute("UPDATE extraction_plans SET case_id=? WHERE case_id=?", (target,source))
            self.conn.execute("INSERT OR IGNORE INTO batch_cases SELECT batch_id,? FROM batch_cases WHERE case_id=?", (target, source))
            self.conn.execute("DELETE FROM batch_cases WHERE case_id=?", (source,))
            self.conn.execute("DELETE FROM cases WHERE id=?", (source,))
            self.audit("merge", source, {"source": source}, {"target": target}, reviewer)

    def extraction_status(self, case_id, chunk_id, signature, status, error_code='', event_ids=None):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO extraction_runs VALUES(?,?,?,?,?,?)',
                              (case_id,chunk_id,signature,status,error_code,stamp()))
            if event_ids is not None:
                self.conn.execute('INSERT OR REPLACE INTO extraction_outputs VALUES(?,?,?,?,?)',
                    (case_id,chunk_id,signature,json.dumps(sorted(set(event_ids))),stamp()))

    def extraction_plan(self, case_id, scope):
        manifest = {k:scope[k] for k in ('rules','semantic')}
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO extraction_plans VALUES(?,?,?,?,?)',
                (scope['run_id'],case_id,scope['mode'],json.dumps(manifest,sort_keys=True),stamp()))

    def approve_rule(self, batch_id, metric_id, rule_hash, reviewer, note):
        if not reviewer.strip() or not note.strip():
            raise ValueError('口径确认必须填写确认人和说明')
        before = self.one('SELECT * FROM rule_approvals WHERE batch_id=? AND metric_id=?',(batch_id,metric_id))
        after = (batch_id,metric_id,rule_hash,reviewer,note,stamp())
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO rule_approvals VALUES(?,?,?,?,?,?)',after)
            self.audit('rule_approval',batch_id+':'+metric_id,before,after,reviewer)

    def save_review(self, batch_id, case_id, metric_id, item_key, denominator, numerator, reason, evidence, reviewer):
        from .rules import get_metric
        metric = get_metric(metric_id)
        if metric['kind'] not in {'rate', 'ratio'}:
            raise ValueError("该指标使用补充测量值，不使用逐项分子分母")
        if not self.one("SELECT * FROM batch_cases WHERE batch_id=? AND case_id=?", (batch_id, case_id)):
            raise ValueError("病例不属于此批次")
        for value in (denominator, numerator):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("计数必须是非负整数或空值（未知）")
        if metric['kind'] == 'rate' and numerator is not None and denominator is not None and numerator > denominator:
            raise ValueError("比例的分子不能超过分母")
        if metric_id in {'core_01', 'core_02'} and (item_key != 'admission' or denominator not in {None,0,1} or numerator not in {None,0,1}):
            raise ValueError("指标一、二每次住院用 admission 单条 0/1 判定")
        if not all(str(x).strip() for x in (item_key, reason, evidence, reviewer)):
            raise ValueError("统计对象标识、理由、证据位置和复核人均必填")
        args = (batch_id, case_id, metric_id, item_key)
        before = self.one("SELECT * FROM reviews WHERE batch_id=? AND case_id=? AND metric_id=? AND item_key=?", args)
        after = dict(zip(('batch_id','case_id','metric_id','item_key','denominator','numerator','reason','evidence','reviewer','updated'),
                         (*args, denominator, numerator, reason, evidence, reviewer, stamp())))
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(after.values()))
            self.audit('indicator_review', ':'.join(args), before, after, reviewer)

    def set_coverage(self, batch_id, case_id, metric_id, complete, evidence, reviewer):
        if not evidence.strip() or not reviewer.strip():
            raise ValueError("请填写统计对象覆盖依据和复核人")
        args = (batch_id,case_id,metric_id)
        before = self.one("SELECT * FROM coverage WHERE batch_id=? AND case_id=? AND metric_id=?", args)
        after = (*args,int(complete),evidence,reviewer,stamp())
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?,?)", after)
            self.audit('coverage', ':'.join(args), before, after, reviewer)

    def save_measure(self, batch_id, metric_id, measure, value, source, reviewer):
        from .rules import get_metric, validate_measure
        metric = get_metric(metric_id)
        validate_measure(metric, measure, value)
        if not source.strip() or not reviewer.strip():
            raise ValueError("补充值必须有来源和复核人")
        if not self.one("SELECT id FROM batches WHERE id=?",(batch_id,)):
            raise ValueError("批次不存在")
        args = (batch_id,metric_id,measure)
        before = self.one("SELECT * FROM measures WHERE batch_id=? AND metric_id=? AND measure=?", args)
        after = (*args,json.dumps(value,ensure_ascii=False),source,reviewer,stamp())
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO measures VALUES(?,?,?,?,?,?,?)",after)
            self.audit('measure', ':'.join(args), before, after, reviewer)
