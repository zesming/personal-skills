# Personal Skills

个人维护的通用 agent skills，集中保存指令、参考资料、模板和辅助脚本。仓库暂为私有，不存放私钥、证书、凭据或上传回执。

## Skills

| Skill | 用途 |
| --- | --- |
| [devflow](skills/devflow/SKILL.md) | 按任务规模执行开发、独立审查、验证与收尾流程。 |
| [agc-upload](skills/agc-upload/SKILL.md) | HarmonyOS 发布签名、打包、AGC API 上传与解析确认；不自动提审或分发。 |
| [app-icon-release](skills/app-icon-release/SKILL.md) | 配置、导出并校验 HarmonyOS、iOS 和 Android 图标与商店素材。 |
| [project-audit](skills/project-audit/SKILL.md) | 从产品、UI 与交互、代码、测试四个维度审查整个项目，或一个 PR/MR、两个提交之间、一段时间内的改动，交叉质证后产出报告并开 draft PR/MR 作为后续修复载体；审 PR/MR 时改为行内评论加审查结论。 |

每个 `skills/<name>/` 是一个完整单元，含 `references/`、`scripts/`、`assets/` 等配套文件，相对引用以 skill 目录为基准。

## 安装

`skills/<name>/` 是唯一源文件。按本机实际使用的 agent，把每个含 `SKILL.md` 的目录软链接到该 agent 的用户级 skills 入口。

### 入口目录

| Agent | 用户级 skills 目录 |
| --- | --- |
| Codex | `~/.codex/skills/` |
| Claude Code | `~/.claude/skills/` |
| DeepSeek Harness | `~/.dsh/skills/` |
| 其他支持 `.agents/skills` 的客户端 | `~/.agents/skills/` |

设置自定义配置根目录时（如 `CODEX_HOME`、`DSH_HOME`），使用该目录下的 `skills/`。

`~/.agents/skills/` 是跨客户端约定目录，Codex、Gemini CLI、Cursor、goose、opencode、VS Code、Windsurf、Augment、Roo Code、Factory、Devin CLI 等都支持；Claude Code 不扫描它，需单独链接到 `~/.claude/skills/`。

### 建立链接

```sh
REPO=~/Projects/personal-skills
ROOTS="$HOME/.codex/skills $HOME/.claude/skills $HOME/.dsh/skills $HOME/.agents/skills"

for s in "$REPO"/skills/*/; do
  n=$(basename "$s")
  for root in $ROOTS; do
    mkdir -p "$root"
    target="$root/$n"
    if [ -L "$target" ]; then
      ln -sfn "${s%/}" "$target"
    elif [ -e "$target" ]; then
      echo "SKIP $target: 已存在非链接内容，请先人工确认"
    else
      ln -s "${s%/}" "$target"
    fi
  done
done
```

已指向同一源目录的链接会保持不变；已存在的真实目录或指向别处的链接只报告不覆盖，确认后再手工处理。脚本不会把新链接建进已有目录里。

### 验证

```sh
REPO=~/Projects/personal-skills
for s in "$REPO"/skills/*/; do
  n=$(basename "$s")
  for root in "$HOME/.codex/skills" "$HOME/.claude/skills" "$HOME/.dsh/skills" "$HOME/.agents/skills"; do
    [ -r "$root/$n/SKILL.md" ] || echo "MISSING: $root/$n"
  done
done
```

再在对应客户端的技能列表中确认条目可用；未识别时重载技能或重启客户端。

DeepSeek Harness 只发现一层（`<根>/<name>/SKILL.md` 或 `<根>/<name>.md`），链接必须直接位于根目录下；同名 skill 以 `~/.dsh/skills/` 优先于 `~/.agents/skills/`。

## 使用

从任一安装入口或仓库目录修改，改的都是同一份文件，无需复制。远端同步通过 Git 提交、推送与拉取。

安装入口是本机绝对路径软链接：移动或重新克隆仓库后需重建链接；切换仓库分支会改变本机使用的技能版本。
