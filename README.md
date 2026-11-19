# Personal Skills

个人维护的通用 agent skills，集中保存指令、参考资料、模板和辅助脚本。仓库暂为私有，不存放私钥、证书、凭据或上传回执。

## Skills

| Skill | 用途 |
| --- | --- |
| [devflow](skills/devflow/SKILL.md) | 按任务规模执行开发、独立审查、验证与收尾流程。 |
| [agc-upload](skills/agc-upload/SKILL.md) | HarmonyOS 发布签名、打包、AGC API 上传与解析确认；不自动提审或分发。 |
| [app-icon-release](skills/app-icon-release/SKILL.md) | 配置、导出并校验 HarmonyOS、iOS 和 Android 图标与商店素材。 |
| [project-audit](skills/project-audit/SKILL.md) | 全面排查项目缺陷，并跨文档、界面、交互、代码和测试找精简机会。也可按 PR/MR、提交区间或时间审查改动，经质证后交付报告或 PR/MR 审查结论。 |

每个 `skills/<name>/` 是一个完整单元，含 `references/`、`scripts/`、`assets/` 等配套文件，相对引用以 skill 目录为基准。

## 安装

仓库里的 `skills/<name>/` 是每个技能的源目录。按客户端使用以下三个入口，软链接都直接指向仓库。

### 入口目录

| 客户端 | 用户级 skills 入口 |
| --- | --- |
| Codex、DeepSeek Harness、TRAE CLI 等支持共享目录的客户端 | `~/.agents/skills/` |
| Claude Code | `~/.claude/skills/`，或 `$CLAUDE_CONFIG_DIR/skills/` |
| Antigravity（Gemini 桌面端 / `agy` CLI） | `~/.gemini/config/skills/` |

Codex、DeepSeek Harness 和 TRAE CLI 直接读取共享入口，避免在 `~/.codex/skills/`、`~/.dsh/skills/` 和 `~/.trae/skills/` 再建同名链接。Claude Code 和 Antigravity 各有自己的入口，需要分别建立链接。

Antigravity 的全局定制目录是 `~/.gemini/config/`，它**不读** `~/.agents/skills/`。注意 `~/.gemini/config/skills.json` 里的 `~/` 不会展开，只能写绝对路径；直接建 `~/.gemini/config/skills/` 软链接更省事。

### 建立链接

```sh
REPO="$HOME/Projects/personal-skills"
CLAUDE_SKILLS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
ANTIGRAVITY_SKILLS="$HOME/.gemini/config/skills"

for root in "$HOME/.agents/skills" "$CLAUDE_SKILLS" "$ANTIGRAVITY_SKILLS"; do
  mkdir -p "$root" || continue
  for skill in "$REPO"/skills/*; do
    [ -r "$skill/SKILL.md" ] || continue
    target="$root/${skill##*/}"
    if [ -L "$target" ] && [ "$target" -ef "$skill" ]; then
      continue
    elif [ -e "$target" ] || [ -L "$target" ]; then
      printf '跳过已存在的入口 %s\n' "$target" >&2
    else
      ln -s "$skill" "$target"
    fi
  done
done
```

重复运行会复用正确的链接，并提示需要检查的同名入口。若按旧版说明安装过，确认共享入口可用后，可移除 Codex、DeepSeek Harness 和 TRAE CLI 专用目录里指向本仓库的旧链接。Antigravity 的 `~/.gemini/config/skills/` 不存在时会被自动创建。

### 验证

```sh
REPO="$HOME/Projects/personal-skills"
CLAUDE_SKILLS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
ANTIGRAVITY_SKILLS="$HOME/.gemini/config/skills"

for root in "$HOME/.agents/skills" "$CLAUDE_SKILLS" "$ANTIGRAVITY_SKILLS"; do
  for skill in "$REPO"/skills/*; do
    [ -r "$skill/SKILL.md" ] || continue
    target="$root/${skill##*/}"
    if [ ! -L "$target" ] || [ ! "$target" -ef "$skill" ]; then
      printf '缺失或指向错误 %s\n' "$target" >&2
    fi
  done
done
```

再在对应客户端的技能列表中确认条目可用；未识别时重载技能或重启客户端。

## 使用

从任一安装入口或仓库目录修改，改的都是同一份文件，无需复制。远端同步通过 Git 提交、推送与拉取。

安装入口是本机绝对路径软链接：移动或重新克隆仓库后需重建链接；切换仓库分支会改变本机使用的技能版本。
