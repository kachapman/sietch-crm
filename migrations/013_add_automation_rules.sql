-- Migration 013: Automation rules + stage tracking

-- Track when projects enter stages (for "stuck in stage" triggers)
ALTER TABLE opportunities ADD COLUMN IF NOT EXISTS stage_entered_at TIMESTAMP DEFAULT NOW();
UPDATE opportunities SET stage_entered_at = created_at WHERE stage_entered_at IS NULL;

-- Automation rules
CREATE TABLE IF NOT EXISTS automation_rules (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    dry_run BOOLEAN DEFAULT FALSE,
    trigger_type TEXT NOT NULL,
    trigger_config JSONB DEFAULT '{}',
    action_type TEXT NOT NULL,
    action_config JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_run_at TIMESTAMP,
    run_count INTEGER DEFAULT 0
);

-- Execution logs
CREATE TABLE IF NOT EXISTS automation_logs (
    id SERIAL PRIMARY KEY,
    rule_id INTEGER REFERENCES automation_rules(id) ON DELETE CASCADE,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    trigger_context JSONB DEFAULT '{}',
    action_result JSONB DEFAULT '{}',
    dry_run BOOLEAN DEFAULT FALSE,
    error TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_automation_logs_rule ON automation_logs(rule_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_logs_opp ON automation_logs(opportunity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_rules_enabled ON automation_rules(enabled) WHERE enabled = TRUE;
