import os
import hashlib
import json
import shutil
from dataclasses import dataclass


@dataclass
class FileEntry:
    rel_path: str
    local_exists: bool
    icloud_exists: bool
    history_exists: bool
    local_hash: str | None
    icloud_hash: str | None
    history_hash: str | None
    status: str
    state_expr: str
    mtime: float
    last_rule: str | None
    last_description: str | None
    last_winner: str | None
    last_action_time: str | None


class VaultScanner:
    def __init__(self, config):
        self.config = config
        self._hash_cache = self._load_hash_cache()

    def _load_hash_cache(self):
        path = self.config.state_file_path
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _load_action_log(self):
        path = os.path.join(self.config.logs_dir, 'sync_actions.json')
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _hash_file(self, path):
        if not os.path.exists(path):
            return None
        try:
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def _get_hash(self, path, side, rel_path):
        """Return cached hash if mtime+size match, else compute fresh."""
        if rel_path in self._hash_cache and side in self._hash_cache[rel_path]:
            cached = self._hash_cache[rel_path][side]
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
                if cached.get('mtime') == mtime and cached.get('size') == size:
                    return cached.get('hash')
            except Exception:
                pass
        return self._hash_file(path)

    def _gather_rel_paths(self):
        rels = set()
        cfg = self.config

        def collect(root):
            if not os.path.exists(root):
                return
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d.lower() not in cfg.ignored_dirs]
                for f in files:
                    name_lower = f.lower()
                    if name_lower in cfg.ignored_files or f.endswith('.tmp') or f.startswith('._'):
                        continue
                    rel = os.path.normpath(os.path.relpath(os.path.join(dirpath, f), root))
                    if not cfg.is_ignored(rel):
                        rels.add(rel)

        collect(cfg.local_vault)
        collect(cfg.icloud_vault)
        collect(cfg.history_dir)
        return rels

    def scan(self) -> list[FileEntry]:
        action_log = self._load_action_log()
        self._hash_cache = self._load_hash_cache()
        entries = []

        for rel in sorted(self._gather_rel_paths()):
            local = os.path.join(self.config.local_vault, rel)
            icloud = os.path.join(self.config.icloud_vault, rel)
            history = os.path.join(self.config.history_dir, rel)

            le = os.path.exists(local)
            ce = os.path.exists(icloud)
            he = os.path.exists(history)

            lh = self._get_hash(local, 'L', rel) if le else None
            ch = self._get_hash(icloud, 'C', rel) if ce else None
            hh = self._get_hash(history, 'H', rel) if he else None

            # Best mtime across existing copies
            mtime = 0.0
            for p in (local, icloud, history):
                try:
                    mt = os.path.getmtime(p)
                    if mt > mtime:
                        mtime = mt
                except Exception:
                    pass

            status, state_expr = self._compute_status(le, ce, he, lh, ch, hh)
            action = action_log.get(rel, {})

            entries.append(FileEntry(
                rel_path=rel,
                local_exists=le,
                icloud_exists=ce,
                history_exists=he,
                local_hash=lh,
                icloud_hash=ch,
                history_hash=hh,
                status=status,
                state_expr=state_expr,
                mtime=mtime,
                last_rule=action.get('rule'),
                last_description=action.get('description'),
                last_winner=action.get('winner'),
                last_action_time=action.get('timestamp'),
            ))

        return entries

    @staticmethod
    def _compute_status(le, ce, he, lh, ch, hh):
        # All three exist
        if le and ce and he:
            if lh == ch == hh:
                return 'IN_SYNC', 'L = C = H'
            if lh != hh and ch == hh:
                return 'LOCAL_CHANGED', 'L ≠ H, C = H'
            if ch != hh and lh == hh:
                return 'REMOTE_CHANGED', 'C ≠ H, L = H'
            if lh == ch:
                return 'BOTH_CHANGED_SAME', 'L = C ≠ H'
            return 'CONFLICT', 'L ≠ H, C ≠ H'

        # Two exist
        if le and ce and not he:
            if lh == ch:
                return 'NO_HISTORY', 'L = C, no H'
            return 'CONFLICT_NO_HISTORY', 'L ≠ C, no H'
        if le and not ce and he:
            if lh == hh:
                return 'REMOTE_DELETED', 'C missing, L = H'
            return 'LOCAL_CHANGED_NO_REMOTE', 'C missing, L ≠ H'
        if not le and ce and he:
            if ch == hh:
                return 'LOCAL_DELETED', 'L missing, C = H'
            return 'REMOTE_CHANGED_NO_LOCAL', 'L missing, C ≠ H'

        # One exists
        if le and not ce and not he:
            return 'NEW_LOCAL', 'L only'
        if not le and ce and not he:
            return 'NEW_REMOTE', 'C only'
        if not le and not ce and he:
            return 'ORPHANED', 'L and C missing'

        return 'UNKNOWN', '?'

    def get_file_content(self, rel_path: str, source: str) -> str | None:
        roots = {
            'L': self.config.local_vault,
            'C': self.config.icloud_vault,
            'H': self.config.history_dir,
        }
        root = roots.get(source)
        if not root:
            return None
        path = os.path.join(root, rel_path)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception:
            return None

    def resolve(self, rel_path: str, winner: str) -> bool:
        """Copy winning version to all three locations."""
        roots = {
            'L': self.config.local_vault,
            'C': self.config.icloud_vault,
            'H': self.config.history_dir,
        }
        src_root = roots.get(winner)
        if not src_root:
            return False
        src = os.path.join(src_root, rel_path)
        if not os.path.exists(src):
            return False
        for key, root in roots.items():
            if key != winner:
                dst = os.path.join(root, rel_path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        return True
