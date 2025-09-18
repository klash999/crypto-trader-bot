from datetime import datetime
from zoneinfo import ZoneInfo
from config import CFG

TZ = ZoneInfo(CFG["TZ"]) if CFG.get("TZ") else None

def now_local():
    return datetime.now(TZ)
