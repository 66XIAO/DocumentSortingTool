"""LLM 子系统。

provider       Provider 协议 + 统一的五类失败分类
openai_compat  OpenAICompatProvider，覆盖兼容 /v1/chat/completions 的服务
ollama         OllamaProvider，全本地零外发
taxonomy       两阶段调用、类目归并、<=12 类 <=2 级收敛
cache          SQLite 批级缓存

整个子系统是可选增强：ai.enabled 默认 false，任何失败都回落到规则引擎，
不构成主流程的单点故障（需求 6）。
"""
