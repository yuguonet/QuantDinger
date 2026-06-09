# MEMORY.md

## 用户
- 做A股量化交易，目标小资金快速复利
- 本地环境：Windows10 + 40核 64G主机，D:\QuantDinger\, powershell, vscode,FLASK, OLLAMA,postgresSQL
- 项目: https://github.com/yuguonet/QuantDinger
- 项目中所有_(下划线)都是内部函数,不能被外部调用,需要调用前必须先说明
- 前端:QuantDinger-Vue, 后端:backend_api_python
- 配置文件在backend_api_python/.env不上传,上传的是env.spalme
- 第一次进行大量修改需要新建函数时先对后端backend_api_python/app/下扫描所有py文件检查是否有现成的函数
- 工作完成后应该按目录位置打包修改过的或新增的文件放到和USER.md同目录下
- 能通过修改上游解决问题的不修改下游.
## 待办
- QuantDinger Agent架构重设计，详见 AGENT_REDESIGN.md

