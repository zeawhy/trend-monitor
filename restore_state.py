#!/usr/bin/env python3
"""状态恢复脚本 (restore_state.py)

从私有仓库 zeawhy/trend-state 拉取最新状态文件，覆盖到本地对应位置。
在覆盖前会自动将本地现有文件完整备份至 backups/pre_restore_<timestamp>/ 目录。

恢复文件清单:
- outputs/seen_terms.json
- hidden_files/word_tracking.json
- hidden_files/page_tracking.json
- outputs/opportunities.json
- hidden_files/gsc_seen_queries.json
- outputs/github_breakout_state.json
- outputs/last_seen.json
- outputs/jev_health.json
- dashboard/data/last_run.json 与 outputs/last_run.json
"""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from state_backup import (
    BASE_DIR,
    BJ_TZ,
    DEFAULT_REPO,
    STATE_FILES_MAP,
    get_git_ssh_command,
    resolve_ssh_key,
)


def restore_state(
    repo_url: Optional[str] = None,
    base_dir: Optional[str] = None,
    create_backup: bool = True,
) -> bool:
    """拉取 trend-state 并恢复到本地。覆盖前先备份本地现有文件。"""
    repo = repo_url or os.environ.get("TREND_STATE_REPO", DEFAULT_REPO)
    workspace = base_dir or BASE_DIR

    key_path, temp_key_file = resolve_ssh_key()
    if not key_path:
        print("[Restore State] 错误: 未找到 trend-state deploy key 私钥", file=sys.stderr)
        return False

    temp_clone_dir = tempfile.mkdtemp(prefix="trend_state_restore_")
    ssh_cmd = get_git_ssh_command(key_path)
    env = dict(os.environ, GIT_SSH_COMMAND=ssh_cmd)

    try:
        print(f"[Restore State] 正在从 {repo} 拉取最新状态...")
        clone_res = subprocess.run(
            ["git", "clone", "--depth", "1", repo, temp_clone_dir],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        if clone_res.returncode != 0:
            print(f"[Restore State] 克隆失败: {clone_res.stderr}", file=sys.stderr)
            return False

        # 检查 trend-state 是否为空
        state_files_found = []
        for src_rel, dest_rel in STATE_FILES_MAP:
            fpath = os.path.join(temp_clone_dir, dest_rel)
            if os.path.exists(fpath):
                state_files_found.append((src_rel, fpath))

        if not state_files_found:
            print("[Restore State] trend-state 仓库中未找到任何状态文件", file=sys.stderr)
            return False

        # 本地备份
        now_str = datetime.now(timezone.utc).astimezone(BJ_TZ).strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(workspace, "backups", f"pre_restore_{now_str}")
        if create_backup:
            os.makedirs(backup_dir, exist_ok=True)
            for src_rel, _ in STATE_FILES_MAP:
                local_f = os.path.join(workspace, src_rel)
                if os.path.exists(local_f):
                    bak_f = os.path.join(backup_dir, src_rel)
                    os.makedirs(os.path.dirname(bak_f), exist_ok=True)
                    shutil.copy2(local_f, bak_f)
            print(f"[Restore State] 本地现有状态已备份至: {backup_dir}")

        # 覆盖还原
        restored_files = []
        for src_rel, remote_fpath in state_files_found:
            local_target = os.path.join(workspace, src_rel)
            os.makedirs(os.path.dirname(local_target), exist_ok=True)
            shutil.copy2(remote_fpath, local_target)
            restored_files.append(src_rel)

        # 确保 outputs/last_run.json 与 dashboard/data/last_run.json 同步
        dash_last_run = os.path.join(workspace, "dashboard", "data", "last_run.json")
        out_last_run = os.path.join(workspace, "outputs", "last_run.json")
        if os.path.exists(dash_last_run) and not os.path.exists(out_last_run):
            shutil.copy2(dash_last_run, out_last_run)
        elif os.path.exists(out_last_run) and not os.path.exists(dash_last_run):
            os.makedirs(os.path.dirname(dash_last_run), exist_ok=True)
            shutil.copy2(out_last_run, dash_last_run)

        print(f"[Restore State] 成功恢复 {len(set(restored_files))} 个状态文件:")
        for rf in sorted(set(restored_files)):
            print(f"  ✓ {rf}")

        return True

    except Exception as e:
        print(f"[Restore State] 状态恢复异常: {e}", file=sys.stderr)
        return False
    finally:
        shutil.rmtree(temp_clone_dir, ignore_errors=True)
        if temp_key_file and os.path.exists(temp_key_file):
            try:
                os.remove(temp_key_file)
            except Exception:
                pass


if __name__ == "__main__":
    ok = restore_state()
    sys.exit(0 if ok else 1)
