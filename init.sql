-- Sietch CRM v3.0 — Full PostgreSQL Schema
-- Run once on first container start via docker-entrypoint-initdb.d

BEGIN;

-- ============================================================================
-- 7.1 Users & Auth
-- ============================================================================

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    external_user_id TEXT,
    password_hash TEXT,
    password_salt TEXT,
    must_change_password BOOLEAN DEFAULT TRUE,
    display_name TEXT,
    first_name TEXT,
    last_name TEXT,
    is_admin BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    avatar_url TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP
);

CREATE INDEX idx_users_external ON users(external_user_id);

CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    ip_address TEXT
);

CREATE TABLE user_roles (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (user_id, role)
);

CREATE TABLE password_reset_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP
);

-- ============================================================================
-- 7.2 Stages (Pipeline Configuration)
-- ============================================================================

CREATE TABLE stages (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    color TEXT,
    sort_order INTEGER DEFAULT 0,
    stage_type SMALLINT DEFAULT 0,
    probability INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE
);

-- ============================================================================
-- 7.3 System Settings
-- ============================================================================

CREATE TABLE system_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    description TEXT,
    updated_at TIMESTAMP DEFAULT NOW(),
    updated_by INTEGER REFERENCES users(id)
);

-- ============================================================================
-- 7.4 Contacts
-- ============================================================================

CREATE TABLE contacts (
    id SERIAL PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    phone TEXT,
    mobile_phone TEXT,
    company TEXT,
    job_title TEXT,
    address_street TEXT,
    address_city TEXT,
    address_state TEXT,
    address_zip TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    created_by INTEGER REFERENCES users(id)
);

CREATE INDEX idx_contacts_name ON contacts(first_name, last_name);
CREATE INDEX idx_contacts_email ON contacts(email);
CREATE INDEX idx_contacts_company ON contacts(company);

-- ============================================================================
-- 7.5 Custom Fields
-- ============================================================================

CREATE TABLE custom_field_definitions (
    id SERIAL PRIMARY KEY,
    field_key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL,
    is_required BOOLEAN DEFAULT FALSE,
    default_value TEXT,
    sort_order INTEGER DEFAULT 0,
    show_on_create BOOLEAN DEFAULT TRUE,
    show_on_edit BOOLEAN DEFAULT TRUE,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE custom_field_options (
    id SERIAL PRIMARY KEY,
    field_id INTEGER REFERENCES custom_field_definitions(id) ON DELETE CASCADE,
    option_value TEXT NOT NULL,
    option_label TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    is_default BOOLEAN DEFAULT FALSE
);

-- ============================================================================
-- 7.6 Opportunities (Projects)
-- ============================================================================

CREATE TABLE opportunities (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    stage_id INTEGER REFERENCES stages(id),
    stage_type SMALLINT DEFAULT 0,
    bid_value DECIMAL(12,2),
    expected_close_date DATE,
    probability INTEGER DEFAULT 0,
    contact_id INTEGER REFERENCES contacts(id),
    responsible_user_id INTEGER REFERENCES users(id),
    is_private BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    created_by INTEGER REFERENCES users(id),
    updated_at TIMESTAMP DEFAULT NOW(),
    updated_by INTEGER REFERENCES users(id),
    project_number SERIAL UNIQUE
);

CREATE INDEX idx_opportunities_stage ON opportunities(stage_id);
CREATE INDEX idx_opportunities_stage_type ON opportunities(stage_type);
CREATE INDEX idx_opportunities_contact ON opportunities(contact_id);
CREATE INDEX idx_opportunities_responsible ON opportunities(responsible_user_id);

CREATE TABLE opportunity_custom_field_values (
    id SERIAL PRIMARY KEY,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    field_id INTEGER REFERENCES custom_field_definitions(id) ON DELETE CASCADE,
    field_value TEXT,
    UNIQUE(opportunity_id, field_id)
);

CREATE INDEX idx_opportunity_custom_field_values_field ON opportunity_custom_field_values(field_id);
CREATE INDEX idx_opportunity_custom_field_values_opp ON opportunity_custom_field_values(opportunity_id);

-- Trigram indexes for fast ILIKE search across project text fields.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_opportunities_title_trgm ON opportunities USING gin (title gin_trgm_ops);
CREATE INDEX idx_opportunities_description_trgm ON opportunities USING gin (description gin_trgm_ops);
CREATE INDEX idx_contacts_first_name_trgm ON contacts USING gin (first_name gin_trgm_ops);
CREATE INDEX idx_contacts_last_name_trgm ON contacts USING gin (last_name gin_trgm_ops);
CREATE INDEX idx_contacts_company_trgm ON contacts USING gin (company gin_trgm_ops);
CREATE INDEX idx_opportunity_custom_field_values_value_trgm ON opportunity_custom_field_values USING gin (field_value gin_trgm_ops);

-- ============================================================================
-- 7.7 Tags
-- ============================================================================

CREATE TABLE tag_definitions (
    id SERIAL PRIMARY KEY,
    title TEXT UNIQUE NOT NULL,
    color TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE opportunity_tags (
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES tag_definitions(id) ON DELETE CASCADE,
    PRIMARY KEY (opportunity_id, tag_id)
);

CREATE INDEX idx_opportunity_tags_opp ON opportunity_tags(opportunity_id);
CREATE INDEX idx_opportunity_tags_tag ON opportunity_tags(tag_id);

-- ============================================================================
-- 7.8 History (Events, Notes, Calls, Meetings)
-- ============================================================================

CREATE TABLE history_categories (
    id SERIAL PRIMARY KEY,
    title TEXT UNIQUE NOT NULL,
    display_color TEXT,
    is_system BOOLEAN DEFAULT TRUE,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE history_events (
    id SERIAL PRIMARY KEY,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES history_categories(id),
    title TEXT,
    content TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    backdated_created_at TIMESTAMP
);

CREATE INDEX idx_history_opp ON history_events(opportunity_id);
CREATE INDEX idx_history_created ON history_events(created_at DESC);

CREATE TABLE history_replies (
    id SERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES history_events(id) ON DELETE CASCADE,
    parent_reply_id INTEGER REFERENCES history_replies(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_by INTEGER REFERENCES users(id),
    deleted_at TIMESTAMP,
    source_notification_id INTEGER
);

CREATE INDEX idx_history_replies_event ON history_replies(event_id);
CREATE INDEX idx_history_replies_parent ON history_replies(parent_reply_id);
CREATE INDEX idx_history_replies_created ON history_replies(created_at DESC);
CREATE INDEX idx_history_replies_source_notification ON history_replies(source_notification_id);

CREATE TABLE history_attachments (
    id SERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES history_events(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT,
    mime_type TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE history_notify_users (
    event_id INTEGER REFERENCES history_events(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id),
    PRIMARY KEY (event_id, user_id)
);

-- ============================================================================
-- 7.9 Tasks
-- ============================================================================

CREATE TABLE tasks (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
    responsible_user_id INTEGER REFERENCES users(id),
    due_date TIMESTAMP,
    priority INTEGER DEFAULT 0,
    is_closed BOOLEAN DEFAULT FALSE,
    closed_at TIMESTAMP,
    closed_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    created_by INTEGER REFERENCES users(id)
);

CREATE INDEX idx_tasks_responsible ON tasks(responsible_user_id);
CREATE INDEX idx_tasks_opportunity ON tasks(opportunity_id);
CREATE INDEX idx_tasks_closed ON tasks(is_closed);

-- ============================================================================
-- 7.10 Notifications (Internal Feed)
-- ============================================================================

CREATE TABLE notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    actor_user_id INTEGER REFERENCES users(id),
    message TEXT,
    payload JSONB,
    is_read BOOLEAN DEFAULT FALSE,
    telegram_sent BOOLEAN DEFAULT FALSE,
    telegram_sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_notifications_user ON notifications(user_id, is_read, created_at DESC);

CREATE TABLE notification_preferences (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    notification_type TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (user_id, notification_type)
);

-- Track Telegram messages sent for notifications (enables reply detection)
CREATE TABLE telegram_notification_log (
    id SERIAL PRIMARY KEY,
    notification_id INTEGER REFERENCES notifications(id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    sent_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_telegram_notification_log_notification ON telegram_notification_log(notification_id);
CREATE INDEX idx_telegram_notification_log_chat ON telegram_notification_log(chat_id, sent_at DESC);

-- Drop old note-tag trigger (replaced by Python notification creation in server.py)
DROP TRIGGER IF EXISTS trigger_create_tag_notifications ON history_events;
DROP FUNCTION IF EXISTS create_tag_notifications();

-- Auto-create notifications when tasks are assigned
CREATE OR REPLACE FUNCTION create_task_notifications()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.responsible_user_id IS NOT NULL 
       AND NEW.responsible_user_id IS DISTINCT FROM NEW.created_by THEN
        INSERT INTO notifications (user_id, type, opportunity_id, actor_user_id, message, payload)
        VALUES (
            NEW.responsible_user_id,
            'task_assigned',
            NEW.opportunity_id,
            NEW.created_by,
            (SELECT u.display_name || ' assigned you a task: ' || NEW.title
             FROM users u WHERE u.id = NEW.created_by),
            jsonb_build_object('task_id', NEW.id, 'task_title', NEW.title, 'due_date', NEW.due_date)
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_create_task_notifications
    AFTER INSERT ON tasks
    FOR EACH ROW EXECUTE FUNCTION create_task_notifications();

-- ============================================================================
-- 7.11 Photo Gallery
-- ============================================================================

CREATE TABLE project_photo_folders (
    id SERIAL PRIMARY KEY,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    folder_type TEXT NOT NULL,
    label TEXT,
    external_url TEXT,
    external_provider TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(opportunity_id, external_url)
);

CREATE TABLE project_photos (
    id SERIAL PRIMARY KEY,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    folder_id INTEGER REFERENCES project_photo_folders(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT,
    mime_type TEXT,
    exif_data JSONB,
    thumbnail_path TEXT,
    alt_description TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TIMESTAMP DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP
);

CREATE INDEX idx_project_photos_opp ON project_photos(opportunity_id);
CREATE INDEX idx_project_photos_uploaded ON project_photos(uploaded_at DESC);

CREATE TABLE document_folders (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES document_folders(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'company')),
    uploaded_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_doc_folders_parent ON document_folders(parent_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_doc_folders_scope ON document_folders(scope, uploaded_by) WHERE is_deleted = FALSE;

CREATE TABLE project_documents (
    id SERIAL PRIMARY KEY,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    notes TEXT,
    file_path TEXT NOT NULL,
    file_size BIGINT,
    mime_type TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TIMESTAMP DEFAULT NOW(),
    is_deleted BOOLEAN DEFAULT FALSE,
    deleted_at TIMESTAMP,
    company_scope BOOLEAN NOT NULL DEFAULT FALSE,
    folder_id INTEGER REFERENCES document_folders(id)
);

CREATE INDEX idx_project_documents_opp ON project_documents(opportunity_id);
CREATE INDEX idx_project_documents_uploaded ON project_documents(uploaded_at DESC);
CREATE INDEX idx_documents_company ON project_documents(company_scope) WHERE company_scope = TRUE;
CREATE INDEX idx_project_documents_folder ON project_documents(folder_id) WHERE folder_id IS NOT NULL;

CREATE TABLE photo_exif_cache (
    image_path TEXT PRIMARY KEY,
    camera_make TEXT,
    camera_model TEXT,
    lens TEXT,
    focal_length TEXT,
    aperture TEXT,
    shutter_speed TEXT,
    iso INTEGER,
    date_taken TIMESTAMP,
    gps_latitude DECIMAL(9,6),
    gps_longitude DECIMAL(9,6),
    processed_at TIMESTAMP DEFAULT NOW()
);

-- 150 MB per-project quota enforcement trigger
CREATE OR REPLACE FUNCTION check_project_photo_quota()
RETURNS TRIGGER AS $$
DECLARE
    current_size BIGINT;
BEGIN
    SELECT COALESCE(SUM(file_size), 0) INTO current_size
    FROM project_photos
    WHERE opportunity_id = NEW.opportunity_id AND is_deleted = FALSE;
    IF current_size + NEW.file_size > 157286400 THEN
        RAISE EXCEPTION 'Project photo storage limit (150MB) exceeded. Current: % bytes, attempted add: % bytes',
            current_size, NEW.file_size;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER enforce_photo_quota
    BEFORE INSERT ON project_photos
    FOR EACH ROW EXECUTE FUNCTION check_project_photo_quota();

-- ============================================================================
-- 7.12 Mail (IMAP Sync Module — Phase 3+)
-- ============================================================================

CREATE TABLE mail_accounts (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER DEFAULT 993,
    password_encrypted TEXT NOT NULL,
    sync_enabled BOOLEAN DEFAULT TRUE,
    sync_interval_seconds INTEGER DEFAULT 180,
    monitored_folders TEXT DEFAULT 'INBOX',
    last_sync TIMESTAMP,
    last_uid TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    smtp_host TEXT,
    smtp_port INTEGER DEFAULT 587,
    smtp_user TEXT,
    smtp_password_encrypted TEXT,
    smtp_use_tls BOOLEAN DEFAULT TRUE,
    smtp_from_name TEXT,
    oauth_provider TEXT,
    oauth_access_token TEXT,
    oauth_refresh_token TEXT,
    oauth_token_expires TIMESTAMP,
    oauth_scopes TEXT,
    signature_html TEXT,
    signature_text TEXT
);

CREATE TABLE mail_messages (
    id SERIAL PRIMARY KEY,
    account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    imap_uid TEXT NOT NULL,
    message_id TEXT,
    in_reply_to TEXT,
    from_addr TEXT,
    to_addr TEXT,
    cc_addr TEXT,
    subject TEXT,
    body_text TEXT,
    body_html TEXT,
    date_received TIMESTAMP,
    is_read BOOLEAN DEFAULT FALSE,
    is_flagged BOOLEAN DEFAULT FALSE,
    is_archived BOOLEAN DEFAULT FALSE,
    folder TEXT DEFAULT 'INBOX',
    conversation_id TEXT,
    starred BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(account_id, imap_uid, folder)
);

CREATE INDEX idx_mail_account ON mail_messages(account_id);
CREATE INDEX idx_mail_date ON mail_messages(date_received DESC);
CREATE INDEX idx_mail_read ON mail_messages(is_read);
CREATE INDEX idx_mail_conversation ON mail_messages(conversation_id);

ALTER TABLE mail_messages ADD COLUMN raw_headers TEXT;
ALTER TABLE mail_messages ADD COLUMN attachments_json JSONB;

CREATE TABLE mail_deal_links (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id) ON DELETE CASCADE,
    opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE CASCADE,
    linked_by_user_id INTEGER REFERENCES users(id),
    linked_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(message_id, opportunity_id)
);

CREATE TABLE mail_flag_queue (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id) ON DELETE CASCADE,
    flag_name TEXT NOT NULL,
    flag_value BOOLEAN NOT NULL,
    queued_at TIMESTAMP DEFAULT NOW(),
    pushed BOOLEAN DEFAULT FALSE
);

-- ============================================================================
-- 7.14 Mail Tags
-- ============================================================================

CREATE TABLE mail_tags (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    color TEXT DEFAULT '#6c757d',
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(title)
);

CREATE TABLE mail_tag_assignments (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id) ON DELETE CASCADE,
    tag_id INTEGER REFERENCES mail_tags(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(message_id, tag_id)
);

-- ============================================================================
-- 7.15 Mail Templates
-- ============================================================================

CREATE TABLE mail_templates (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    subject TEXT DEFAULT '',
    body_html TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- 7.16 Mail Account Sharing
-- ============================================================================

CREATE TABLE mail_account_access (
    id SERIAL PRIMARY KEY,
    account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    granted_by INTEGER REFERENCES users(id),
    granted_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(account_id, user_id)
);

-- ============================================================================
-- 7.17 Outgoing Mail Tracking
-- ============================================================================

CREATE TABLE mail_outgoing (
    id SERIAL PRIMARY KEY,
    account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    from_addr TEXT,
    to_addr TEXT,
    cc_addr TEXT,
    bcc_addr TEXT,
    subject TEXT,
    body_text TEXT,
    body_html TEXT,
    deal_id INTEGER REFERENCES opportunities(id),
    template_id INTEGER REFERENCES mail_templates(id),
    status TEXT DEFAULT 'queued',
    sent_at TIMESTAMP,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    attachments_json JSONB
);

CREATE INDEX idx_mail_outgoing_deal ON mail_outgoing(deal_id);
CREATE INDEX idx_mail_outgoing_status ON mail_outgoing(status);

-- ============================================================================
-- 7.17b Mail Folders (IMAP sync + local folders)
-- ============================================================================

CREATE TABLE mail_folders (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT DEFAULT 'folder',
    folder_type TEXT DEFAULT 'local',
    imap_account_id INTEGER REFERENCES mail_accounts(id) ON DELETE CASCADE,
    imap_path TEXT,
    last_sync TIMESTAMP,
    user_id INTEGER REFERENCES users(id),
    is_system BOOLEAN DEFAULT FALSE,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_mail_folders_account ON mail_folders(imap_account_id);
CREATE INDEX idx_mail_folders_user ON mail_folders(user_id);

-- ============================================================================
-- 7.17c Mail Attachments (IMAP incoming)
-- ============================================================================

CREATE TABLE mail_attachments (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    mime_type TEXT DEFAULT 'application/octet/octet-stream',
    size_bytes INTEGER DEFAULT 0,
    imap_part_id TEXT,
    stored_path TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_mail_attachments_msg ON mail_attachments(message_id);

-- ============================================================================
-- 7.18 Sync Tracking (Bidirectional with OnlyOffice — transition only)
-- ============================================================================

CREATE TABLE sync_watermarks (
    source TEXT PRIMARY KEY,
    last_sync_timestamp TIMESTAMP,
    last_processed_id INTEGER
);

CREATE TABLE opportunity_changes (
    id SERIAL PRIMARY KEY,
    opportunity_id INTEGER REFERENCES opportunities(id),
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TIMESTAMP DEFAULT NOW(),
    changed_by INTEGER REFERENCES users(id),
    pushed_to_onlyoffice BOOLEAN DEFAULT FALSE,
    pushed_at TIMESTAMP
);

CREATE TABLE sync_errors (
    id SERIAL PRIMARY KEY,
    error_type TEXT NOT NULL,
    source_table TEXT,
    record_id INTEGER,
    message TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- 7.19 Email Classifier (Future — tables created now, unused until merge)
-- ============================================================================

CREATE TABLE email_classifications (
    id SERIAL PRIMARY KEY,
    message_id INTEGER REFERENCES mail_messages(id),
    classified_by TEXT DEFAULT 'classifier',
    suggested_project_id INTEGER REFERENCES opportunities(id),
    confidence_score DECIMAL(3,2),
    action_type TEXT,
    action_target JSONB,
    status TEXT DEFAULT 'pending',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE classifier_training_data (
    id SERIAL PRIMARY KEY,
    message_subject TEXT,
    message_body_hash TEXT,
    sender_email TEXT,
    correct_classification TEXT,
    correct_project_id INTEGER,
    correct_action_type TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- 7.19b Mail Contractors (Scanner routing rules)
-- ============================================================================

CREATE TABLE mail_contractors (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    imap_account_id INTEGER REFERENCES mail_accounts(id) ON DELETE SET NULL,
    folder TEXT DEFAULT 'INBOX',
    action TEXT DEFAULT 'link_only',
    responsible_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    enabled BOOLEAN DEFAULT TRUE,
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- 7.19c Mail Classification Rules
-- ============================================================================

CREATE TABLE mail_classification_rules (
    id SERIAL PRIMARY KEY,
    rule_name TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    action TEXT DEFAULT 'tag',
    action_target TEXT,
    priority INTEGER DEFAULT 0,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- 7.20 Mail Contacts (Per-User)
-- ============================================================================

CREATE TABLE mail_contacts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT DEFAULT '',
    company TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(user_id, email)
);

CREATE INDEX idx_mail_contacts_user ON mail_contacts(user_id);
CREATE INDEX idx_mail_contacts_email ON mail_contacts(user_id, email);

-- ============================================================================
-- 7.21 User Profiles (Migrated from JSON Immediately)
-- ============================================================================

CREATE TABLE user_profiles (
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE PRIMARY KEY,
    kanban_layout JSONB,
    tile_configs JSONB,
    theme_preference TEXT DEFAULT 'default',
    notification_email_digest TEXT DEFAULT 'disabled',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- 7.21 Company Branding Configuration
-- ============================================================================

CREATE TABLE branding (
    id SERIAL PRIMARY KEY,
    company_name TEXT DEFAULT 'Sietch CRM',
    logo_path TEXT DEFAULT '/assets/sietch-logo-2-nobg2.png',
    watermark_path TEXT,
    login_title TEXT DEFAULT 'Sietch CRM',
    header_eyebrow TEXT DEFAULT 'Sietch CRM',
    header_title TEXT DEFAULT 'Workspace <em>dashboard</em>',
    primary_color TEXT DEFAULT '#3b82f6',
    favicon_path TEXT DEFAULT '/favicon.ico',
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Insert default branding
INSERT INTO branding (company_name, login_title, header_eyebrow, header_title)
VALUES ('Sietch CRM', 'Sietch CRM', 'Sietch CRM', 'Workspace <em>dashboard</em>');

-- ============================================================================
-- Seed Data
-- ============================================================================

-- Default history categories
INSERT INTO history_categories (title, display_color, is_system, sort_order) VALUES
    ('Note', '#4a90d9', TRUE, 1),
    ('Call', '#27ae60', TRUE, 2),
    ('Meeting', '#8e44ad', TRUE, 3),
    ('Email', '#e67e22', TRUE, 4),
    ('Customer Update', '#e74c3c', TRUE, 5),
    ('Text Message', '#1abc9c', TRUE, 6),
    ('Task', '#95a5a6', TRUE, 7),
    ('Comment', '#34495e', TRUE, 8);

-- Default system settings
INSERT INTO system_settings (key, value, description) VALUES
    ('default_stage_id', '1', 'Default stage for new projects'),
    ('default_probability', '0', 'Default probability percentage'),
    ('bid_required', 'false', 'Whether bid value is required on create'),
    ('currency_symbol', '$', 'Currency symbol for display'),
    ('opportunity_prefix', 'PRJ', 'Prefix for project numbering'),
    ('mail_sync_enabled', 'false', 'Enable IMAP email sync');

-- Default stage (New Lead) so the system is usable on first boot
INSERT INTO stages (title, color, sort_order, stage_type, probability, is_active) VALUES
    ('New Lead', '#3498db', 0, 0, 0, TRUE);

-- Migration 2026-07-23: Phase 2G — avatar_url on users (idempotent for existing DBs)
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN avatar_url TEXT;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- ============================================================================
-- 7.19 Persistent Minimized Modal State (cross-device sync)
-- ============================================================================

CREATE TABLE IF NOT EXISTS minimized_modal_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMP DEFAULT NOW()
);

COMMIT;
