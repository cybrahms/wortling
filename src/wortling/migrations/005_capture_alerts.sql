ALTER TABLE alerts ADD COLUMN capture_id TEXT REFERENCES captures(id);
CREATE INDEX alerts_capture ON alerts(capture_id,acknowledged_at);

-- Recover legacy ownership only where the recorded failure identifies one capture.
UPDATE alerts SET capture_id=(
    SELECT MIN(r.capture_id) FROM processing_runs r
    JOIN captures c ON c.id=r.capture_id
    WHERE c.user_id=alerts.user_id AND r.error=alerts.message
      AND r.finished_at<=alerts.created_at
    HAVING COUNT(DISTINCT r.capture_id)=1
) WHERE kind='capture_failed';

UPDATE alerts SET acknowledged_at=(
    SELECT MAX(r.finished_at) FROM processing_runs r
    WHERE r.capture_id=alerts.capture_id AND r.status IN ('processed','skipped_not_german')
) WHERE kind='capture_failed' AND acknowledged_at IS NULL AND capture_id IN (
    SELECT id FROM captures WHERE status IN ('processed','skipped_not_german')
);
