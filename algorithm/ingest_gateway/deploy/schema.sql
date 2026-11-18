-- Registry for the ingest gateway. Lives in its own database, never in rag_flow.
CREATE TABLE IF NOT EXISTS library (
  id            CHAR(32) PRIMARY KEY,
  name          VARCHAR(128) NOT NULL,
  description   VARCHAR(1024) NOT NULL DEFAULT '',
  visibility    VARCHAR(16) NOT NULL DEFAULT 'public',
  owner_caller  VARCHAR(128) NULL,
  created_by    VARCHAR(128) NOT NULL,
  created_at    DATETIME NOT NULL,
  UNIQUE KEY uq_library_name (name)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ingest_record (
  id              CHAR(32) PRIMARY KEY,
  library_id      CHAR(32) NOT NULL,
  caller_id       VARCHAR(128) NOT NULL,
  filename        VARCHAR(255) NOT NULL,
  size_bytes      BIGINT NOT NULL,
  sha256          CHAR(64) NOT NULL,
  format          VARCHAR(16) NOT NULL,
  category        VARCHAR(16) NOT NULL,
  chunk_method    VARCHAR(32) NOT NULL,
  route           VARCHAR(16) NOT NULL,
  stage           VARCHAR(16) NOT NULL,
  ragflow_doc_id  CHAR(32) NULL,
  chunk_count     INT NOT NULL DEFAULT 0,
  progress        FLOAT NOT NULL DEFAULT 0,
  error_code      VARCHAR(64) NULL,
  error_message   TEXT NULL,
  attempts        INT NOT NULL DEFAULT 0,       -- consecutive transient failures of the current step
  next_attempt_at DATETIME NULL,                -- set while waiting to retry a transient failure
  created_at      DATETIME NOT NULL,
  updated_at      DATETIME NOT NULL,
  UNIQUE KEY uq_library_sha (library_id, sha256),
  KEY ix_stage (stage),
  CONSTRAINT fk_record_library FOREIGN KEY (library_id) REFERENCES library (id)
) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
