"""EpiAlert Database Module

Provides database access for the EpiAlert project.

Uses SQLite by default (built-in, no installation required).
Can be configured to use MySQL if available.

All paths are relative to the project root directory.
"""

import sqlite3
import os
from pathlib import Path
from src.config import DB_CONFIG


# ---------------------------------------------------------------------------
# Database path configuration
# ---------------------------------------------------------------------------

# Default SQLite database path (relative to project root)
DEFAULT_SQLITE_PATH = Path("epialert_demo.db")

# MySQL configuration (from .env / config)
# If MYSQL_ENABLED is True and mysql.connector is available, use MySQL
MYSQL_ENABLED = os.getenv("MYSQL_ENABLED", "False").lower() in ("true", "1", "yes")


def get_db_path() -> Path:
    """Get the database file path, always absolute.

    Returns SQLite path by default. Can be overridden via DB_PATH env var.
    The env var value is made absolute if relative, so CWD changes cannot
    silently redirect the database to a different directory.
    """
    db_path = os.getenv("DB_PATH", None)
    if db_path:
        p = Path(db_path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        return p
    # Default: resolve relative to the project root, not CWD.
    # We detect the project root as the directory containing this file's parent.
    # db.py lives in src/database/, so the project root is two levels up.
    return (Path(__file__).resolve().parent.parent.parent / DEFAULT_SQLITE_PATH)


def _enable_wal(conn):
    """Enable WAL mode for better concurrent write performance."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache


def get_connection():
    """Get a database connection.
    
    Prefers SQLite if mysql.connector is not available.
    Falls back to SQLite if MySQL connection fails.
    """
    # Try MySQL first if enabled
    if MYSQL_ENABLED:
        try:
            import mysql.connector
            db_path = get_db_path()
            # MySQL doesn't use file paths the same way
            config = {
                "host": DB_CONFIG.get("host", "localhost"),
                "port": DB_CONFIG.get("port", 3306),
                "database": DB_CONFIG.get("database", "epialert"),
                "user": DB_CONFIG.get("user", "root"),
                "password": DB_CONFIG.get("password", ""),
            }
            conn = mysql.connector.connect(**config)
            return conn
        except ImportError:
            pass  # fall through to SQLite
        except Exception:
            pass  # fall through to SQLite
    
    # Use SQLite as default — path is already absolute from get_db_path()
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _enable_wal(conn)
    return conn


def init_schema():
    """Initialize the database schema with required tables.
    
    Creates tables if they don't exist:
    - people: person_id, village, street
    - reports: report_id, person_id, village, street, report_date, infection, raw_text, week_number
    """
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS people (
            person_id VARCHAR(10) PRIMARY KEY,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            report_date DATE NOT NULL,
            infection VARCHAR(100),
            raw_text TEXT NOT NULL,
            week_number INT NOT NULL
        )
    """)
    
    # SQLite uses CREATE UNIQUE INDEX for unique constraints
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS person_week_unique ON reports(person_id, week_number)")
    
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_week ON reports(week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_disease_week ON reports(infection, week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reports_location_week ON reports(village, street, week_number)")

    # Detection state table — persists full detection state per (disease, spatial_unit, week)
    # This is what makes the adaptive baseline reproducible, auditable, and replayable.
    # Persisting a_t (not deriving on demand) is the key property.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS detection_state (
            disease VARCHAR(100) NOT NULL,
            spatial_unit VARCHAR(100) NOT NULL,
            week_number INT NOT NULL,
            cusum_S REAL NOT NULL DEFAULT 0,
            ewma_Z REAL NOT NULL DEFAULT 0,
            baseline_mu REAL,
            baseline_sigma REAL,
            baseline_Bt_size INT,
            status VARCHAR(20) NOT NULL DEFAULT 'IN_CONTROL',
            a_t INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (disease, spatial_unit, week_number)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ds_disease_week ON detection_state(disease, week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ds_spatial_week ON detection_state(spatial_unit, week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ds_status ON detection_state(status)")

    # ---- P3: Additional tables for web application ----
    
    # Users table for web auth
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(100) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # People table with phone numbers (P2 enriched)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS people (
            person_id VARCHAR(10) PRIMARY KEY,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            phone_number VARCHAR(20)
        )
    """)
    # Add phone_number column if upgrading from old schema (without it)
    try:
        cur.execute("ALTER TABLE people ADD COLUMN phone_number VARCHAR(20)")
    except sqlite3.OperationalError:
        pass  # column already exists
    
    # Raw reports from ingestion (P4/P6)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10) NOT NULL,
            week_number INT NOT NULL,
            raw_text TEXT NOT NULL,
            extracted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_reports_week ON raw_reports(week_number)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_reports_person_week ON raw_reports(person_id, week_number)")
    
    # Extracted records (P4)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS extracted_records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10) NOT NULL,
            week_number INT NOT NULL,
            disease VARCHAR(100),
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            reporting_date DATE,
            extractor_type VARCHAR(20) NOT NULL DEFAULT 'rule',
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people(person_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_extracted_week ON extracted_records(week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_extracted_disease_week ON extracted_records(disease, week_number)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_extracted_person_week ON extracted_records(person_id, week_number)")
    
    # Quarantine for failed validation (P5)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quarantine (
            qw_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id VARCHAR(10),
            week_number INT NOT NULL,
            raw_text TEXT NOT NULL,
            failure_reason VARCHAR(200) NOT NULL,
            quarantined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved INTEGER NOT NULL DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_quarantine_week ON quarantine(week_number)")
    
    # Weekly aggregates: (disease, village, street, week) counts (P7)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weekly_aggregates (
            agg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            population INT NOT NULL DEFAULT 0,
            rate REAL,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (week_number, disease, village, street)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agg_disease_week ON weekly_aggregates(disease, week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agg_location_week ON weekly_aggregates(village, street, week_number)")
    
    # Baseline stats (P7)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS baseline_stats (
            stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            mu REAL,
            sigma REAL,
            Bt_size INT,
            status VARCHAR(20),
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (week_number, disease, village, street)
        )
    """)
    
    # CUSUM stats (P7)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cusum_stats (
            stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            S REAL NOT NULL DEFAULT 0,
            z_t REAL,
            signal INTEGER NOT NULL DEFAULT 0,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (week_number, disease, village, street)
        )
    """)
    
    # EWMA stats (P7)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ewma_stats (
            stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            Z REAL NOT NULL DEFAULT 0,
            UCL REAL,
            signal INTEGER NOT NULL DEFAULT 0,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (week_number, disease, village, street)
        )
    """)
    
    # Trend stats (P7)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trend_stats (
            stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            recent_count INT,
            direction VARCHAR(10),
            slope REAL,
            pct_change REAL,
            sustained_increase INTEGER NOT NULL DEFAULT 0,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (week_number, disease, village, street)
        )
    """)
    
    # Detection results: combined detector output (P8)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS detection_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            observed_count INT,
            expected_count INT,
            cusum_signal INTEGER NOT NULL DEFAULT 0,
            ewma_signal INTEGER NOT NULL DEFAULT 0,
            trend_sustained INTEGER NOT NULL DEFAULT 0,
            baseline_deviation REAL,
            status VARCHAR(20) NOT NULL DEFAULT 'NORMAL',
            severity VARCHAR(20),
            explanation TEXT,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fusion_mode VARCHAR(20) NOT NULL DEFAULT 'union',
            UNIQUE (week_number, disease, village, street, fusion_mode)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dr_disease_week ON detection_results(disease, week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dr_status ON detection_results(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dr_fusion_mode ON detection_results(fusion_mode)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_dr_fusion_week ON detection_results(fusion_mode, week_number)")
    
    # Alerts (P9) — populated from detection_results where status is ALERT or HIGH_ALERT.
    # fusion_mode records which detection_results rows the alert was derived from.
    # The natural key is (disease, village, street, week_number, fusion_mode) —
    # an alert is about a specific disease at a specific place in a specific week
    # in a specific fusion mode, so union and confirmation alerts coexist.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            street VARCHAR(50) NOT NULL,
            week_number INT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'WATCH',
            severity VARCHAR(20) NOT NULL DEFAULT 'low',
            observed_count INT,
            expected_count INT,
            cusum_signal INTEGER NOT NULL DEFAULT 0,
            ewma_signal INTEGER NOT NULL DEFAULT 0,
            cusum_stat REAL,
            ewma_stat REAL,
            trend_description TEXT,
            baseline_deviation REAL,
            message TEXT,
            sms_eligible INTEGER NOT NULL DEFAULT 0,
            fusion_mode VARCHAR(20) NOT NULL DEFAULT 'confirmation',
            explanation TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (disease, village, street, week_number, fusion_mode)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_disease_week ON alerts(disease, week_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_fusion ON alerts(fusion_mode)")
    
    # SMS messages (P10) — one row per (alert_id, phone_number), deduplicated.
    # Only 3 distinct phone numbers exist in the dataset, so an alert affecting
    # 400 people sends at most 3 SMS messages (one per unique number).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sms_messages (
            sms_id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL,
            phone_number VARCHAR(20) NOT NULL,
            message TEXT NOT NULL,
            provider VARCHAR(50) NOT NULL DEFAULT 'mock',
            provider_response TEXT,
            delivery_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            error_text TEXT,
            FOREIGN KEY (alert_id) REFERENCES alerts(alert_id),
            UNIQUE (alert_id, phone_number)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_alert ON sms_messages(alert_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_messages(phone_number)")
    
    # Ground truth (P12)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ground_truth (
            gt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id VARCHAR(10) NOT NULL UNIQUE,
            disease VARCHAR(100) NOT NULL,
            village VARCHAR(50) NOT NULL,
            start_week INT NOT NULL,
            end_week INT NOT NULL,
            confirmed INTEGER NOT NULL DEFAULT 1
        )
    """)
    
    # Evaluation results (P12)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS evaluation_results (
            eval_id INTEGER PRIMARY KEY AUTOINCREMENT,
            detector VARCHAR(50) NOT NULL,
            metric VARCHAR(50) NOT NULL,
            value REAL,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_eval_detector ON evaluation_results(detector)")
    
    # Processing runs (P6)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processing_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_number INT NOT NULL,
            run_type VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            records_processed INT NOT NULL DEFAULT 0,
            records_quarantined INT NOT NULL DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            UNIQUE (week_number, run_type)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_week ON processing_runs(week_number)")

    # --- Migration: alerts table must include fusion_mode in its unique key ---
    # The original schema used UNIQUE (disease, village, street, week_number).
    # With both union and confirmation alerts coexisting, the key must be
    # (disease, village, street, week_number, fusion_mode).
    # SQLite cannot alter a UNIQUE constraint in place. If the table was
    # created with the old schema (no fusion_mode in UNIQUE), we recreate it.
    # If the table already has fusion_mode in its UNIQUE (new schema or already
    # migrated), there is nothing to do.
    cur.execute("PRAGMA table_info(alerts)")
    alert_cols = {r[1] for r in cur.fetchall()}
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'")
    row = cur.fetchone()
    alerts_sql = row[0] if row else None
    has_fusion_in_unique = False
    if alerts_sql and "UNIQUE" in alerts_sql:
        try:
            uq_part = alerts_sql.split("UNIQUE")[1].split(")")[0]
            has_fusion_in_unique = "fusion_mode" in uq_part
        except Exception:
            has_fusion_in_unique = False
    if "fusion_mode" not in alert_cols:
        # Very old schema — no fusion_mode column at all. Add it.
        cur.execute("ALTER TABLE alerts ADD COLUMN fusion_mode VARCHAR(20) NOT NULL DEFAULT 'confirmation'")
        cur.execute("UPDATE alerts SET fusion_mode = 'confirmation' WHERE fusion_mode IS NULL")
        has_fusion_in_unique = False
    if not has_fusion_in_unique:
        # Recreate the alerts table with fusion_mode in the UNIQUE key.
        # This preserves all existing rows.
        print("Migration: rebuilding alerts table with fusion_mode in UNIQUE key...")
        cur.execute("CREATE TABLE IF NOT EXISTS alerts_new ("
            "alert_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "disease VARCHAR(100) NOT NULL,"
            "village VARCHAR(50) NOT NULL,"
            "street VARCHAR(50) NOT NULL,"
            "week_number INT NOT NULL,"
            "status VARCHAR(20) NOT NULL DEFAULT 'WATCH',"
            "severity VARCHAR(20) NOT NULL DEFAULT 'low',"
            "observed_count INT,"
            "expected_count INT,"
            "cusum_signal INTEGER NOT NULL DEFAULT 0,"
            "ewma_signal INTEGER NOT NULL DEFAULT 0,"
            "cusum_stat REAL,"
            "ewma_stat REAL,"
            "trend_description TEXT,"
            "baseline_deviation REAL,"
            "message TEXT,"
            "sms_eligible INTEGER NOT NULL DEFAULT 0,"
            "fusion_mode VARCHAR(20) NOT NULL DEFAULT 'confirmation',"
            "explanation TEXT,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "UNIQUE (disease, village, street, week_number, fusion_mode)"
        ")")
        cur.execute(
            "INSERT OR IGNORE INTO alerts_new "
            "(alert_id, disease, village, street, week_number, status, severity, "
            "observed_count, expected_count, cusum_signal, ewma_signal, cusum_stat, ewma_stat, "
            "trend_description, baseline_deviation, message, sms_eligible, fusion_mode, "
            "explanation, created_at) "
            "SELECT alert_id, disease, village, street, week_number, status, severity, "
            "observed_count, expected_count, cusum_signal, ewma_signal, cusum_stat, ewma_stat, "
            "trend_description, baseline_deviation, message, sms_eligible, fusion_mode, "
            "explanation, created_at FROM alerts"
        )
        cur.execute("DROP TABLE alerts")
        cur.execute("ALTER TABLE alerts_new RENAME TO alerts")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_disease_week ON alerts(disease, week_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_fusion ON alerts(fusion_mode)")
        print("Migration: alerts table rebuilt with fusion_mode in UNIQUE key.")
    else:
        # New schema already in place.
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_disease_village_street_week_fusion "
            "ON alerts(disease, village, street, week_number, fusion_mode)"
        )

    # Ensure fusion_mode column exists on detection_results.
    # ALTER TABLE ADD COLUMN is backward-compatible — it does NOT destroy data.
    # We do NOT drop and recreate the table (that destroyed 10,920 rows on every
    # call in the previous version). The UNIQUE constraint on
    # (week_number, disease, village, street, fusion_mode) was created when the
    # table was first defined with fusion_mode in the spec.
    cur.execute("PRAGMA table_info(detection_results)")
    has_fusion = any(r[1] == "fusion_mode" for r in cur.fetchall())
    if not has_fusion:
        cur.execute('ALTER TABLE detection_results ADD COLUMN fusion_mode VARCHAR(20) NOT NULL DEFAULT "union"')
        cur.execute('UPDATE detection_results SET fusion_mode = "union" WHERE fusion_mode IS NULL OR fusion_mode NOT IN ("union", "confirmation")')

    conn.commit()
    cur.close()
    conn.close()





def upsert_detection_state(conn, disease: str, spatial_unit: str, week_number: int,
                            cusum_S: float, ewma_Z: float, baseline_mu: float,
                            baseline_sigma: float, baseline_Bt_size: int,
                            status: str, a_t: int) -> None:
    """Upsert a detection state record.

    Uses parameterised SQL only. Never construct SQL from data values.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open database connection.
    disease : str
        Disease/syndrome name.
    spatial_unit : str
        Street, cluster, or village identifier.
    week_number : int
        Week number (1-based).
    cusum_S : float
        CUSUM statistic S_t.
    ewma_Z : float
        EWMA statistic Z_t.
    baseline_mu : float
        Baseline mean mu_t (or None if insufficient).
    baseline_sigma : float
        Baseline std sigma_t (or None if insufficient).
    baseline_Bt_size : int
        Number of eligible weeks |B_t|.
    status : str
        One of IN_CONTROL, PROVISIONAL, CONFIRMED, INSUFFICIENT_BASELINE.
    a_t : int
        Alert flag: 0 or 1. a_t=1 marks the week as baseline-ineligible.
    """
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO detection_state
            (disease, spatial_unit, week_number, cusum_S, ewma_Z,
             baseline_mu, baseline_sigma, baseline_Bt_size, status, a_t)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(disease, spatial_unit, week_number) DO UPDATE SET
            cusum_S = excluded.cusum_S,
            ewma_Z = excluded.ewma_Z,
            baseline_mu = excluded.baseline_mu,
            baseline_sigma = excluded.baseline_sigma,
            baseline_Bt_size = excluded.baseline_Bt_size,
            status = excluded.status,
            a_t = excluded.a_t
    """, (disease, spatial_unit, week_number, cusum_S, ewma_Z,
          baseline_mu, baseline_sigma, baseline_Bt_size, status, a_t))
    conn.commit()
    cur.close()


def get_detection_state(disease: str, spatial_unit: str, week_number: int) -> dict:
    """Retrieve a detection state record.

    Parameters
    ----------
    disease : str
    spatial_unit : str
    week_number : int

    Returns
    -------
    dict or None if no record exists.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM detection_state WHERE disease = ? AND spatial_unit = ? AND week_number = ?",
        (disease, spatial_unit, week_number)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row is None:
        return None
    return dict(row)


def people_count(week_number: int = None) -> int:
    """Count people in the database.
    
    If week_number is specified, counts distinct people with records in that week.
    Otherwise counts all people in the people table.
    """
    conn = get_connection()
    cur = conn.cursor()
    
    if week_number:
        cur.execute("SELECT COUNT(DISTINCT person_id) FROM reports WHERE week_number = ?", (week_number,))
    else:
        cur.execute("SELECT COUNT(*) FROM people")
    
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


def get_person_weekly_record(person_id: str, week_number: int) -> dict:
    """Get a person's weekly record for a specific week."""
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute(
        "SELECT * FROM reports WHERE person_id = ? AND week_number = ?",
        (person_id, week_number)
    )
    row = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if row:
        return dict(row)
    return {}


def get_weekly_stats(week_number: int) -> dict:
    """Get statistics for a specific week."""
    conn = get_connection()
    cur = conn.cursor()
    
    # Total records
    cur.execute("SELECT COUNT(*) FROM reports WHERE week_number = ?", (week_number,))
    total = cur.fetchone()[0]
    
    # Infection distribution
    cur.execute(
        "SELECT infection, COUNT(*) FROM reports WHERE week_number = ? GROUP BY infection",
        (week_number,)
    )
    infection_dist = {row[0]: row[1] for row in cur.fetchall()}
    
    # People count
    cur.execute("SELECT COUNT(DISTINCT person_id) FROM reports WHERE week_number = ?", (week_number,))
    person_count = cur.fetchone()[0]
    
    cur.close()
    conn.close()
    
    return {
        "week_number": week_number,
        "total_records": total,
        "person_count": person_count,
        "infection_distribution": infection_dist,
    }


def close():
    """Placeholder for compatibility - SQLite connections are auto-closed."""
    pass
