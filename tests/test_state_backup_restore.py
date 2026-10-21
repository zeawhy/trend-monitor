#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import restore_state
import state_backup


class TestStateBackupRestore(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.workspace = os.path.join(self.test_dir, "workspace")
        self.remote_repo = os.path.join(self.test_dir, "remote.git")
        os.makedirs(self.workspace, exist_ok=True)

        # 创建一个本地 bare git 仓库模拟 GitHub 远程
        subprocess.run(["git", "init", "--bare", "-b", "master", self.remote_repo], check=True, capture_output=True)

        # 创建一个假的 key
        self.key_path = os.path.join(self.test_dir, "id_rsa")
        with open(self.key_path, "w") as f:
            f.write("fake-key-content\n")
        os.chmod(self.key_path, 0o600)

        # 环境变量隔离
        self.orig_env = os.environ.copy()
        os.environ["TREND_STATE_KEY_PATH"] = self.key_path
        os.environ["TREND_STATE_REPO"] = self.remote_repo

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_resolve_ssh_key_from_env_content(self):
        os.environ["TREND_STATE_DEPLOY_KEY"] = "dummy-private-key"
        key_path, temp_file = state_backup.resolve_ssh_key()
        self.assertIsNotNone(key_path)
        self.assertEqual(key_path, temp_file)
        self.assertTrue(os.path.exists(key_path))
        with open(key_path) as f:
            self.assertEqual(f.read().strip(), "dummy-private-key")
        os.remove(temp_file)

    def test_backup_and_restore_cycle(self):
        # 1. 在 workspace 中准备状态文件
        outputs_dir = os.path.join(self.workspace, "outputs")
        hidden_dir = os.path.join(self.workspace, "hidden_files")
        dash_dir = os.path.join(self.workspace, "dashboard", "data")
        os.makedirs(outputs_dir, exist_ok=True)
        os.makedirs(hidden_dir, exist_ok=True)
        os.makedirs(dash_dir, exist_ok=True)

        seen_file = os.path.join(outputs_dir, "seen_terms.json")
        with open(seen_file, "w") as f:
            json.dump({"term1": {"last_seen_at": "2026-10-21"}}, f)

        opps_file = os.path.join(outputs_dir, "opportunities.json")
        with open(opps_file, "w") as f:
            json.dump([{"term": "term1", "status": "new"}], f)

        word_file = os.path.join(hidden_dir, "word_tracking.json")
        with open(word_file, "w") as f:
            json.dump([{"term": "term1"}], f)

        last_run_file = os.path.join(dash_dir, "last_run.json")
        with open(last_run_file, "w") as f:
            json.dump({"full_at": "2026-10-21T12:00:00+08:00", "fast_at": "2026-10-21T12:00:00+08:00", "status": "ok"}, f)

        # 2. 执行备份 (对本地文件推送到 bare 仓库)
        # 覆写 get_git_ssh_command，避免对本地路径执行 ssh
        orig_ssh_cmd = state_backup.get_git_ssh_command
        state_backup.get_git_ssh_command = lambda k: "true"
        restore_state.get_git_ssh_command = lambda k: "true"
        try:
            ok = state_backup.backup_state(repo_url=self.remote_repo, base_dir=self.workspace)
            self.assertTrue(ok)

            # 再次备份，由于无修改，应正常返回 True
            ok_repeat = state_backup.backup_state(repo_url=self.remote_repo, base_dir=self.workspace)
            self.assertTrue(ok_repeat)

            # 3. 模拟本地状态损坏或丢失
            with open(seen_file, "w") as f:
                json.dump({"corrupted": True}, f)
            os.remove(opps_file)

            # 4. 执行恢复
            restore_ok = restore_state.restore_state(repo_url=self.remote_repo, base_dir=self.workspace)
            self.assertTrue(restore_ok)

            # 5. 验证文件已成功恢复
            with open(seen_file) as f:
                restored_seen = json.load(f)
            self.assertIn("term1", restored_seen)
            self.assertNotIn("corrupted", restored_seen)

            self.assertTrue(os.path.exists(opps_file))
            with open(opps_file) as f:
                restored_opps = json.load(f)
            self.assertEqual(len(restored_opps), 1)
            self.assertEqual(restored_opps[0]["term"], "term1")

            # 6. 验证覆盖前备份已生成
            backups_dir = os.path.join(self.workspace, "backups")
            self.assertTrue(os.path.exists(backups_dir))
            baks = os.listdir(backups_dir)
            self.assertTrue(len(baks) >= 1)
            bak_path = os.path.join(backups_dir, baks[0], "outputs", "seen_terms.json")
            self.assertTrue(os.path.exists(bak_path))
            with open(bak_path) as f:
                corrupted_bak = json.load(f)
            self.assertTrue(corrupted_bak.get("corrupted"))

        finally:
            state_backup.get_git_ssh_command = orig_ssh_cmd
            restore_state.get_git_ssh_command = orig_ssh_cmd


if __name__ == "__main__":
    unittest.main()
