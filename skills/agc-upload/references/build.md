# 构建与签名

默认用 `devecocli build --product <name> --build-mode release` 构建，之后交给 AGC 上传脚本。`build_app.py` 的 `--build-mode` 会原样传递工程支持的模式名，例如 `releaseCandidate`。

## 版本

- `versionName` 是可见版本，例如 `1.0.0`；同一版本候选的 `buildVersion` 为 `1`、`2`、`3`。同一次候选的正式与测试上传使用同一个 build 和 SHA-256。
- `versionCode` 是独立字段，不自动写入 build 号。后续已发布应用更新时仍需核对 AGC 版本码要求。
- `build_app.py` 在本机产品配置保存递增值。另一工作副本/机器须从已有上传记录或 CI 取得下一编号，再用 `--build-number` 传入；不能将各机从 1 开始当作全局递增。

`buildVersion` 从 API 23、DevEco Studio 6.1.0 Beta2 起支持；长度 1–18 字节，仅数字和最多两个点，数字段无前导零。所有 HAP/HSP 该字段必须一致。产品级覆盖位于 `build-profile.json5` 的 `app.products[]`，脚本仅更新目标的 versionName/buildVersion，保留签名与版本码值，写回权限 0600。

来源：[工程 build-profile](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/ide-hvigor-build-profile-app)、[app.json5](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/app-configuration-file)、[打包工具](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/packing-tool)。

## 外置 runtime profile

项目已实现消费 `AGC_RUNTIME_BUILD_PROFILE` 的 hvigor hook 时，使用工程外的受保护配置构建候选：

```sh
"$PY" <skill目录>/scripts/build_app.py \
  --project /绝对路径/harmonyos --product default \
  --version-name 1.0.0 --build-mode releaseCandidate \
  --runtime-profile /绝对路径/runtime-build-profile.json5
```

`--runtime-profile` 必须指向工程目录外已存在且权限为 `0600` 的 JSON/JSON5 文件，文件应有 `app.products`、`app.signingConfigs`，且 product 名称与工程配置匹配。脚本只在该文件中预留 `buildVersion`/`versionName`，把其绝对路径设置为 `AGC_RUNTIME_BUILD_PROFILE` 后调用 `devecocli`，不会写工程的 tracked `build-profile.json5`；工程级构建锁仍然生效。没有相应 hvigor hook 的项目不能使用此模式。

## 签名

市场包需要发布用途证书和匹配包名的 Profile。Release 模式、signed 文件名或 .app 后缀均不证明签名用途；用 SDK 签名工具验证最终 `.app`，并检查内嵌 Profile。当前 DevEco 对整个 App Pack 签名，包内 HAP 可以是未签名内容，其字节由外层签名保护。不要输出 Profile 正文、签名别名或密码。

只有调试 Profile 时先取得发布材料或核实官方云签名流程。不要修改签名内容伪造类型，不假设 AGC 上传权限能导出云端私钥。

`inspect` 的结构、元数据和哈希检查不能替代签名验证/AGC 检测；已安装 HAP、entry@ohosTest 测试包不可直接用作市场包。

来源：[发布与云签名](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/ide-publish-app)。

## Provisioning API 首次发布

用户已确认可创建新发布身份时，使用 `provision_release.py`：先在本机持久化受保护的签名私钥和 CSR，再申请证书，最后申请 Profile。应用签名密钥独立于 AGC Service Account。

| 步骤 | 接口 | 请求与结果 |
| --- | --- | --- |
| 发布证书 | POST `/api/publish/v3/cert` | `csr/certName/certType=2`，返回 `certInfo.id/certDownloadUrl` |
| 发布 Profile | POST `/api/publish/v3/provision` | `provisionName/provisionType=2/certId/appId`，返回 `provisionInfo.provisionDownloadUrl` |
| 查询证书 | POST `/api/publish/v3/cert/list` | 只读查询，`certType=2` 可筛选发布证书；实际响应 `certList` |
| 查询 Profile | GET `/api/publish/v3/provision/list` | `appId` 在请求头；分页 `fromRecCount/maxReqCount`，返回 `provisionList` |

证书/Profile 下载使用 API 返回的短期 HTTPS 地址，不附带 AGC Bearer。请求创建前先保存阶段，结果不明时从现有证书/Profile 列表核对，不能重复创建或丢弃本机私钥。复用时检查有效期、公钥和包名；密钥不匹配或过期不能自动轮换。

SDK 工具验签可用 `verify-app -inFile … -outCertChain … -outProfile …`，无需在命令参数中提供密码。签名由配置好本机材料的 `devecocli build` 完成，工具输出保存在本机，不将签名别名或密码复制到聊天。

已验证的 API 返回差异：证书下载可能使用 `.myhuaweicloud.cn` OBS 域名；PEM 链可能根证书在前，应通过 CSR 公钥识别叶证书并验证、重排签发链，不能默认第一张就是应用证书。若创建成功但本地解析失败，先通过只读证书列表取得既有记录，下载并确认公钥匹配后恢复下载阶段，不能重新申请证书。

官方来源：[申请证书](https://developer.huawei.com/consumer/cn/doc/app/agc-help-provision-api-apply-cent-0000002236201302)、[申请 Profile](https://developer.huawei.com/consumer/cn/doc/app/agc-help-provision-api-apply-provision-0000002271000689)、[查询证书](https://developer.huawei.com/consumer/cn/doc/app/agc-help-provision-api-query-cent-0000002271160629)、[查询 Profile](https://developer.huawei.com/consumer/cn/doc/app/agc-help-provision-api-query-provision-0000002236041506)、[签名流水线](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/ide-command-line-building-app)。
