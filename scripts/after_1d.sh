#!/bin/bash
# 1D K线同步backfill_db.py完成后执行

echo "[$(date)] 1D 任务完成"

# 调 Python 脚本
python sync_sector_daily.py     # 同步行业/板块/概念数据


echo "[$(date)] 1D 批处理完成"
