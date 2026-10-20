# B站视频推荐 · AstrBot

给 AstrBot 一个稳定、直接的公开视频推荐入口：**关键词 → Tavily搜索 → B站公开元数据核实 → 标题、UP主、链接和可用封面**。

不需要大模型决定是否调用搜索工具，不绑定角色、QQ号、NapCat实例或服务器。手动指令独立运行；如果已经安装 Private Companion，可选择让它在原有主动聊天调度中分享视频。

## 快速开始

1. AstrBot WebUI → 插件 → 从链接安装：

   ```text
   https://github.com/Ackerley1123/astrbot_plugin_bilibili_recommend
   ```

2. 插件配置中填写 `tavily_api_keys`。已有 AstrBot Tavily Key 时，可留空并保持 `reuse_astrbot_tavily=true`。独立Key优先。申请入口：[Tavily](https://app.tavily.com/)。
3. 保存配置并重载插件，在私聊中发送：

   ```text
   /B站推荐
   /B站推荐 JPOP
   /B站推荐 YOASOBI
   /B站推荐 新番
   ```

默认兴趣为动画、二次元、JPOP，可以在 `topics` 中换成自己的公开兴趣。指定参数时优先使用该关键词。每次推荐一个视频。

默认指令前缀为 `/`；如修改了 AstrBot 唤醒前缀，请使用对应前缀。别名包括 `/b站推荐`、`/bili推荐`、`/推荐B站视频`。包含空格的短语请用 AstrBot 支持的引号语法包裹，例如 `/B站推荐 "YOASOBI live"`。

回复示例：

```text
🎬 视频真实标题
UP主：视频发布者
https://www.bilibili.com/video/实际BV号/
[可用时附封面]
```

封面或图片发送依赖适配器能力；链接始终作为文本返回。**推荐链接和封面，不下载或发送视频文件。**

## 配置

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `tavily_api_keys` | `[]` | 插件独立Key，支持最多尝试3个Key；UI遮罩 |
| `reuse_astrbot_tavily` | `true` | 无独立Key时读取当前会话的AstrBot Tavily Key |
| `topics` | 动画、二次元、JPOP | 无关键词时依次轮换 |
| `allow_groups` | `false` | 默认只在私聊提供指令 |
| `allowed_user_ids` | `[]` | 发送者白名单；空列表不限制 |
| `allowed_sessions` | `[]` | 精确会话UMO白名单；空列表不限制 |
| `cooldown_seconds` | `15` | 同一会话手动请求间隔 |
| `timeout_seconds` | `60` | 整次推荐超时 |
| `max_results` | `7` | 一次搜索候选数，1—10 |
| `send_cover` | `true` | 获取并附带封面 |
| `cover_max_mb` | `6` | 每张封面最多6MiB，可调1—10 |
| `cache_limit` | `50` | 最多缓存封面数 |
| `dedup_limit` | `100` | 各会话最近100个视频不重复 |
| `enable_companion_bridge` | `false` | 可选主动分享接入 |
| `proactive_targets` | `[]` | 明确允许主动联系的私聊UMO |

用户和会话白名单同时配置时取交集。打开群聊开关不会绕过白名单。手动请求不依赖空闲时间、免打扰时段或主动消息配额；这些是主动聊天调度的规则。

## 可选：主动分享

手动指令**不依赖** Private Companion。需要主动分享时：

1. 安装支持 `get_private_companion_api().register_proactive_ability()` 的 [Private Companion](https://github.com/menglimi/astrbot_plugin_private_companion)。
2. 在陪伴插件中授权目标私聊，设置主动联系上限、空闲间隔、免打扰。
3. 本插件打开 `enable_companion_bridge`，将已授权的私聊UMO填入 `proactive_targets`。UMO可从 AstrBot 会话信息中取得；它包含平台实例、消息类型和会话标识，不能只填写裸用户ID。
4. 为避免把跨平台规范化身份错误当作发送者ID，启用桥接时使用 `allowed_sessions` 限制会话，并保持 `allowed_user_ids` 为空。后者非空时桥接候选会关闭，手动白名单仍正常工作。
5. 重载本插件。更换或重载陪伴插件后，也请重载本插件以重新接入。

视频能力默认冷却2小时，失败重试至少间隔15分钟；在陪伴插件扩展页可能有已保存的能力参数，以该页面为准。**不会自行启动另一个定时发送器，不会代替用户授权主动私聊。**

手动与主动共享会话去重，手动请求不改变主动检索的冷却时间。标题、UP主、发布时间仅作为素材，主动上下文明示不要声称已经播放、看完或听完，也不要把旧视频称为新发布。

## 数据与网络

- 搜索关键词来自本次指令显式参数或已配置的公开兴趣；不读取聊天历史、人设、记忆数据库或账号密码，不自动把私人近况提交检索。
- Tavily请求固定发送到官方 `https://api.tavily.com/search`，使用 `basic` 搜索。通常一次推荐消耗一次搜索；Key不可用/限额时最多尝试配置中的前三个Key。费用、额度以Tavily账户为准。
- 从搜索结果中仅接受标准B站BV视频链接，再读取B站公开元数据接口，只有核实成功才推荐。索引结果不等同B站个性化首页，不保证最新、官方投稿或特定内容分级；管理员应选择适合受众的主题。
- 封面仅从B站 `*.hdslb.com` 的HTTPS地址获取，不跟随重定向。限制下载大小、图片格式、像素数和缓存数量。封面失败保留文本，不捏造视频或图片。
- 不需要B站账号/Cookie，不播放视频，不自动点赞、投币、收藏、关注或评论。
- 状态位于 AstrBot 插件数据目录；会话标识以SHA-256摘要作为键，最近推荐ID按会话隔离，最多保存500个会话。摘要用于减少直接标识，不等同匿名化或加密。
- `secret: true` 只是WebUI遮罩，Key仍由AstrBot保存在配置文件里；不要上传运行配置、状态、日志或缓存。仓库的 `.gitignore` 已排除这些文件。

## 常见问题

**没有搜索结果或B站接口失败？**

B站公开接口和搜索索引都可能受网络、风控、删除或地区限制影响。插件不会绕过验证或编造结果；请稍后重试或换关键词。可先确认Tavily Key额度及服务器到Tavily、B站API、封面域名的连通性。

**搜到的是旧视频？**

首版优先保证可核实、可打开的公开视频推荐，不做“最新资讯”保证。要找特定时期的内容，可在关键词中明确年份；仍应以视频页面时间为准。

**指令没响应？**

确认前缀、插件启用状态、配置权限、依赖和日志。若安装过同名指令的本地兴趣分享扩展，请停用旧扩展或解决指令重名，避免两个处理器争用 `/B站推荐`。

**能用自然语言直接调用吗？**

首版提供明确指令，不注册LLM工具。普通自然语言搜索仍由你现有的AstrBot工具和模型决定；使用本插件指令可以直接触发推荐流程。

## 开发与验证

Python 3.10+；AstrBot >=4.29.1,<5。适配层使用AstrBot公开的配置、事件、消息组件和数据目录API。当前主要在AstrBot 4.29.1 / OneBot场景验证，其他适配器的图片投递需要各自验收；没有宣称所有平台都已实测。

```bash
python -m venv .venv
# 激活虚拟环境后执行
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用公开格式的合成数据与模拟网络，不要求真实Key，不发送平台消息。GitHub Actions会在Python 3.10/3.12上运行测试。

项目文件：`main.py` 为AstrBot入口，`service.py` 为独立推荐逻辑，`_conf_schema.json` 为配置界面，`tests/` 为测试。开发时无需安装陪伴插件。

## 参考与许可

- [AstrBot插件开发](https://docs.astrbot.app/dev/star/plugin-new.html)、[插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)、[消息发送](https://docs.astrbot.app/dev/star/guides/send-message.html)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- 可选桥接使用 [Private Companion公开扩展API](https://github.com/menglimi/astrbot_plugin_private_companion#开发者接入)，未包含其实现或运行数据。

代码以 [MIT License](LICENSE) 开源。视频和封面归各自权利人所有；本仓库不分发视频文件或预置封面。
