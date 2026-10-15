---
name: agc-upload
description: 完成 HarmonyOS 发布签名、打包，以及通过 API 上传 AGC 软件包并确认解析结果。支持 buildVersion 递增和失败续接；上架自检由用户手动操作，不包含正式提审或邀请测试分发。
---

# AGC 打包、签名与上传

原生 HarmonyOS 发布链路：复用或初始化发布签名 → `devecocli` 打包 → SDK 验签 → API 上传及解析确认。上传由随附脚本完成；对应渠道取得 package ID 且解析正常后，才报告上传完成。Skill 到此结束，上架自检由用户手动操作，不使用 computer use 或浏览器自动启动自检。

## 首次配置

Python 3.10+；将依赖装在专用环境，后续复用：

```sh
python3 -m venv "$HOME/.cache/agc-upload/venv"
"$HOME/.cache/agc-upload/venv/bin/python" -m pip install -r <skill目录>/scripts/requirements.txt
```

优先使用 AGC 开发者级 Service Account。凭据从 `--credentials`、`AGC_CREDENTIALS_FILE` 或 `~/.config/agc-upload/credentials.json` 读取，文件权限为 `0600`。不把私钥、client secret、JWT 或临时上传 URL 放进聊天、命令参数、普通日志、skill 或项目仓库。

已有凭据直接使用；缺少时只需用户一次性提供本机文件路径。创建入口、角色和兼容的 API Client 格式见 [认证与接口](references/api.md)。项目级凭据不能代替开发者级发布权限。

下文 `PY` 表示专用环境的 Python，`UPLOAD` 表示本 skill 的 `scripts/agc_upload.py`。示例路径与应用 ID 必须替换为实际值。

```sh
"$PY" "$UPLOAD" discover --bundle-name com.example.app
```

## 签名初始化与复用

先判断是否已有必须沿用的发布签名。用户已明确首次发布且允许创建签名时，直接执行初始化，不再次确认。已有正式应用、备案签名或云签名身份时不能自动更换密钥；API 的证书/Profile 列表为空不能证明云签名不存在。

```sh
"$PY" <skill目录>/scripts/provision_release.py --project /绝对路径/harmonyos --app-id APP_ID --bundle-name com.example.app --product default --create-signing
```

脚本在 `~/.config/agc-upload/signing/<包名>/` 生成本机签名密钥和 CSR，通过 Provisioning API 申请发布证书和 Profile，校验证书公钥、Profile 用途与包名，然后写入本机签名配置。已有状态会复用材料；未知结果的创建请求会停止，不盲目重复申请。签名目录和本机 `build-profile.json5` 不得进入版本库。

后续使用同一流程时加 `--reuse-only`，避免本机签名状态丢失后静默创建新身份。该模式不能重新申请证书或 Profile。仅需沿用项目已有、由其他流程管理的正式签名时，验证现有配置即可，不必迁入此脚本的存储。

签名材料和 Service Account 凭据是两套不同的密钥：不能用 API 鉴权私钥给应用签名。新建材料需要发布证书/Profile 权限；创建流程与恢复边界见 [构建与签名](references/build.md)。

## 构建与版本

1. 读取项目发布约束，确认包名、应用和渠道。通过身份发现接口取得 appId；上传脚本再次校验包名映射，多个匹配项不能随意选择。
2. 核查本机的发布证书/Profile。Release 构建模式不证明签名为发布用途；不能用调试 Profile 上传正式候选。签名材料留在本机已忽略配置中。
   同时执行项目声明的发布资源检查；若项目提供 `scripts/check-app-icons.py`，在具备 Pillow 的 Python 环境运行源码检查，并在构建后用 `--app <本次signed.app>` 检查最终包。HarmonyOS 应用和桌面 Ability 的图标应引用前景/背景分层资源，两层源图均为 1024×1024，背景完整不透明且无预制圆角/内边距。新版 SDK 的 restool 会独立于普通纹理压缩将图标通过 SLR 编码为 512×512；包检查需核对资源 ID、图层与转换后的内容，不应绕过 SDK 或仅凭尺寸放行。AGC 解析正常不能代替图标 UX 预审。
3. 通过 `scripts/build_app.py` 调用 `devecocli` 构建整个 product，HAP 或测试 HAP 不能替代市场 App Pack。

```sh
"$PY" <skill目录>/scripts/build_app.py --project /绝对路径/harmonyos --version-name 1.0.0
```

脚本在本机 `build-profile.json5` 的所选 product 固定 `versionName`，递增数字 `buildVersion`，然后构建 Release `.app`。同一次候选上传正式和测试渠道时复用同一个包和 build 号。失败的构建号也保留；明确重试同一候选时可用 `--build-number N`。`versionCode` 不自动修改，后续已上架版本更新仍须遵循 AGC 版本码规则。需要项目自定义构建模式时传 `--build-mode releaseCandidate`（默认仍为 `release`）。

若项目的 hvigor hook 已实现消费 `AGC_RUNTIME_BUILD_PROFILE`，可把受保护的外置 `build-profile` 传给 `--runtime-profile`：该文件必须在工程目录外、已存在且权限为 `0600`，并包含匹配 product 的 `app.products` 和 `app.signingConfigs`。脚本只在外置文件中预留版本并以绝对路径传入环境变量，保留工程跟踪文件字节不变；未实现此 hook 的项目不适用 runtime 模式。细节见 [构建说明](references/build.md)。

构建日志保存在权限受限的本机目录，不直接输出含签名配置的完整日志。只使用脚本返回的本次新 **signed.app**，避免选中 unsigned 或历史包。上传前执行：

```sh
"$PY" <skill目录>/scripts/verify_app.py /绝对路径/候选-signed.app --bundle-name com.example.app
```

验签脚本调用本机 SDK 的 `hap-sign-tool.jar verify-app` 验证整个 App Pack，并检查内嵌 Profile 为发布用途且包名匹配。未签名包和调试 Profile 都不能通过；签名验证通过仍不代替 AGC 解析结果。

## 上传与恢复

先 `inspect` 核对包名、版本、build 号、Release 元数据和 SHA-256。用户已要求上传且目标、渠道明确时直接执行，不重复索取确认；查看能力、编写 skill 或检查包不表示要执行上传。

```sh
"$PY" "$UPLOAD" inspect /绝对路径/候选.app
"$PY" "$UPLOAD" upload /绝对路径/候选.app --app-id APP_ID --bundle-name com.example.app --channel release --receipt /绝对路径/release-receipt.json
"$PY" "$UPLOAD" upload /绝对路径/候选.app --app-id APP_ID --bundle-name com.example.app --channel testing --receipt /绝对路径/testing-receipt.json
```

两个渠道分别记录回执；只要求一个时只执行对应上传。地区明确时传 `--mainland 1` 或 `0`。`upload --dry-run` 完全离线，可在没有凭据时核对候选。

脚本请求短期上传地址、流式 PUT 原始文件、登记软件包，再轮询解析。不要改成旧 Android multipart 接口，不向存储端发送 AGC Bearer token。

超时或失败后保留回执。已取得 package ID 的任务继续只读查询；文件已上传且登记尚未开始时可续接。登记请求结果不明时先查后台状态，不能重发整条链路。回执不保存签名 URL/凭据，不能用其他候选覆盖。

```sh
"$PY" "$UPLOAD" resume --receipt /绝对路径/receipt.json
"$PY" "$UPLOAD" status --app-id APP_ID --package-id PACKAGE_ID
```

用 `--help` 核对脚本选项。未知返回结构、鉴权失败或解析失败应报告阶段与脱敏错误码，不吞成成功，不反复上传试探。

正式候选上传不等于提审；测试包上传不等于绑定测试版本、发送邀请或开放分发。当前 skill 不执行这些额外操作。交付时报告版本/build、SHA-256、渠道、package ID、解析结果和回执路径，说明未完成环节。

## 维护与验证

接口改变时查 [认证与接口](references/api.md) 的官方来源。网页为空壳时可读取同一 URL 的 `.md` 版本。修改脚本后运行 `python -m unittest discover -s <skill目录>/scripts -p 'test_*.py'`；离线模拟不能冒充真实上传验证。
