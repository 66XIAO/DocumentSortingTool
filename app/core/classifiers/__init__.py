"""分类器实现。

base 定义 Classifier 协议、Suggestion、ClassifierPipeline 与四种策略的装配表；
其余模块是具体分类器，按 priority 参与管线求值（关键词 200 > 扩展名 100）。
"""
