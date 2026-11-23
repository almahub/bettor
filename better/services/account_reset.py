"""Explicit offline account reset; football evidence and credentials are preserved."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from better.services.data_backup import create_backup, digest

FINANCIAL_TABLES = ('bet_slip_selections', 'bet_notes', 'bet_events', 'bet_slips', 'bet_candidates')
FINANCIAL_ENTITIES = ('bank:EUR:v1', 'automatic-multiples:v1', 'candidate-refresh:v1')
FINANCIAL_PREFIXES = ('auto-multiple-slip:', 'salabet-style-')


def reset_account(home, *, backend_lock, safety_directory):
    home = Path(home).resolve()
    identity = hashlib.sha256(str(home).lower().encode()).hexdigest()[:24]
    if (not backend_lock.acquired or getattr(backend_lock, 'account_identity', None) != identity
            or getattr(backend_lock, 'kind', None) != 'backend'):
        raise ValueError('Reset consentito soltanto con il blocco backend della stessa installazione')
    config = yaml.safe_load((home / 'config/888/app.yaml').read_text(encoding='utf-8'))
    url = config['database_url']
    if not url.startswith('sqlite:///data/'):
        raise ValueError('Il reset richiede il database locale della installazione')
    path = (home / url.removeprefix('sqlite:///')).resolve()
    if not path.is_relative_to(home / 'data') or not path.is_file():
        raise ValueError('Percorso database non consentito')
    # A verified complete backup is copied outside the rotating backup set.
    backup = create_backup(home)
    safety = Path(safety_directory).resolve()
    safety.mkdir(parents=True, exist_ok=True)
    saved = safety / backup.name
    shutil.copy2(backup, saved)
    if digest(backup) != digest(saved):
        raise ValueError('Copia di sicurezza non verificata; reset annullato')
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Database non integro; reset annullato')
        # The user explicitly requested an offline purge. Restore every
        # append-only trigger in the same transaction before committing.
        triggers = [(name, sql) for name, table, sql in conn.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'")
            if table in (*FINANCIAL_TABLES, 'annotations')]
        for name, _ in triggers:
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        removed = {}
        for table in FINANCIAL_TABLES:
            removed[table] = conn.execute('DELETE FROM ' + table).rowcount
        ids = []
        for identifier, entity, payload in conn.execute('SELECT id,entity_id,payload FROM annotations'):
            if (entity in FINANCIAL_ENTITIES or entity.startswith(FINANCIAL_PREFIXES)
                    or (entity == 'timing-comparison:v1' and json.loads(payload).get('kind') == 'TRANSFER')):
                ids.append((identifier,))
        conn.executemany('DELETE FROM annotations WHERE id=?', ids)
        removed['financial_annotations'] = len(ids)
        for _, sql in triggers:
            conn.execute(sql)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Riferimenti incoerenti; reset annullato')
    return {'database': str(path), 'safety_backup': str(saved), 'removed': removed}
