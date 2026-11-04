"""Read-only migration inventory. No file moves or legacy semantic conversions."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.layout import inventory_project
from app.storage import FileStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Settings.from_env().data_dir)
    parser.add_argument('--project-id')
    args = parser.parse_args()
    if not args.data_dir.is_dir():
        parser.error('DATA_DIR must be an existing directory')
    store = FileStore(args.data_dir)
    projects = [args.project_id] if args.project_id else [str(p.id) for p in store.list_projects()]
    reports = [inventory_project(store, project) for project in projects]
    print(json.dumps({'dry_run': True, 'projects': reports}, indent=2))
    return 0 if all(report['valid'] for report in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
