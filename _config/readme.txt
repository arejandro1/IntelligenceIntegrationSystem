This folder is used for placing config files.
The purpose is to facilitate the mapping of non-external directories during deployment.

In the future, scattered configuration files will be gradually centralized in this directory.

## 安全警告

- **示例配置禁止填入真实值**：`config_example.json` 和 `ai_client_config_example.py` 中的 token、API Key、密码等敏感信息应保持为占位符（如 `<generate-uuid4-here>`、`<set-locally>`），**切勿**提交真实凭证到仓库。
- **配置文件不在版本控制中**：真实的 `config.json`、`ai_client_config.py`、`Authentication.db` 已被 `.gitignore` 排除，请确保不在 Git 历史中提交这些文件。
- **凭证泄露后的处理**：如果发现示例配置中的 token/密钥与真实值一致，应立即轮换所有相关凭证，并考虑清理 Git 历史（`git filter-branch` 或 `BFG Repo-Cleaner`）。
  +++++++ REPLACE
