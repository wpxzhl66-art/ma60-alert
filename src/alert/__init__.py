"""实时预警系统：股价上穿 60 周线 → Server酱微信推送 → 跳转同花顺。

模块：
  config     读取 config/alert_config.yaml
  quoter     腾讯批量实时行情
  ma60_base  60周线快照（MA60_prev / 标签 / 行业 / 年内上穿计数）
  update     日线增量更新（baostock，收盘后）
  pusher     Server酱推送（含每日额度护栏）
  scanner    触发判定 + 同日/同周去重状态
  template   推送文案模板（含跳转链接与回测参照）
"""
