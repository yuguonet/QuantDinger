# -*- coding: utf-8 -*-
"""
focal_loss.py — LightGBM 二分类 focal loss 自定义目标函数（实验用）

作用:
  为 P0-A 尾部二分类提供 focal loss 训练目标（改进文档 §2）。
  尾部正样本占比仅 3~5%，普通 logloss 把算力花在大量易分负样本上；
  focal loss 通过 (1-pt)^γ 调制，聚焦"难分样本"（预测错的尾部事件）。

用法:
  from focal_loss import focal_loss_objective, focal_loss_eval
  m = lgb.LGBMClassifier(objective=focal_loss_objective(gamma=2.0, alpha=0.5),
                         eval_metric="auc", ...)

口径:
  - 标签 y ∈ {0,1}（float）；内部对 pred 做 sigmoid 得 p，裁剪到 [eps, 1-eps] 防 log(0)。
  - α 是正样本权重（α=0.5 等价无权重；α>0.5 加重正样本）；γ 聚焦强度（γ=0 退化为
    加权 logloss，可用作自检）。
  - hess 精确二阶导后裁剪到 ≥ hess_min：LightGBM 要求非负 hess，focal 的二阶导在
    极端 p 区间可能微负，裁剪是标准做法（不影响凸性近似质量）。

易错点:
  1. grad/hess 必须对 logit z 求导（不是对 p）；本模块已换算，勿重复乘 p(1-p)。
  2. 调用签名双约定：lightgbm 原生接口是 func(preds, dataset)，但 sklearn 接口
     (LGBMClassifier) 包装后是 func(labels, preds) 且都是 ndarray——本模块自动识别
     两种约定，换接口不必改代码。
  3. custom objective 下 sklearn 接口的早停需要显式 eval_metric（如 "auc"），
     否则 LGBM 不知道用什么指标早停。
  4. γ=0 时 focal ≡ 加权 logloss，数值上应与 scale_pos_weight=α/(1-α) 的
     grad 同号等比（冒烟测试用此性质自检）。
  5. focal 的二阶导在易分样本区可为负（本质性质），hess_min 裁剪是标准做法；
     数值校验时只在 hess 显著正区比对。
"""
from __future__ import annotations

import numpy as np


def focal_loss_objective(gamma: float = 2.0, alpha: float = 0.5, eps: float = 1e-6,
                         hess_min: float = 1e-7):
    """返回可直接传给 lightgbm 的 objective(preds, dataset) -> (grad, hess)。"""
    if not (0.0 <= alpha <= 1.0):
        raise ValueError(f"alpha 需在 [0,1]: {alpha}")
    if gamma < 0:
        raise ValueError(f"gamma 需 >= 0: {gamma}")

    def objective(a, b):
        # 双约定识别(易错点2): 原生 func(preds, dataset) vs sklearn func(labels, preds)
        if hasattr(b, "get_label"):
            z = np.asarray(a, dtype=np.float64)
            y = np.asarray(b.get_label(), dtype=np.float64)
        else:
            y = np.asarray(a, dtype=np.float64)
            z = np.asarray(b, dtype=np.float64)
        p = 1.0 / (1.0 + np.exp(-z))
        p = np.clip(p, eps, 1.0 - eps)
        om = 1.0 - p                      # 1-p
        ln_p = np.log(p)
        ln_om = np.log(om)

        grad = np.empty_like(p)
        hess = np.empty_like(p)

        pos = y > 0.5
        neg = ~pos

        # 正样本: FL = -α (1-p)^γ ln p ; d/dz 见头注(勿重复乘 p(1-p))
        if pos.any():
            pp, oop, lnp = p[pos], om[pos], ln_p[pos]
            core = gamma * pp * lnp - oop
            grad[pos] = alpha * (oop ** gamma) * core
            dg_dp = alpha * (
                -gamma * (oop ** (gamma - 1.0)) * core
                + (oop ** gamma) * (gamma * (lnp + 1.0) + 1.0)
            )
            hess[pos] = pp * oop * dg_dp

        # 负样本: FL = -(1-α) p^γ ln(1-p)
        if neg.any():
            pp, oop, lnom = p[neg], om[neg], ln_om[neg]
            core = pp - gamma * oop * lnom
            grad[neg] = (1.0 - alpha) * (pp ** gamma) * core
            dg_dp = (1.0 - alpha) * (
                gamma * (pp ** (gamma - 1.0)) * core
                + (pp ** gamma) * (1.0 + gamma + gamma * lnom)
            )
            hess[neg] = pp * oop * dg_dp

        return grad, np.maximum(hess, hess_min)

    return objective


def focal_loss_eval(gamma: float = 2.0, alpha: float = 0.5, eps: float = 1e-6):
    """配套 feval（监控用）：返回 focal loss 值，name='focal'。"""
    def feval(a, b):
        if hasattr(b, "get_label"):
            z = np.asarray(a, dtype=np.float64)
            y = np.asarray(b.get_label(), dtype=np.float64)
        else:
            y = np.asarray(a, dtype=np.float64)
            z = np.asarray(b, dtype=np.float64)
        p = np.clip(1.0 / (1.0 + np.exp(-z)), eps, 1.0 - eps)
        pt = np.where(y > 0.5, p, 1.0 - p)
        at = np.where(y > 0.5, alpha, 1.0 - alpha)
        loss = -at * (1.0 - pt) ** gamma * np.log(pt)
        return "focal", float(loss.mean()), False
    return feval
