#!/usr/bin/env python3
"""状态备份模块 (state_backup.py)

在全量任务结束后，将运行所需状态文件自动提交推送到私有仓库 zeawhy/trend-state。
--fast 运行不备份。
推送失败仅记日志到 stderr，绝不阻塞主流程。

备份文件清单:
- outputs/seen_terms.json (去重与已通知状态库)
- hidden_files/word_tracking.json (命中率 14 天追踪)
- hidden_files/page_tracking.json (页面变现与 PnL 追踪)
- outputs/opportunities.json (全生命周期机会看板数据)
- hidden_files/gsc_seen_queries.json (GSC query 基线)
- outputs/github_breakout_state.json (breakout 历史记录)
- outputs/last_seen.json (最新产出记录)
- outputs/jev_health.json (Jev 最近 5 轮健康度)
- dashboard/data/last_run.json 与 last_run.json (任务运行时间戳)
"""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BJ_TZ = timezone(timedelta(hours=8))

# 需要备份与恢复的状态文件相对路径映射 (源相对路径 -> 备份仓库相对路径)
STATE_FILES_MAP = [
    ("outputs/seen_terms.json", "outputs/seen_terms.json"),
    ("hidden_files/word_tracking.json", "hidden_files/word_tracking.json"),
    ("hidden_files/page_tracking.json", "hidden_files/page_tracking.json"),
    ("outputs/opportunities.json", "outputs/opportunities.json"),
    ("hidden_files/gsc_seen_queries.json", "hidden_files/gsc_seen_queries.json"),
    ("outputs/github_breakout_state.json", "outputs/github_breakout_state.json"),
    ("outputs/last_seen.json", "outputs/last_seen.json"),
    ("outputs/jev_health.json", "outputs/jev_health.json"),
    ("dashboard/data/last_run.json", "dashboard/data/last_run.json"),
    ("dashboard/data/last_run.json", "last_run.json"),  # 根目录镜像一份，便于 Actions 并发判定读取
]

DEFAULT_REPO = "git@github.com:zeawhy/trend-state.git"
DEFAULT_KEY_PATH = os.path.expanduser("~/.ssh/trend_state_deploy_key")


def resolve_ssh_key() -> Tuple[Optional[str], Optional[str]]:
    """解析 deploy key 私钥路径。

    支持:
    1. 环境变量 TREND_STATE_DEPLOY_KEY (直接传入私钥内容时写入临时文件)
    2. 环境变量 TREND_STATE_KEY_PATH (文件路径)
    3. 默认路径 ~/.ssh/trend_state_deploy_key

    返回: (key_path, temp_file_to_cleanup)
    """
    key_content = os.environ.get("TREND_STATE_DEPLOY_KEY", "").strip()
    if key_content:
        tf = tempfile.NamedTemporaryFile("w", delete=False, prefix="deploy_key_")
        tf.write(key_content)
        if not key_content.endswith("\n"):
            tf.write("\n")
        tf.close()
        os.chmod(tf.name, 0o600)
        return tf.name, tf.name

    path_env = os.environ.get("TREND_STATE_KEY_PATH", "").strip()
    if path_env and os.path.exists(path_env):
        return path_env, None

    if os.path.exists(DEFAULT_KEY_PATH):
        return DEFAULT_KEY_PATH, None

    return None, None


def get_git_ssh_command(key_path: str) -> str:
    """构造安全的 git ssh command"""
    return f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"


def backup_state(repo_url: Optional[str] = None, base_dir: Optional[str] = None) -> bool:
    """将本地状态文件同步推送到 trend-state 仓库。

    一次运行只提交一次 (1 commit)。
    失败仅记日志，返回 False，不抛出异常。
    """
    repo = repo_url or os.environ.get("TREND_STATE_REPO", DEFAULT_REPO)
    workspace = base_dir or BASE_DIR

    key_path, temp_key_file = resolve_ssh_key()
    if not key_path:
        print("[State Backup] 未找到 trend-state deploy key，跳过备份", file=sys.stderr)
        return False

    temp_clone_dir = tempfile.mkdtemp(prefix="trend_state_backup_")
    ssh_cmd = get_git_ssh_command(key_path)
    env = dict(os.environ, GIT_SSH_COMMAND=ssh_cmd)

    try:
        # 1. 克隆或初始化仓库
        clone_res = subprocess.run(
            ["git", "clone", "--depth", "1", repo, temp_clone_dir],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        is_empty_repo = False
        if clone_res.returncode != 0:
            # 判断是否是空仓库
            if "warning: You appear to have cloned an empty repository" in clone_res.stderr or not os.path.exists(os.path.join(temp_clone_dir, ".git")):
                is_empty_repo = True
                subprocess.run(["git", "init", "-b", "master", temp_clone_dir], check=True, capture_output=True)
                subprocess.run(["git", "-C", temp_clone_dir, "remote", "add", "origin", repo], check=True, capture_output=True)
            else:
                print(f"[State Backup] 克隆 trend-state 失败: {clone_res.stderr}", file=sys.stderr)
                return False

        # 2. 拷贝状态文件到克隆目录
        copied_count = 0
        for src_rel, dest_rel in STATE_FILES_MAP:
            src_full = os.path.join(workspace, src_rel)
            if os.path.exists(src_full):
                dest_full = os.path.join(temp_clone_dir, dest_rel)
                os.makedirs(os.path.dirname(dest_full), exist_ok=True)
                shutil.copy2(src_full, dest_full)
                copied_count += 1

        if copied_count == 0:
            print("[State Backup] 本地无可用状态文件，跳过备份")
            return True

        # 3. 检查是否有变动
        status_res = subprocess.run(
            ["git", "-C", temp_clone_dir, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )

        if not status_res.stdout.strip():
            print("[State Backup] 状态文件无变动，跳过提交")
            return True

        # 4. 提交并推送到 master 分支
        now_iso = datetime.now(timezone.utc).astimezone(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(["git", "-C", temp_clone_dir, "config", "user.name", "trend-monitor[bot]"], check=True)
        subprocess.run(["git", "-C", temp_clone_dir, "config", "user.email", "bot@zeawhy.internal"], check=True)
        subprocess.run(["git", "-C", temp_clone_dir, "add", "-A"], check=True)
        subprocess.run(["git", "-C", temp_clone_dir, "commit", "-m", f"state backup: {now_iso}"], check=True)

        push_cmd = ["git", "-C", temp_clone_dir, "push"]
        if is_empty_repo:
            push_cmd.extend(["-u", "origin", "master"])
        else:
            push_cmd.extend(["origin", "HEAD:master"])

        push_res = subprocess.run(push_cmd, capture_output=True, text=True, env=env, timeout=60)
        if push_res.returncode != 0:
            print(f"[State Backup] 推送 trend-state 失败: {push_res.stderr}", file=sys.stderr)
            return False

        print(f"[State Backup] 状态备份成功 ({copied_count} 个状态文件已推送到 trend-state)")
        return True

    except Exception as e:
        print(f"[State Backup] 状态备份异常(已忽略): {e}", file=sys.stderr)
        return False
    finally:
        shutil.rmtree(temp_clone_dir, ignore_errors=True)
        if temp_key_file and os.path.exists(temp_key_file):
            try:
                os.remove(temp_key_file)
            except Exception:
                pass


if __name__ == "__main__":
    backup_state()
