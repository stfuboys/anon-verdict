"""Consistent SQLite snapshots and offline restore. Never copy a live SQLite file."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid


def backup_directory(db_path):
    return Path(os.getenv('BACKUP_DIR') or (Path(db_path).parent / 'backups'))


@contextmanager
def runtime_lock(db_path):
    path = Path(str(db_path) + '.runtime.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Database is in use: stop the bot before restore/start another instance.') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def verify_backup(path):
    path = Path(path).resolve()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
        result = conn.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise RuntimeError('SQLite integrity check failed')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'users', 'stories', 'comments'} <= tables:
            raise RuntimeError('Not an Anon Verdict database')
        counts = {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                  for name in ('users', 'stories', 'comments')}
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'file': path.name, 'sha256': digest.hexdigest(), 'counts': counts, 'size': path.stat().st_size}


def list_backups(db_path):
    directory = backup_directory(db_path)
    return sorted(directory.glob('anon-verdict-*.sqlite3'), reverse=True) if directory.exists() else []


def create_backup(db_path, reason='manual', keep=None):
    source = Path(db_path).resolve()
    if not source.is_file():
        raise FileNotFoundError('Database does not exist')
    if reason not in {'manual', 'scheduled', 'before-migration', 'before-restore'}:
        raise ValueError('Unknown backup reason')
    directory = backup_directory(db_path)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = directory / f'anon-verdict-{stamp}-{reason}-{uuid.uuid4().hex[:8]}.sqlite3'
    temporary = destination.with_suffix('.tmp')
    try:
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=20) as src:
            with sqlite3.connect(temporary) as dest:
                src.backup(dest, pages=256, sleep=0.05)
        os.chmod(temporary, 0o600)
        info = verify_backup(temporary)
        os.replace(temporary, destination)
        info['file'] = destination.name
        info['created_at'] = datetime.now(timezone.utc).isoformat()
        info['reason'] = reason
        manifest = destination.with_suffix('.json')
        manifest.write_text(json.dumps(info, ensure_ascii=False, indent=2))
        os.chmod(manifest, 0o600)
        limit = max(2, int(keep if keep is not None else os.getenv('BACKUP_KEEP', '28')))
        for old in list_backups(db_path)[limit:]:
            old.unlink()
            old.with_suffix('.json').unlink(missing_ok=True)
        return destination, info
    finally:
        temporary.unlink(missing_ok=True)


def restore_backup(snapshot, db_path):
    snapshot = Path(snapshot).resolve()
    target = Path(db_path).resolve()
    if snapshot == target:
        raise ValueError('Snapshot and target must be different files')
    verify_backup(snapshot)
    with runtime_lock(target):
        # Keep a rollback copy and fully checkpoint the old file before replace.
        if target.exists():
            create_backup(target, reason='before-restore')
            with sqlite3.connect(target, timeout=20) as conn:
                if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                    raise RuntimeError('Another SQLite connection is busy; restore aborted')
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(target.name + '.restore-' + uuid.uuid4().hex)
        try:
            shutil.copyfile(snapshot, staged)
            os.chmod(staged, 0o600)
            verify_backup(staged)
            # The bot's exclusive lock prevents it reopening the old WAL here.
            for suffix in ('-wal', '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    return verify_backup(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    backup = sub.add_parser('create')
    backup.add_argument('--db', required=True)
    check = sub.add_parser('verify')
    check.add_argument('snapshot')
    restore = sub.add_parser('restore', help='Stop the bot before restoring')
    restore.add_argument('snapshot')
    restore.add_argument('--db', required=True)
    args = parser.parse_args()
    if args.command == 'create':
        path, info = create_backup(args.db)
        print(json.dumps(info, ensure_ascii=False))
    elif args.command == 'verify':
        print(json.dumps(verify_backup(args.snapshot), ensure_ascii=False))
    else:
        print(json.dumps(restore_backup(args.snapshot, args.db), ensure_ascii=False))


if __name__ == '__main__':
    main()
