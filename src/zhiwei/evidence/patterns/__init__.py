"""Evidence PatternRef 独立复算组件（spec s8 §2/§5）。

deterministic known-pattern 路径的复算层：从 snapshot 序列独立重算模式的结构、
窗口与 realized SNR——detector 与 scorer 共享版本化 kind/unit，不共享实现
（docs/RISK_EVAL.md §4）。
"""
