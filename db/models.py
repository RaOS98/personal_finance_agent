"""Database schema definitions, table creation, and seed data."""

SCHEMA_SQL = """
-- Reference tables

CREATE TABLE IF NOT EXISTS categories (
    id      SERIAL PRIMARY KEY,
    name    VARCHAR(50)  NOT NULL UNIQUE,
    slug    VARCHAR(30)  NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS accounts (
    id       SERIAL PRIMARY KEY,
    name     VARCHAR(100) NOT NULL UNIQUE,
    bank     VARCHAR(50)  NOT NULL,
    currency VARCHAR(3)   NOT NULL,
    type     VARCHAR(20)  NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(100) NOT NULL UNIQUE,
    aliases    TEXT[]       NOT NULL DEFAULT '{}',
    account_id INTEGER      REFERENCES accounts(id)
);

-- Operational tables

CREATE TABLE IF NOT EXISTS transactions (
    id                     SERIAL PRIMARY KEY,
    amount                 DECIMAL(12,2)  NOT NULL,
    currency               VARCHAR(3)     NOT NULL,
    date                   DATE           NOT NULL,
    merchant               VARCHAR(200),
    description            TEXT,
    category_id            INTEGER        NOT NULL REFERENCES categories(id),
    payment_method_id      INTEGER        NOT NULL REFERENCES payment_methods(id),
    telegram_image_id      VARCHAR(200),
    image_path             VARCHAR(300),
    reconciliation_status  VARCHAR(20)    NOT NULL DEFAULT 'unreconciled',
    created_at             TIMESTAMP      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_amt_date_pm
    ON transactions (amount, date, payment_method_id);

CREATE TABLE IF NOT EXISTS statement_lines (
    id                     SERIAL PRIMARY KEY,
    account_id             INTEGER        NOT NULL REFERENCES accounts(id),
    billing_period         VARCHAR(7)     NOT NULL,
    date                   DATE           NOT NULL,
    description            VARCHAR(300)   NOT NULL,
    amount                 DECIMAL(12,2)  NOT NULL,
    reconciliation_status  VARCHAR(20)    NOT NULL DEFAULT 'pending',
    created_at             TIMESTAMP      NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, billing_period, date, description, amount)
);

CREATE INDEX IF NOT EXISTS idx_statement_lines_acct_amt_date
    ON statement_lines (account_id, amount, date);

CREATE TABLE IF NOT EXISTS reconciliation_matches (
    id                SERIAL PRIMARY KEY,
    statement_line_id INTEGER     NOT NULL REFERENCES statement_lines(id) UNIQUE,
    transaction_id    INTEGER     NOT NULL REFERENCES transactions(id) UNIQUE,
    verdict           VARCHAR(20) NOT NULL,
    confirmed_by      VARCHAR(20) NOT NULL,
    created_at        TIMESTAMP   NOT NULL DEFAULT NOW()
);
"""

SEED_SQL = """
-- Categories
INSERT INTO categories (id, name, slug) VALUES
    (1,  'Food & Dining',    'food_dining'),
    (2,  'Groceries',        'groceries'),
    (3,  'Transportation',   'transportation'),
    (4,  'Housing',          'housing'),
    (5,  'Utilities',        'utilities'),
    (6,  'Health',           'health'),
    (7,  'Entertainment',    'entertainment'),
    (8,  'Shopping',         'shopping'),
    (9,  'Education',        'education'),
    (10, 'Other',            'other')
ON CONFLICT DO NOTHING;

-- Accounts
INSERT INTO accounts (id, name, bank, currency, type) VALUES
    (1, 'Visa Infinite Sapphire', 'BCP', 'PEN', 'credit_card'),
    (2, 'Amex Platinum',          'BCP', 'PEN', 'credit_card'),
    (3, 'Soles Current Account',  'BCP', 'PEN', 'current_account'),
    (4, 'Dollars Current Account','BCP', 'USD', 'current_account')
ON CONFLICT DO NOTHING;

-- Payment methods
INSERT INTO payment_methods (id, name, aliases, account_id) VALUES
    (1, 'BCP Visa Infinite Sapphire', '{sapphire,sap,visa}',    1),
    (2, 'BCP Amex Platinum',          '{amex,platinum}',         2),
    (3, 'Yape',                        '{yape}',                 3),
    (4, 'BCP Transfer (Soles)',        '{transfer,bcp}',         3),
    (5, 'BCP Transfer (Dollars)',      '{usd,dollars}',          4),
    (6, 'Cash',                        '{cash,efectivo}',        NULL)
ON CONFLICT DO NOTHING;

-- Reset sequences to avoid PK collisions with future inserts
SELECT setval('categories_id_seq',      (SELECT COALESCE(MAX(id), 1) FROM categories));
SELECT setval('accounts_id_seq',        (SELECT COALESCE(MAX(id), 1) FROM accounts));
SELECT setval('payment_methods_id_seq', (SELECT COALESCE(MAX(id), 1) FROM payment_methods));
"""
