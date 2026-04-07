import os
import json
from datetime import datetime


class SyncActionLog:
    """Records which sync rule was applied to each file and which version won."""

    def __init__(self, path: str):
        self.path = path
        self.actions: dict = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    self.actions = json.load(f)
            except Exception:
                self.actions = {}

    def record(self, rel_path: str, rule: str, state: str, description: str, winner: str | None):
        self.actions[rel_path] = {
            'rule': rule,
            'state': state,
            'description': description,
            'winner': winner,
            'timestamp': datetime.now().isoformat(),
        }

    def save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.actions, f, indent=2)
        except Exception:
            pass

    def get(self, rel_path: str) -> dict | None:
        return self.actions.get(rel_path)
