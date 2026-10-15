# Personal Skills

个人维护的通用 agent skills，集中保存指令、参考资料、模板、辅助脚本及离线测试。

## Skills

| Skill | 用途 |
| --- | --- |
| [devflow](skills/devflow/SKILL.md) | 按任务规模执行开发、独立审查、验证与收尾流程。 |
| [agc-upload](skills/agc-upload/SKILL.md) | HarmonyOS 发布签名、打包、AGC API 上传与解析确认；不自动提审或分发。 |
| [app-icon-release](skills/app-icon-release/SKILL.md) | 配置、导出并校验 HarmonyOS、iOS、Android 图标与商店素材。 |

每个 `skills/<name>/` 是一个完整单元。复用时保留目录中的 `references/`、`scripts/`、`assets/` 和 `agents/` 等配套文件，相对引用以 skill 目录为基准。

## 安装与使用

此仓库的 `skills/<name>/` 是唯一源文件。按本机实际使用的 agent，分别软链接到它自己的用户级 skills 目录；不依赖公共 `.agents/skills` 目录，也不替换客户端整个 skills 根目录。

### 为本机建立链接

以下步骤也可直接交给 agent 执行：

1. 将仓库克隆到准备长期保留的本机位置，取得仓库的绝对路径；私有仓库需先具备 GitHub 访问权限。
2. 识别本机已安装且实际使用的 agent。结合 CLI（如 `command -v codex`、`command -v claude`）、桌面或 IDE 客户端、现有配置和已加载技能列表核对。CLI 不在 PATH 不代表没安装；仅有残留配置目录也不能证明客户端仍在使用。不为此自动安装客户端。
3. 确认每个 agent 当前版本实际使用的用户级 skills 目录，至少覆盖本机使用的 Codex 和 Claude Code。默认入口见下表；设置了自定义配置根目录时，以客户端实际配置为准。其他 agent 先查它自己的目录规则，再添加入口，不猜测路径，也不一律使用 `.agents/skills`。

   | Agent | 自有用户级 skills 入口 |
   | --- | --- |
   | Codex | `~/.codex/skills/`；自定义 `CODEX_HOME` 时为该目录下的 `skills/` |
   | Claude Code | `~/.claude/skills/`；自定义配置根目录时使用对应的 `skills/` |

   Codex 这里沿用本机已验证可加载的自有入口，不将公共目录作为替代安装位置；其他版本需核对实际加载结果。参考 [Codex 安装说明](https://learn.chatgpt.com/use-cases/reusable-codex-skills)、[Codex 环境变量](https://learn.chatgpt.com/docs/config-file/environment-variables)及 [Claude Code 技能目录规则](https://code.claude.com/docs/en/skills#choose-where-skills-load)。

4. 对每个已确认的 agent，将仓库中所有含 `SKILL.md` 的 `skills/<name>/` 完整目录，分别软链接到 `<该 agent 的 skills 目录>/<name>`。链接目标使用仓库中的绝对路径，保留脚本、参考资料和模板的相对关系。只在需要时创建缺少的目录，不改动其他技能。

   - 已有链接指向同一源目录：保持不变，重复安装不创建副本。
   - 已有真实目录、其他目标的链接或断链：先比较并报告差异，确认处理方式后再替换；不强制覆盖、不自动删除旧内容，也不将新链接建进旧目录里面。

5. 验证每个入口确为软链接、解析后指向仓库同名目录，且 `SKILL.md` 和配套文件可读。再在对应客户端的技能列表中确认条目可用；更新未被识别时重新加载技能或重启客户端。报告已关联的 agent、技能、目标目录，以及未确认的客户端或未解决冲突；不能只凭创建了目录就宣称安装成功。

本机 Codex 与 Claude Code 的 `devflow`、`agc-upload`、`app-icon-release` 均通过各自入口指向仓库中的同名目录。其他电脑需按上述步骤单独建立链接。

### 日常维护

从任一安装入口或仓库目录修改，改的都是同一份文件，无需手工复制。GitHub 远端仍需通过 Git 提交、推送或拉取同步；不配置定时拉取、启动钩子或自动提交。

安装入口使用本机绝对路径软链接；移动或重新克隆仓库后需更新链接，不要删除仍被引用的仓库目录。切换仓库分支也会改变本机使用的技能版本。

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
