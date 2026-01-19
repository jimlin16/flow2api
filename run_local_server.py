import uvicorn
import os
import sys

# 將當前目錄添加到 Python 路徑，確保能找到 src 包
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

if __name__ == "__main__":
    print("正在啟動本地 Flow2API 服務...")
    print("請確保您已安裝依賴: pip install -r requirements.txt")
    print("服務將運行在: http://localhost:38000")
    
    # 啟動 uvicorn，監聽 38000 端口以匹配 Docker 的映射習慣
    # reload=True 方便開發調試
    uvicorn.run("src.main:app", host="0.0.0.0", port=38000, reload=False)
