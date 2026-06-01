import subprocess, sys, os
from datetime import datetime

def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")

def main():
    log("1D 任务完成")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tasks = [
        ("sync_index_daily.py", None),
        ("sync_sector_daily.py", None),
#        ("sync_index_daily.py", ["--market", "CNStock"]),
#        ("sync_sector_daily.py", ["--force"]),        
    ]
    for s, args in tasks:
        subprocess.run([sys.executable, os.path.join(script_dir, s)] + (args or []), cwd=script_dir)
    log("1D 批处理完成")

if __name__ == "__main__":
    main()