CREATE DATABASE IF NOT EXISTS epialert;
USE epialert;

CREATE TABLE IF NOT EXISTS people (
    person_id VARCHAR(10) PRIMARY KEY,
    village VARCHAR(50) NOT NULL,
    street VARCHAR(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    person_id VARCHAR(10) NOT NULL,
    village VARCHAR(50) NOT NULL,
    street VARCHAR(50) NOT NULL,
    report_date DATE NOT NULL,
    infection VARCHAR(100) NULL,
    raw_text TEXT NOT NULL,
    week_number INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_person_week (person_id, week_number),
    FOREIGN KEY (person_id) REFERENCES people(person_id)
);

CREATE INDEX idx_reports_week ON reports(week_number);
CREATE INDEX idx_reports_disease_week ON reports(infection, week_number);
CREATE INDEX idx_reports_location_week ON reports(village, street, week_number);
