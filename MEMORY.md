# MEMORY.md

## 用户
- 做A股量化交易，目标小资金快速复利
- 本地环境：Windows10 + 40核 64G主机，D:\QuantDinger\, powershell, vscode,FLASK, OLLAMA,postgresSQL
- 项目: https://github.com/yuguonet/QuantDinger
- 项目中所有_(下划线)都是内部函数,不能被外部调用,需要调用前必须先说明
- 前端:QuantDinger-Vue, 后端:backend_api_python
- 配置文件在backend_api_python/.env不上传,上传的是env.spalme
- 工作完成后应该按目录位置打包修改过的或新增的文件放到和USER.md同目录下

## 核心认知
- **价格折扣一切** — 所有信息(政策/消息/基本面)最终都反映在价格上，K线是唯一能回测验证的地基
- **分项记分制** — 每个子项0-100打分，-1000否决，加权求和，无优先级，权重可迭代
- **数据陷阱** — 龙虎榜(盘后+游资一日游)、资金流向(滞后)、新闻(你看到时市场已反应)

## 待办
- QuantDinger Agent架构重设计，详见 2026-06-07.md + AGENT_REDESIGN.md

