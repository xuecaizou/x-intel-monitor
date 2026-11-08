# -*- coding: utf-8 -*-
"""运维脚本：备份 / 校验 / 恢复演练（第四批）。

用法：
  python scripts/backup_restore.py backup  [--dest backups]
  python scripts/backup_restore.py verify  <备份目录>
  python scripts/backup_restore.py restore <备份目录> <目标空目录>
  python scripts/backup_restore.py export-pending <db路径> > pending.json

注意：备份含 runtime.json（webhook/API Key），请按敏感数据保护。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app.backup as backup  # noqa: E402

DATA = ROOT / "data"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backup", "verify", "restore", "export-pending"])
    ap.add_argument("a", nargs="?")
    ap.add_argument("b", nargs="?")
    ap.add_argument("--dest", default=str(ROOT / "backups"))
    args = ap.parse_args()
    if args.cmd == "backup":
        r = backup.create_backup(DATA, Path(args.dest), project_root=ROOT)
        print(json.dumps({"dir": r["dir"], "files": len(r["manifest"]["files"]),
                          "integrity": r["manifest"]["sqlite_integrity"],
                          "commit": r["manifest"]["code_commit"]}, ensure_ascii=False))
    elif args.cmd == "verify":
        print(json.dumps(backup.verify_backup(Path(args.a)), ensure_ascii=False, indent=2))
    elif args.cmd == "restore":
        print(json.dumps(backup.restore_to(Path(args.a), Path(args.b)), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(backup.export_pending_deliveries(Path(args.a or DATA / "xintel.db")),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
