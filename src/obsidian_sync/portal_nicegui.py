from datetime import datetime

from nicegui import app, ui

from .config import SyncConfig
from .scanner import VaultScanner


STATUS_PRIORITY = {
    'CONFLICT': 0,
    'CONFLICT_NO_HISTORY': 1,
    'LOCAL_CHANGED': 2,
    'REMOTE_CHANGED': 3,
    'LOCAL_CHANGED_NO_REMOTE': 4,
    'REMOTE_CHANGED_NO_LOCAL': 5,
    'NEW_LOCAL': 6,
    'NEW_REMOTE': 7,
    'IN_SYNC': 8,
}


def _fmt_time(value: str | None) -> str:
    if not value:
        return ''
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime('%Y-%m-%d %I:%M:%S %p').lstrip('0')
    except Exception:
        return value


def _time_sort_key(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0


def launch_portal(config: SyncConfig, port: int) -> None:
    scanner = VaultScanner(config)

    ui.add_head_html('''
    <style>
      body { font-size: 16px; color: #e8eef8; }
      .q-toolbar.portal-header { min-height: 72px; background: linear-gradient(90deg, #2f1f53 0%, #1b3d63 55%, #0d5b76 100%); position: relative; }
      .portal-title { font-size: 34px; font-weight: 700; letter-spacing: 0.5px; text-align: center; width: 100%; }
      .portal-updated { font-size: 14px; opacity: 0.9; position: absolute; right: 24px; top: 50%; transform: translateY(-50%); }
      .nicegui-content { background: radial-gradient(circle at 18% 12%, #2a3557 0%, #1a2238 42%, #0d1321 100%); }
      .portal-shell { max-width: 1800px; margin: 0 auto; padding: 20px 40px; }
      .stats-row { width: 100%; gap: 12px; }
      .stat-card { background: #182543; border: 1px solid #2f4269; border-radius: 10px; padding: 8px 12px; min-width: 120px; }
      .stat-label { font-size: 11px; letter-spacing: 0.4px; color: #9db2d2; text-transform: uppercase; }
      .stat-value { font-size: 19px; font-weight: 700; color: #f2f6ff; margin-top: 2px; }
      .q-table__container { border-radius: 12px; overflow: hidden; background: #121d33; border: 1px solid #31486f; }
      .q-table thead tr th { font-size: 15px; font-weight: 700; color: #cdd9f0; background: #162a45; }
      .q-table tbody tr td { font-size: 15px; color: #e7eefb; border-color: #2a3f62; }
      .q-table tbody tr:nth-child(even) td { background: #13213a; }
      .q-table tbody tr:nth-child(odd) td { background: #101b31; }
      .file-name { font-weight: 700; color: #f4f8ff; }
      .status-chip { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 13px; font-weight: 700; margin-right: 8px; }
      .status-sync { background: #1f5530; color: #c5f7d2; border: 1px solid #2d7942; }
      .status-alert { background: #6a3216; color: #ffd9c2; border: 1px solid #8a4320; }
      .status-new { background: #1a3b74; color: #cce0ff; border: 1px solid #2a5ab0; }
      .status-neutral { background: #2b3340; color: #d2dae6; border: 1px solid #434f62; }
      .state-text { opacity: 0.92; color: #c8d7ec; }
      .winner-up { color: #74f28f; font-weight: 700; }
      .winner-down { color: #77b9ff; font-weight: 700; }
      .winner-neutral { color: #d2dae6; font-weight: 700; }
    </style>
    ''', shared=True)

    columns = [
        {'name': 'path', 'label': 'File', 'field': 'path', 'sortable': True},
        {'name': 'localHash', 'label': 'L', 'field': 'localHash', 'sortable': True},
        {'name': 'icloudHash', 'label': 'C', 'field': 'icloudHash', 'sortable': True},
        {'name': 'historyHash', 'label': 'H', 'field': 'historyHash', 'sortable': True},
        {'name': 'statusState', 'label': 'Status', 'field': 'statusState', 'sortable': True},
        {'name': 'description', 'label': 'Last Rule Applied', 'field': 'description', 'sortable': False},
        {'name': 'winner', 'label': 'Winner', 'field': 'winner', 'sortable': True},
        {'name': 'actionTime', 'label': 'When', 'field': 'actionTime', 'sortable': True},
    ]

    def stat_card(label: str):
        with ui.column().classes('stat-card'):
            ui.label(label).classes('stat-label')
            value = ui.label('0').classes('stat-value')
        return value

    def legacy_rows() -> list[dict]:
        entries = scanner.scan()
        return [{
            'path': e.rel_path,
            'local': e.local_exists,
            'icloud': e.icloud_exists,
            'history': e.history_exists,
            'localHash': (e.local_hash or '')[:8],
            'icloudHash': (e.icloud_hash or '')[:8],
            'historyHash': (e.history_hash or '')[:8],
            'status': e.status,
            'state': e.state_expr,
            'mtime': e.mtime,
            'rule': e.last_rule,
            'description': e.last_description,
            'winner': e.last_winner,
            'actionTime': e.last_action_time,
        } for e in entries]

    if not any(getattr(route, 'path', None) == '/api/files' for route in app.routes):
        @app.get('/api/files')
        def api_files() -> list[dict]:
            return legacy_rows()

    @ui.page('/')
    def index_page() -> None:
        with ui.header().classes('portal-header items-center justify-center'):
            ui.label('Obsidian Sync Portal').classes('portal-title')
            last_updated = ui.label('').classes('portal-updated')

        with ui.column().classes('w-full portal-shell'):
            with ui.row().classes('stats-row'):
                total_value = stat_card('Total Files')
                in_sync_value = stat_card('In Sync')
                changed_value = stat_card('Changed')
                conflict_value = stat_card('Conflicts')
                new_value = stat_card('New Files')
                skipped_value = stat_card('No Actions Yet')

            table = ui.table(columns=columns, rows=[], row_key='path').classes('w-full')
            table.props('flat bordered wrap-cells')

        table.add_slot('body-cell-path', '''
            <q-td key="path" :props="props">
              <span class="file-name">{{ props.row.path }}</span>
            </q-td>
        ''')

        table.add_slot('body-cell-statusState', '''
            <q-td key="statusState" :props="props">
              <span
                class="status-chip"
                :class="
                  props.row.status === 'IN_SYNC' ? 'status-sync' :
                  (props.row.status === 'NEW_LOCAL' || props.row.status === 'NEW_REMOTE') ? 'status-new' :
                  (props.row.status.includes('CONFLICT') || props.row.status.includes('CHANGED') || props.row.status.includes('DELETE')) ? 'status-alert' :
                  'status-neutral'
                "
              >{{ props.row.statusLabel }}</span>
              <span class="state-text">{{ props.row.stateExpr }}</span>
            </q-td>
        ''')

        table.add_slot('body-cell-winner', '''
            <q-td key="winner" :props="props">
              <span
                :class="
                  props.row.winnerClass === 'up' ? 'winner-up' :
                  props.row.winnerClass === 'down' ? 'winner-down' :
                  'winner-neutral'
                "
              >{{ props.row.winner }}</span>
            </q-td>
        ''')

        rows_cache: list[dict] = []

        def current_display_time() -> str:
            return datetime.now().strftime('%I:%M:%S %p').lstrip('0')

        def to_row(entry) -> dict:
            state = entry.state_expr
            winner = {
                'L': '↑ Local',
                'C': '↓ iCloud',
                'H': '↔ History',
                None: '',
            }.get(entry.last_winner, entry.last_winner or '')
            winner_class = {
                'L': 'up',
                'C': 'down',
                'H': 'neutral',
                None: 'neutral',
            }.get(entry.last_winner, 'neutral')

            status_label = entry.status.replace('_', ' ')
            if entry.status == 'IN_SYNC':
                status_label = 'L=C=H'
                state = ''

            return {
                'path': entry.rel_path,
                'localHash': (entry.local_hash or '')[:8] or '-',
                'icloudHash': (entry.icloud_hash or '')[:8] or '-',
                'historyHash': (entry.history_hash or '')[:8] or '-',
                'statusState': state,
                'status': entry.status,
                'statusLabel': status_label,
                'stateExpr': state,
                'mtime': entry.mtime,
                'description': entry.last_description or '-',
                'winner': winner,
                'winnerClass': winner_class,
                'actionTime': _fmt_time(entry.last_action_time) or '-',
                'actionTs': _time_sort_key(entry.last_action_time),
            }

        def refresh_table() -> None:
            nonlocal rows_cache
            entries = scanner.scan()
            rows_cache = [to_row(e) for e in entries]
            rows_cache.sort(
                key=lambda r: (
                    -(r.get('actionTs') or 0.0),
                    STATUS_PRIORITY.get(r.get('status', ''), 99),
                    -(r.get('mtime') or 0.0),
                    r.get('path', '').lower(),
                )
            )
            table.rows = rows_cache
            table.update()

            total = len(rows_cache)
            in_sync = sum(1 for r in rows_cache if r.get('status') == 'IN_SYNC')
            conflicts = sum(1 for r in rows_cache if 'CONFLICT' in (r.get('status') or ''))
            changed = sum(1 for r in rows_cache if 'CHANGED' in (r.get('status') or ''))
            new_files = sum(1 for r in rows_cache if r.get('status') in ('NEW_LOCAL', 'NEW_REMOTE'))
            skipped = sum(1 for r in rows_cache if r.get('description') == '-')

            total_value.text = str(total)
            in_sync_value.text = str(in_sync)
            changed_value.text = str(changed)
            conflict_value.text = str(conflicts)
            new_value.text = str(new_files)
            skipped_value.text = str(skipped)
            last_updated.text = f'Last updated: {current_display_time()}'

        refresh_table()
        ui.timer(2.0, refresh_table)

    ui.run(host='127.0.0.1', port=port, show=False, reload=False, title='Obsidian Sync Portal')
