# Personal Skills

个人维护的通用 agent skills，集中保存指令、参考资料、模板、辅助脚本及离线测试。

## Skills

| Skill | 用途 |
| --- | --- |
| [devflow](skills/devflow/SKILL.md) | 按任务规模执行开发、独立审查、验证与收尾流程。 |
| [agc-upload](skills/agc-upload/SKILL.md) | HarmonyOS 发布签名、打包、AGC API 上传与解析确认；不自动提审或分发。 |
| [app-icon-release](skills/app-icon-release/SKILL.md) | 配置、导出并校验 HarmonyOS、iOS、Android 图标与商店素材。 |

每个 `skills/<name>/` 是一个完整单元。复用时保留目录中的 `references/`、`scripts/`、`assets/` 和 `agents/` 等配套文件，相对引用以 skill 目录为基准。

## 本机统一维护

本机以此仓库的 `skills/<name>/` 为唯一源文件，已有安装入口通过目录软链接指向它：

- Codex 的 `~/.codex/skills/{devflow,agc-upload,app-icon-release}` 分别指向仓库中的同名目录。
- Claude Code 的 `~/.claude/skills/devflow` 指向同一份 `skills/devflow`，不另存副本。

从任一安装入口或仓库目录修改，改的都是同一份文件，无需手工复制。GitHub 远端仍需通过 Git 提交、推送或拉取同步，不会自动提交本机改动。

安装入口使用本机绝对路径软链接；移动或重新克隆仓库后需更新链接，不要删除仍被引用的仓库目录。其他电脑克隆后也需自行建立本机链接。技能更新未被识别时，可重启对应客户端。

## AGC 离线测试

AGC 辅助脚本需要 Python 3.10+。依赖固定在 [requirements.txt](skills/agc-upload/scripts/requirements.txt)，实际发布还需要对应 SDK、`devecocli` 等本机工具，详见 skill 说明。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r skills/agc-upload/scripts/requirements.txt
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s skills/agc-upload/scripts -p 'test_*.py'
```

这些测试使用临时合成材料和模拟接口，不执行真实应用构建、签名申请或 AGC 上传。AGC 真实操作只在用户明确要求且目标、权限和渠道已确认时执行。

## 入库边界

- 只收录确认由自己维护、允许入库的 skill；系统内置或外部来源的 skill 不直接作为自建内容收录。
- 私钥、证书、签名 Profile、API 凭据、密码、本地签名配置和上传回执不得提交；私有仓库也不作为凭据存储。
- 更新平台规则时区分官方要求、工具链行为与项目约定；离线测试通过不等于商店审核或真实上传通过。
- 仓库暂为私有，未额外授予开源许可。
