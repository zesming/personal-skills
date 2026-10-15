# HarmonyOS AGC API 合同

核对日期：2026-09-14（上架自检接口范围）；上传与签名合同沿用既有验证。以下为 HarmonyOS 专用接口；不套用旧 Android 上传流程。API 基址为 `https://connect-api.cloud.huawei.com`。

## 认证

AGC「用户与访问 → API密钥 → Connect API → Service Account」创建**开发者级**账号，赋予相应 APP 管理权限。上传接口允许账号持有者、管理员、APP 管理员、运营。保存下载的 private.json，包含 `key_id/private_key/sub_account`。

Service Account：本地 PS256 JWT；header 为 `kid/typ=JWT/alg=PS256`；payload 为 `iss=sub_account`、`aud=https://oauth-login.cloud.huawei.com/oauth2/v3/token`、UTC 秒级 `iat`、`exp=iat+3600`。直接用 Bearer JWT 调 API，不将 JWT 再拿去换 token。

已有 API Client：JSON 包含 `client_id/client_secret`。向 `/api/oauth2/v1/token` POST JSON，`grant_type=client_credentials`；后续带 Bearer token 与 `client_id` header。需要团队级客户端（项目 N/A），新凭据优先 Service Account。

来源：[服务端授权](https://developer.huawei.com/consumer/cn/doc/app/agc-help-connect-api-obtain-server-auth-0000002271134661)。

## 上传步骤

| 阶段 | 方法与路径 | 合同 |
| --- | --- | --- |
| 目标发现 | GET `/api/publish/v2/appid-list` | 每次仅 query 一个 `packageName`，`packageTypes=7`；`appids[].key/value` 为显示名称/appId，用该查询结果核验目标 ID |
| 上传地址 | GET `/api/publish/v2/upload-url/for-obs` | query `appId/fileName/contentLength/sha256`，可选 `chineseMainlandFlag=0或1`；返回 `urlInfo` |
| 传输 | PUT `urlInfo.url` | 原始文件字节，使用返回的 headers；HTTP 200 为传输成功 |
| 正式登记 | PUT `/api/publish/v3/app-package-info?appId=…` | JSON `fileName/objectId`；返回 `packageId` |
| 测试登记 | POST `/api/publish/v2/test/version/pkg?appId=…` | JSON `distributeMode=1,file:{fileName,objectId}`；返回单元素字符串数组 `pkgVersion`，其中的值是测试包 ID，用它轮询；不调用正式登记 |
| 解析状态 | GET `/api/publish/v3/package/compile/status` | query `appId/pkgIds`；`pkgStateList[].pkgId/successStatus`，0正常、1解析中、2失败 |

`urlInfo` 含 `objectId/url/method/headers`；URL 约五分钟过期。上传 headers 含存储 Authorization、x-amz-content-sha256、x-amz-date、Host、user-agent、Content-Type，与 AGC Bearer 鉴权不同。文件长度须与申请值相同。普通 APP 上限 4 GB，元服务 10 MB。

JSON API 同时核查 HTTP 与 `ret.code=0`。仅 HTTP 200、空列表或未知枚举不算解析成功。登记超时不证明未登记，保留 objectId、候选哈希和阶段后先查询。

官方来源：

- [包名对应 appId](https://developer.huawei.com/consumer/cn/doc/app/agc-help-publish-api-appid-list-0000002271000617)
- [上传地址](https://developer.huawei.com/consumer/cn/doc/app/agc-help-upload-api-upload-url-0000002236201294) 与 [CommonUrlInfo](https://developer.huawei.com/consumer/cn/doc/app/agc-help-upload-api-data-commonurlinfo-0000002272577809)
- [文件上传](https://developer.huawei.com/consumer/cn/doc/app/agc-help-upload-api-upload-file-0000002271160621)
- [正式登记](https://developer.huawei.com/consumer/cn/doc/app/agc-help-publish-api-app-package-info-update-0000002236201250)
- [测试登记](https://developer.huawei.com/consumer/cn/doc/doccenter-submission/agc-help-test-api-add-test-package-0000002236201330)
- [解析查询](https://developer.huawei.com/consumer/cn/doc/app/agc-help-publish-api-query-compile-status-0000002236041434) 与 [PackageStates](https://developer.huawei.com/consumer/cn/doc/app/agc-help-publish-api-data-packagestates-0000002271160601)
- [包详情](https://developer.huawei.com/consumer/cn/doc/app/agc-help-publish-api-query-package-info-0000002328848469)，可核对 versionName/versionCode/buildVersion。

`/api/publish/v3/app-submit` 是独立提审操作，当前脚本不调用；测试绑定、邀请与提交同样不包含在包上传中。

## 软件包上架自检的 API 边界

公开目录中仅有邀请测试的 `GET /api/publish/v2/test/app/version/detect-report?appId=…&versionId=…`，不是软件包管理的自检接口。本 skill 仅通过 API 完成上传和解析确认，上架自检由用户手动操作。不要为此创建或提交邀请测试版本，也不要把 `pkgVersion` 返回的测试包 ID 当成邀请测试版本 ID。

华为文档页面为 SPA；网页或 `.md` 无正文时，可读取公开文档门户 `POST https://svc-drcn.developer.huawei.com/community/servlet/consumer/cn/documentPortal/getDocumentById`，JSON 为 `{"objectId":"官方文档 slug","language":"cn"}`，正文位于 `value.content.content`。它只用于读取官方文档，不是 AGC 业务接口。
