-- Migration 007: Trendyol AI yorum özeti sütunu
ALTER TABLE products ADD COLUMN IF NOT EXISTS ai_review_summary TEXT;
