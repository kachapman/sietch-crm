-- Migration 018: Photo Report Templates
-- Adds templates table for configurable photo report generation

CREATE TABLE IF NOT EXISTS photo_report_templates (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    logo_path TEXT,
    company_name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city_state_zip TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    primary_color TEXT NOT NULL DEFAULT '#1a1a1a',
    text_logo_lines JSONB DEFAULT '["Company"]',
    text_logo_font_size INTEGER DEFAULT 16,
    text_logo_tagline_size INTEGER DEFAULT 8,
    max_image_width_inches NUMERIC(3,1) DEFAULT 5.5,
    image_alignment TEXT NOT NULL DEFAULT 'center',
    show_photo_date BOOLEAN DEFAULT TRUE,
    title_includes_claim BOOLEAN DEFAULT TRUE,
    show_captions BOOLEAN DEFAULT TRUE,
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
