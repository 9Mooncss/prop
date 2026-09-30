# ADR-0006: Versioned rules with provenance; seed research can never produce VERIFIED

Status: accepted

Rules are versioned rows (`rule_key`, `version`, `is_current`) with raw text, normalized params, raw params,
evidence links, confidence and interpretation status. Seed research is loaded as UNVERIFIED (confidence 0.6,
because fragments were captured through a summarizing fetch tool). A raw fetch that contains the fragment
upgrades evidence to SOURCE_MATCHED (0.8). Only the owner confirms (CONFIRMED). Conflicts between official
documents are stored with both values and block confirmation until resolved. Firm status is computed, never
declared.
