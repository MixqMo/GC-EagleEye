-- GUATECOMPRAS INTELLIGENCE / MODELO RELACIONAL OCDS 1.1 / v1.0
-- Requiere PostgreSQL 15+; sin extensiones obligatorias.
-- Fuente primaria: registros OCDS (ocid, compiledRelease, releases).
-- Los campos libres no se codifican como ENUM: prevalecen los valores publicados.
BEGIN;
CREATE SCHEMA IF NOT EXISTS gci;

CREATE TABLE IF NOT EXISTS gci.ingest_runs (
    run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_uri TEXT NOT NULL,
    source_path TEXT,
    source_period TEXT,
    source_format TEXT NOT NULL CHECK (source_format IN ('json','jsonl','json.gz','jsonl.gz')),
    source_sha256 CHAR(64) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    state TEXT NOT NULL DEFAULT 'running' CHECK (state IN ('running','finished','failed')),
    records_read BIGINT NOT NULL DEFAULT 0,
    snapshots_inserted BIGINT NOT NULL DEFAULT 0,
    current_updates BIGINT NOT NULL DEFAULT 0,
    invalid_records BIGINT NOT NULL DEFAULT 0,
    comment TEXT
);
CREATE INDEX IF NOT EXISTS ingest_runs_source_idx ON gci.ingest_runs (source_uri, started_at DESC);

CREATE TABLE IF NOT EXISTS gci.procurement_processes (
    process_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ocid TEXT NOT NULL UNIQUE,
    tender_source_id TEXT,
    title TEXT,
    status TEXT,
    status_details TEXT,
    procurement_method TEXT,
    procurement_method_details TEXT,
    main_procurement_category TEXT,
    number_of_tenderers INTEGER CHECK (number_of_tenderers >= 0),
    tender_published_at TIMESTAMPTZ,
    tender_start_at TIMESTAMPTZ,
    tender_end_at TIMESTAMPTZ,
    buyer_org_key TEXT,
    procuring_org_key TEXT,
    language TEXT,
    initiation_type TEXT,
    segmentation_id TEXT,
    current_published_at TIMESTAMPTZ,
    current_snapshot_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Versiones históricas completas: la evidencia inmutable es el JSON OCDS original.
CREATE TABLE IF NOT EXISTS gci.process_snapshots (
    snapshot_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE RESTRICT,
    record_hash CHAR(64) NOT NULL,
    compiled_release_id TEXT,
    compiled_release_date TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    observed_first_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_record JSONB NOT NULL,
    UNIQUE (process_id, record_hash),
    UNIQUE (process_id, snapshot_id)
);
CREATE INDEX IF NOT EXISTS process_snapshots_history_idx ON gci.process_snapshots (process_id, published_at DESC NULLS LAST, snapshot_id DESC);
CREATE INDEX IF NOT EXISTS process_snapshots_compiled_id_idx ON gci.process_snapshots (compiled_release_id);

ALTER TABLE gci.procurement_processes
    ADD CONSTRAINT fk_process_current_snapshot
    FOREIGN KEY (process_id, current_snapshot_id) REFERENCES gci.process_snapshots(process_id, snapshot_id)
    DEFERRABLE INITIALLY IMMEDIATE;

CREATE TABLE IF NOT EXISTS gci.snapshot_ingestions (
    snapshot_id BIGINT NOT NULL REFERENCES gci.process_snapshots(snapshot_id) ON DELETE CASCADE,
    run_id BIGINT NOT NULL REFERENCES gci.ingest_runs(run_id) ON DELETE CASCADE,
    record_ordinal BIGINT NOT NULL CHECK (record_ordinal > 0),
    seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, record_ordinal),
    UNIQUE (snapshot_id, run_id, record_ordinal)
);

-- Una organización puede ser comprador, unidad adquirente, proveedor u oferente.
-- org_key conserva el ID fuente completo, p.ej. GT-NIT-..., GT-GCUC-..., GT-CISP-...
CREATE TABLE IF NOT EXISTS gci.organizations (
    org_key TEXT PRIMARY KEY,
    name TEXT,
    id_scheme TEXT,
    id_value TEXT,
    address_country TEXT,
    address_region TEXT,
    address_locality TEXT,
    address_street TEXT,
    organization_type_id TEXT,
    organization_type_desc TEXT,
    organization_entity_type_id TEXT,
    organization_entity_type_desc TEXT,
    organization_level_id TEXT,
    organization_level_desc TEXT,
    legal_entity_type_id TEXT,
    legal_entity_type_desc TEXT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE gci.procurement_processes
    ADD CONSTRAINT fk_process_buyer FOREIGN KEY (buyer_org_key)
    REFERENCES gci.organizations(org_key) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE gci.procurement_processes
    ADD CONSTRAINT fk_process_procuring FOREIGN KEY (procuring_org_key)
    REFERENCES gci.organizations(org_key) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX IF NOT EXISTS organizations_identifier_idx ON gci.organizations (id_scheme, id_value);
CREATE INDEX IF NOT EXISTS organizations_name_idx ON gci.organizations (lower(name));

CREATE TABLE IF NOT EXISTS gci.organization_identifiers (
    org_key TEXT NOT NULL REFERENCES gci.organizations(org_key) ON DELETE CASCADE,
    scheme TEXT NOT NULL,
    identifier TEXT NOT NULL,
    PRIMARY KEY (org_key, scheme, identifier)
);
CREATE INDEX IF NOT EXISTS organization_identifiers_lookup_idx ON gci.organization_identifiers (scheme, identifier);

CREATE TABLE IF NOT EXISTS gci.organization_memberships (
    child_org_key TEXT NOT NULL REFERENCES gci.organizations(org_key) ON DELETE CASCADE,
    parent_org_key TEXT NOT NULL REFERENCES gci.organizations(org_key) ON DELETE CASCADE,
    PRIMARY KEY (child_org_key, parent_org_key),
    CHECK (child_org_key <> parent_org_key)
);
CREATE INDEX IF NOT EXISTS organization_memberships_parent_idx ON gci.organization_memberships (parent_org_key);

CREATE TABLE IF NOT EXISTS gci.organization_contacts (
    contact_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_key TEXT NOT NULL REFERENCES gci.organizations(org_key) ON DELETE CASCADE,
    contact_kind TEXT NOT NULL CHECK (contact_kind IN ('primary','additional')),
    contact_hash CHAR(64) NOT NULL,
    contact_name TEXT,
    email TEXT,
    telephone TEXT,
    fax_number TEXT,
    UNIQUE (org_key, contact_kind, contact_hash)
);

CREATE TABLE IF NOT EXISTS gci.process_organization_roles (
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    org_key TEXT NOT NULL REFERENCES gci.organizations(org_key),
    role_code TEXT NOT NULL,
    PRIMARY KEY (process_id, org_key, role_code)
);
CREATE INDEX IF NOT EXISTS process_org_roles_reverse_idx ON gci.process_organization_roles (org_key, role_code, process_id);

CREATE TABLE IF NOT EXISTS gci.process_categories (
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    category_kind TEXT NOT NULL CHECK (category_kind IN ('additional_procurement','submission_method')),
    category_value TEXT NOT NULL,
    PRIMARY KEY (process_id, category_kind, category_value)
);

CREATE TABLE IF NOT EXISTS gci.bids (
    bid_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    bid_source_id TEXT NOT NULL,
    bid_date TIMESTAMPTZ,
    status TEXT,
    amount NUMERIC,
    currency TEXT,
    UNIQUE (process_id, bid_source_id)
);
CREATE INDEX IF NOT EXISTS bids_process_status_idx ON gci.bids (process_id, status);
CREATE TABLE IF NOT EXISTS gci.bid_tenderers (
    bid_pk BIGINT NOT NULL REFERENCES gci.bids(bid_pk) ON DELETE CASCADE,
    org_key TEXT NOT NULL REFERENCES gci.organizations(org_key),
    PRIMARY KEY (bid_pk, org_key)
);
CREATE INDEX IF NOT EXISTS bid_tenderers_org_idx ON gci.bid_tenderers (org_key, bid_pk);

CREATE TABLE IF NOT EXISTS gci.awards (
    award_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    award_source_id TEXT NOT NULL,
    award_date TIMESTAMPTZ,
    title TEXT,
    status TEXT,
    status_details TEXT,
    amount NUMERIC,
    currency TEXT,
    UNIQUE (process_id, award_source_id),
    UNIQUE (process_id, award_pk)
);
CREATE INDEX IF NOT EXISTS awards_status_amount_idx ON gci.awards (status, currency, amount);
CREATE TABLE IF NOT EXISTS gci.award_suppliers (
    award_pk BIGINT NOT NULL REFERENCES gci.awards(award_pk) ON DELETE CASCADE,
    org_key TEXT NOT NULL REFERENCES gci.organizations(org_key),
    PRIMARY KEY (award_pk, org_key)
);
CREATE INDEX IF NOT EXISTS award_suppliers_org_idx ON gci.award_suppliers (org_key, award_pk);

CREATE TABLE IF NOT EXISTS gci.contracts (
    contract_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    contract_source_id TEXT NOT NULL,
    contract_number TEXT,
    award_source_id TEXT, -- preservado aunque no exista adjudicación correspondiente
    award_pk BIGINT,
    signed_at TIMESTAMPTZ,
    period_start_at TIMESTAMPTZ,
    period_end_at TIMESTAMPTZ,
    title TEXT,
    status TEXT,
    status_details TEXT,
    amount NUMERIC,
    currency TEXT,
    UNIQUE (process_id, contract_source_id),
    UNIQUE (process_id, contract_pk),
    FOREIGN KEY (process_id,award_pk) REFERENCES gci.awards(process_id,award_pk) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS contracts_process_award_idx ON gci.contracts (process_id, award_source_id);

CREATE TABLE IF NOT EXISTS gci.tender_items (
    item_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    item_source_id TEXT NOT NULL,
    description TEXT,
    quantity NUMERIC,
    unit_name TEXT,
    classification_scheme TEXT,
    classification_id TEXT,
    UNIQUE (process_id, item_source_id)
);
CREATE INDEX IF NOT EXISTS tender_items_classification_idx ON gci.tender_items (classification_scheme, classification_id);
CREATE TABLE IF NOT EXISTS gci.tender_item_attributes (
    item_pk BIGINT NOT NULL REFERENCES gci.tender_items(item_pk) ON DELETE CASCADE,
    attribute_source_id TEXT NOT NULL,
    name TEXT,
    value TEXT,
    PRIMARY KEY (item_pk, attribute_source_id)
);

CREATE TABLE IF NOT EXISTS gci.documents (
    document_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('tender','contract')),
    owner_source_id TEXT NOT NULL,
    contract_pk BIGINT,
    document_source_id TEXT NOT NULL,
    title TEXT,
    document_type TEXT,
    document_type_details TEXT,
    published_at TIMESTAMPTZ,
    mime_type TEXT,
    language TEXT,
    url TEXT,
    UNIQUE (process_id, scope, owner_source_id, document_source_id),
    CHECK ((scope = 'tender' AND contract_pk IS NULL) OR (scope = 'contract' AND contract_pk IS NOT NULL)),
    FOREIGN KEY (process_id,contract_pk) REFERENCES gci.contracts(process_id,contract_pk) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS documents_url_idx ON gci.documents (url);
CREATE INDEX IF NOT EXISTS documents_type_idx ON gci.documents (document_type, scope);

-- Las entradas de releases[] son REFERENCIAS a releases, no su contenido completo.
CREATE TABLE IF NOT EXISTS gci.release_references (
    release_ref_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    process_id BIGINT NOT NULL REFERENCES gci.procurement_processes(process_id) ON DELETE CASCADE,
    release_url TEXT NOT NULL,
    release_date TIMESTAMPTZ,
    tags TEXT[] NOT NULL DEFAULT '{}',
    UNIQUE (process_id, release_url)
);
CREATE INDEX IF NOT EXISTS release_references_date_idx ON gci.release_references (release_date);

CREATE TABLE IF NOT EXISTS gci.data_quality_issues (
    issue_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES gci.ingest_runs(run_id) ON DELETE CASCADE,
    snapshot_id BIGINT REFERENCES gci.process_snapshots(snapshot_id) ON DELETE SET NULL,
    ocid TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    issue_code TEXT NOT NULL,
    json_path TEXT,
    detail TEXT,
    source_value JSONB,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS data_quality_issues_run_idx ON gci.data_quality_issues (run_id, severity, issue_code);
CREATE INDEX IF NOT EXISTS data_quality_issues_ocid_idx ON gci.data_quality_issues (ocid);

CREATE INDEX IF NOT EXISTS processes_buyer_idx ON gci.procurement_processes (buyer_org_key, status);
CREATE INDEX IF NOT EXISTS processes_procurement_method_idx ON gci.procurement_processes (procurement_method_details);
CREATE INDEX IF NOT EXISTS processes_period_idx ON gci.procurement_processes (tender_published_at);
CREATE INDEX IF NOT EXISTS processes_status_idx ON gci.procurement_processes (status, status_details);
COMMENT ON SCHEMA gci IS 'GuateCompras Intelligence: esquema OCDS relacional, versionado y auditable';
COMMENT ON COLUMN gci.procurement_processes.ocid IS 'Identificador público estable del proceso; no confundir con compiledRelease.id';
COMMENT ON COLUMN gci.process_snapshots.raw_record IS 'Registro OCDS completo (ocid, compiledRelease, releases) sin pérdida';
COMMENT ON COLUMN gci.contracts.award_source_id IS 'Referencia fuente sin FK obligatoria para permitir links ausentes o fuera de orden';
COMMIT;
